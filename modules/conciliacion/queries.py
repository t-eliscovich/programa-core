"""Queries para la conciliación bancaria."""

from __future__ import annotations

import hashlib
from datetime import date

import db


def cheques_depositados_rango(desde: date, hasta: date) -> list[dict]:
    """Cheques en stat='B' (depositado Pichincha) con fechad en el rango.

    Después de la migración 0013, el stat 'D' se usa para "Daniela" (gestión
    de cobranza). Los cheques depositados quedan en 'B' (vocabulario
    canónico 2026-04-29). Antes este filtro era stat='D' y no devolvía nada
    para conciliar.

    Se excluyen Z (en cartera), R (reversados), A (acreditados — cleared),
    P (postergados), D (Daniela). Sólo 'B' es el universo de cheques
    depositados a investigar contra el extracto del banco.
    """
    return (
        db.fetch_all(
            """
        SELECT id_cheque, no_cheque, fecha, fechad, importe, codigo_cli
          FROM scintela.cheque
         WHERE stat = 'B'
           AND fechad BETWEEN %s AND %s
         ORDER BY fechad DESC, id_cheque DESC
        """,
            (desde, hasta),
        )
        or []
    )


def cheque_por_id(id_cheque: int) -> dict | None:
    return db.fetch_one(
        """
        SELECT id_cheque, no_cheque, codigo_cli, importe, stat, fechad
          FROM scintela.cheque
         WHERE id_cheque = %s
        """,
        (id_cheque,),
    )


# ─── Log manual de conciliación de depósitos ──────────────────────────────
# Migration 0039_conciliacion_manual_log.sql


def firma_deposito(fecha, valor, codigo: str, concepto: str) -> str:
    """Genera una firma estable para un depósito del Excel.

    Misma fecha + mismo valor + mismo código + mismo concepto → misma firma.
    Usada para dedupe del log de conciliación manual.
    """
    fecha_s = fecha.isoformat() if hasattr(fecha, "isoformat") else str(fecha or "")
    raw = f"{fecha_s}|{float(valor or 0):.2f}|{codigo or ''}|{(concepto or '')[:80]}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def marcar_deposito(
    *,
    firma_dep: str,
    fecha_dep,
    valor_dep: float,
    codigo_dep: str,
    concepto_dep: str,
    accion: str,
    id_transaccion: int | None = None,
    nota: str = "",
    usuario: str = "web",
) -> dict:
    """Inserta una decisión del usuario en `conciliacion_manual_log`.

    Inserta SIEMPRE una nueva fila (el log es append-only para auditoría).
    Si querés saber el estado actual de un depósito, usá `ultimo_estado_dep`.
    """
    if accion not in ("confirmado", "rechazado", "pendiente"):
        raise ValueError(f"acción inválida: {accion!r}")
    row = db.fetch_one(
        """
        INSERT INTO scintela.conciliacion_manual_log
            (firma_dep, fecha_dep, valor_dep, codigo_dep, concepto_dep,
             accion, id_transaccion, nota, usuario)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, creado_en
        """,
        (
            firma_dep,
            fecha_dep,
            valor_dep,
            codigo_dep or "",
            (concepto_dep or "")[:1000],
            accion,
            id_transaccion,
            (nota or "")[:500],
            usuario[:50],
        ),
    )
    return {"id": int(row["id"]), "creado_en": row["creado_en"]} if row else {}


# ─── Selector de banco (Fase C — 2026-05-23) ─────────────────────────────


def bancos_disponibles() -> list[dict]:
    """Lista de bancos para el dropdown del upload.

    Ordenamos Pichincha primero (default), después por no_banco ASC.
    Si la columna `nombre` no existe en `scintela.banco` (legacy schemas),
    devolvemos solo el id como nombre.
    """
    rows = db.fetch_all(
        """
        SELECT no_banco,
               COALESCE(nombre, 'Banco ' || no_banco::text) AS nombre
          FROM scintela.banco
         ORDER BY (no_banco = 10) DESC, no_banco ASC
        """
    ) or []
    return [{"no_banco": int(r["no_banco"]), "nombre": r["nombre"]} for r in rows]


def nombre_banco(no_banco: int) -> str | None:
    """Nombre legible de un banco, o None si no existe."""
    row = db.fetch_one(
        "SELECT COALESCE(nombre, 'Banco ' || no_banco::text) AS nombre "
        "FROM scintela.banco WHERE no_banco = %s",
        (int(no_banco),),
    )
    return row["nombre"] if row else None


# ─── Últimos extractos procesados (Fase E — 2026-05-23) ───────────────────


# ─── Tracking de uploads (TMT 2026-05-26 dueña) ───────────────────────────
# La dueña quiere "ver los files ya subidos" — incluyendo los que se subieron
# pero NO se confirmaron matches. ultimos_extractos() abajo solo ve los que
# tienen al menos 1 match confirmado en banco_conciliacion_match, así que es
# ciego a "subí pero no confirmé nada".
#
# Bootstrap defensivo de la tabla — el deploy no corre migrate.py auto y la
# migración 0053 puede quedar pending.

_BOOTSTRAP_UPLOAD_SQL = """
CREATE TABLE IF NOT EXISTS scintela.conciliacion_upload (
    id            BIGSERIAL PRIMARY KEY,
    no_banco      INTEGER NOT NULL,
    filename      TEXT,
    file_hash     TEXT,
    n_filas       INTEGER,
    fecha_min     DATE,
    fecha_max     DATE,
    usuario       TEXT,
    creado_en     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_concil_upload_banco_fecha
    ON scintela.conciliacion_upload (no_banco, creado_en DESC);
"""
_upload_tabla_bootstrapped = False


def _bootstrap_upload_tabla() -> None:
    global _upload_tabla_bootstrapped
    if _upload_tabla_bootstrapped:
        return
    try:
        db.execute(_BOOTSTRAP_UPLOAD_SQL)
    except Exception:
        pass
    _upload_tabla_bootstrapped = True


def registrar_upload(
    *,
    no_banco: int,
    filename: str,
    file_hash: str | None,
    n_filas: int,
    fecha_min,
    fecha_max,
    usuario: str = "web",
) -> int | None:
    """Inserta un registro de upload. Devuelve id o None si falló (fail-soft)."""
    _bootstrap_upload_tabla()
    try:
        row = db.fetch_one(
            """
            INSERT INTO scintela.conciliacion_upload
                (no_banco, filename, file_hash, n_filas, fecha_min, fecha_max, usuario)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (int(no_banco), (filename or "")[:200],
             (file_hash or "")[:64] or None,
             int(n_filas or 0),
             fecha_min, fecha_max,
             (usuario or "web")[:50]),
        )
        return int(row["id"]) if row else None
    except Exception:
        return None


def uploads_recientes(no_banco: int | None = None, limit: int = 20) -> list[dict]:
    """Lista los últimos uploads de extractos, con info de si tienen matches confirmados.

    El JOIN con banco_conciliacion_match (matches del MISMO día/banco) es
    aproximado — banco_conciliacion_match no tiene FK a conciliacion_upload.
    Como proxy: cuenta los matches confirmados (no_banco, mismo día calendario).
    """
    _bootstrap_upload_tabla()
    try:
        if no_banco is not None:
            rows = db.fetch_all(
                """
                SELECT u.id, u.no_banco, u.filename, u.file_hash, u.n_filas,
                       u.fecha_min, u.fecha_max, u.usuario, u.creado_en,
                       (SELECT COUNT(*) FROM scintela.banco_conciliacion_match m
                          WHERE m.no_banco = u.no_banco
                            AND DATE(m.creado_en) = DATE(u.creado_en)) AS n_matches
                  FROM scintela.conciliacion_upload u
                 WHERE u.no_banco = %s
                 ORDER BY u.creado_en DESC
                 LIMIT %s
                """,
                (int(no_banco), int(limit)),
            ) or []
        else:
            rows = db.fetch_all(
                """
                SELECT u.id, u.no_banco, u.filename, u.file_hash, u.n_filas,
                       u.fecha_min, u.fecha_max, u.usuario, u.creado_en,
                       (SELECT COUNT(*) FROM scintela.banco_conciliacion_match m
                          WHERE m.no_banco = u.no_banco
                            AND DATE(m.creado_en) = DATE(u.creado_en)) AS n_matches
                  FROM scintela.conciliacion_upload u
                 ORDER BY u.creado_en DESC
                 LIMIT %s
                """,
                (int(limit),),
            ) or []
    except Exception:
        rows = []
    return [
        {
            "id": int(r["id"]),
            "no_banco": int(r["no_banco"]),
            "filename": r.get("filename") or "",
            "file_hash": (r.get("file_hash") or "")[:12],
            "n_filas": int(r.get("n_filas") or 0),
            "n_matches": int(r.get("n_matches") or 0),
            "fecha_min": r.get("fecha_min"),
            "fecha_max": r.get("fecha_max"),
            "usuario": r.get("usuario") or "",
            "creado_en": r.get("creado_en"),
        }
        for r in rows
    ]


def ultimos_extractos(no_banco: int | None = None, limit: int = 5) -> list[dict]:
    """Resumen de las últimas conciliaciones realizadas.

    Agrupa filas de banco_conciliacion_match por (no_banco, día) — proxy
    razonable para "extracto subido el dd/mm".
    """
    if no_banco is not None:
        rows = db.fetch_all(
            """
            SELECT no_banco,
                   DATE(creado_en) AS dia_proceso,
                   COUNT(*) AS n_movs,
                   MIN(real_fecha) AS desde_fecha,
                   MAX(real_fecha) AS hasta_fecha
              FROM scintela.banco_conciliacion_match
             WHERE no_banco = %s
             GROUP BY no_banco, DATE(creado_en)
             ORDER BY DATE(creado_en) DESC
             LIMIT %s
            """,
            (int(no_banco), int(limit)),
        ) or []
    else:
        rows = db.fetch_all(
            """
            SELECT no_banco,
                   DATE(creado_en) AS dia_proceso,
                   COUNT(*) AS n_movs,
                   MIN(real_fecha) AS desde_fecha,
                   MAX(real_fecha) AS hasta_fecha
              FROM scintela.banco_conciliacion_match
             GROUP BY no_banco, DATE(creado_en)
             ORDER BY DATE(creado_en) DESC
             LIMIT %s
            """,
            (int(limit),),
        ) or []
    return [
        {
            "no_banco": int(r["no_banco"]),
            "dia_proceso": r["dia_proceso"],
            "n_movs": int(r["n_movs"]),
            "desde_fecha": r["desde_fecha"],
            "hasta_fecha": r["hasta_fecha"],
        }
        for r in rows
    ]


def estado_actual_depositos(firmas: list[str]) -> dict[str, dict]:
    """Devuelve el último estado de cada firma_dep solicitada.

    Output: { firma_dep: {accion, id, creado_en, usuario, nota} } — solo
    incluye las firmas que SÍ tienen log. Las que nunca se marcaron quedan
    fuera y la UI las trata como "sin decisión".
    """
    if not firmas:
        return {}
    # ORDER BY firma_dep, creado_en DESC + DISTINCT ON → último por firma
    rows = (
        db.fetch_all(
            """
        SELECT DISTINCT ON (firma_dep)
               firma_dep, accion, id, creado_en, usuario, nota,
               id_transaccion
          FROM scintela.conciliacion_manual_log
         WHERE firma_dep = ANY(%s)
         ORDER BY firma_dep, creado_en DESC, id DESC
        """,
            (firmas,),
        )
        or []
    )
    out: dict[str, dict] = {}
    for r in rows:
        out[r["firma_dep"]] = {
            "accion": r["accion"],
            "id": int(r["id"]),
            "creado_en": r["creado_en"],
            "usuario": r["usuario"],
            "nota": r["nota"] or "",
            "id_transaccion": r["id_transaccion"],
        }
    return out
