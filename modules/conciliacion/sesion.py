"""Sesión persistente de conciliación bancaria.

TMT 2026-05-28 dueña: 'puede quedar abierta esa pagina hasta no cerrar la
conciliacion?'. Sí — guardamos el extracto parseado + flag abierta/cerrada
en `scintela.banco_conciliacion_sesion` (migration 0060). Cada vez que la
pantalla post-procesar carga, lee la sesión abierta del usuario, re-corre
el matcher contra los movs no conciliados todavía, y arma 3 buckets:

    - manual:        real_only y bancsis_only para checkbox 1:1 manual.
    - impuestos:     real_only categorizados como COMISION (auto-acoplar).
    - transferencias: matches con razón 'P0' (doc-id exacto).

Además expone:
    - sha256_bytes(b) → hash para detectar re-uploads del mismo archivo.
    - mov_to_dict / dict_to_mov → serialización JSON del payload.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

import db
from modules.conciliacion.matcher_banco import (
    ConciliacionBanco,
    matchear_extracto_banco,
)
from modules.conciliacion.parser_banco import MovBanco

_LOG = logging.getLogger("programa_core.conciliacion.sesion")


# ─── Serialización MovBanco ↔ JSON ────────────────────────────────────


def _mov_to_dict(m: MovBanco) -> dict:
    return {
        "fecha": m.fecha.isoformat() if m.fecha else None,
        "concepto": m.concepto or "",
        "documento": m.documento or "",
        "monto": str(m.monto) if m.monto is not None else "0",
        "saldo": str(m.saldo) if m.saldo is not None else "0",
        "codigo": m.codigo or "",
        "tipo": m.tipo or "",
        "oficina": m.oficina or "",
    }


def _dict_to_mov(d: dict) -> MovBanco:
    return MovBanco(
        fecha=date.fromisoformat(d["fecha"]) if d.get("fecha") else None,
        concepto=d.get("concepto", ""),
        documento=d.get("documento", ""),
        monto=Decimal(d.get("monto") or "0"),
        saldo=Decimal(d.get("saldo") or "0"),
        codigo=d.get("codigo", ""),
        tipo=d.get("tipo", ""),
        oficina=d.get("oficina", ""),
    )


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ─── Detección de migration 0060 ──────────────────────────────────────


def tabla_existe() -> bool:
    """¿Corrió la migration 0060? Cacheado por proceso."""
    if hasattr(tabla_existe, "_cache"):
        return tabla_existe._cache
    try:
        row = db.fetch_one(
            """
            SELECT 1
              FROM information_schema.tables
             WHERE table_schema='scintela'
               AND table_name='banco_conciliacion_sesion'
            """
        )
        tabla_existe._cache = bool(row)
    except Exception:
        tabla_existe._cache = False
    return tabla_existe._cache


# ─── CRUD básico de sesión ────────────────────────────────────────────


def sesion_abierta(no_banco: int, usuario: str) -> dict | None:
    """La sesión abierta del par (no_banco, usuario), o None."""
    return db.fetch_one(
        """
        SELECT id, no_banco, usuario, abierta_en, cerrada_en, cerrada_por,
               extracto_hash, extracto_nombre, extracto_payload,
               matches_hechos, pdf_path
          FROM scintela.banco_conciliacion_sesion
         WHERE no_banco = %s AND usuario = %s AND cerrada_en IS NULL
         ORDER BY abierta_en DESC
         LIMIT 1
        """,
        (int(no_banco), usuario[:50]),
    )


def sesion_por_id(sesion_id: int) -> dict | None:
    return db.fetch_one(
        """
        SELECT id, no_banco, usuario, abierta_en, cerrada_en, cerrada_por,
               extracto_hash, extracto_nombre, extracto_payload,
               matches_hechos, pdf_path
          FROM scintela.banco_conciliacion_sesion
         WHERE id = %s
        """,
        (int(sesion_id),),
    )


def matches_de_sesion(sesion: dict) -> list[dict]:
    """Lista los matches confirmados durante esta sesión.

    Aproximamos por ventana temporal: banco_conciliacion_match.creado_en
    entre sesion.abierta_en y sesion.cerrada_en (o NOW si está abierta).
    No hay FK directa sesion_id → match (la tabla 0046 es anterior a 0060).

    Devuelve filas listas para renderizar: real + bancsis lado a lado.
    """
    if not sesion:
        return []
    no_banco = int(sesion.get("no_banco") or 0)
    abierta = sesion.get("abierta_en")
    cerrada = sesion.get("cerrada_en")
    if not no_banco or not abierta:
        return []
    filtro_undo = ""
    try:
        # Si migration 0047 corrió, tenemos columna deshecho_en.
        from modules.conciliacion.matcher_banco import _tiene_migration_47
        if _tiene_migration_47():
            filtro_undo = "AND m.deshecho_en IS NULL"
    except Exception:
        pass
    sql = f"""
        SELECT m.id, m.estado, m.creado_en, m.usuario,
               m.real_fecha, m.real_documento, m.real_monto, m.real_tipo, m.real_concepto,
               m.id_transaccion,
               tb.fecha       AS tb_fecha,
               tb.documento   AS tb_documento,
               tb.importe     AS tb_importe,
               tb.numreferencia AS tb_numreferencia,
               tb.concepto    AS tb_concepto,
               tb.prov        AS tb_prov
          FROM scintela.banco_conciliacion_match m
          LEFT JOIN scintela.transacciones_bancarias tb
            ON tb.id_transaccion = m.id_transaccion
         WHERE m.no_banco = %s
           AND m.creado_en >= %s
           AND m.creado_en <= COALESCE(%s, CURRENT_TIMESTAMP)
           {filtro_undo}
         ORDER BY m.creado_en DESC
         LIMIT 1000
    """
    try:
        rows = db.fetch_all(sql, (no_banco, abierta, cerrada)) or []
    except Exception as e:
        _LOG.warning("matches_de_sesion falló: %s", e)
        return []
    return [dict(r) for r in rows]


def sesion_por_hash(no_banco: int, extracto_hash: str) -> dict | None:
    """Busca cualquier sesión (abierta o cerrada) con el MISMO hash del
    extracto para el mismo banco. Usado para detectar re-uploads del mismo
    archivo y evitar duplicar el trabajo. TMT 2026-05-29 pedido dueña:
    'si vuelvo a subir el mismo archivo no se tiene que duplicar'.

    Devuelve la sesión más reciente que coincide, o None.
    """
    if not extracto_hash:
        return None
    try:
        return db.fetch_one(
            """
            SELECT id, no_banco, usuario, abierta_en, cerrada_en, cerrada_por,
                   extracto_hash, extracto_nombre, matches_hechos
              FROM scintela.banco_conciliacion_sesion
             WHERE no_banco = %s
               AND extracto_hash = %s
             ORDER BY abierta_en DESC
             LIMIT 1
            """,
            (int(no_banco), extracto_hash),
        )
    except Exception:
        return None


def crear_sesion(
    no_banco: int,
    usuario: str,
    movs: Iterable[MovBanco],
    *,
    extracto_hash: str | None = None,
    extracto_nombre: str | None = None,
) -> int:
    """Crea una sesión NUEVA con el extracto parseado.

    Si ya hay una abierta para el par (no_banco, usuario), la cierra primero
    como 'abandonada' (cerrada_por='auto-replaced') para respetar el unique
    index parcial.
    """
    movs = list(movs)
    payload = json.dumps([_mov_to_dict(m) for m in movs])

    with db.tx() as conn:
        # Si ya hay una abierta, cerrarla automáticamente. La dueña empieza
        # una conciliación nueva y la vieja queda como abandonada.
        db.execute(
            """
            UPDATE scintela.banco_conciliacion_sesion
               SET cerrada_en = CURRENT_TIMESTAMP,
                   cerrada_por = 'auto-replaced'
             WHERE no_banco = %s AND usuario = %s AND cerrada_en IS NULL
            """,
            (int(no_banco), usuario[:50]),
            conn=conn,
        )
        row = db.execute_returning(
            """
            INSERT INTO scintela.banco_conciliacion_sesion
                (no_banco, usuario, extracto_hash, extracto_nombre, extracto_payload)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (int(no_banco), usuario[:50], extracto_hash, (extracto_nombre or "")[:200], payload),
            conn=conn,
        )
        sid = int(row["id"]) if row else 0
    # Snapshot del balance inicial al abrir la sesión (FUERA del db.tx).
    # Capturado como evento_tipo='sesion_abierta', evento_ref=<sesion_id>.
    try:
        from modules.conciliacion import saldo_snapshot as _ss
        _ss.snapshot(
            no_banco=int(no_banco),
            evento_tipo="sesion_abierta",
            evento_ref=str(sid),
            usuario=usuario,
            descripcion=f"apertura sesión #{sid}",
        )
    except Exception as e:
        _LOG.warning("snapshot apertura sesión #%s falló: %s", sid, e)
    return sid


def cerrar_sesion(sesion_id: int, usuario: str, pdf_path: str | None = None) -> bool:
    n = db.execute(
        """
        UPDATE scintela.banco_conciliacion_sesion
           SET cerrada_en = CURRENT_TIMESTAMP,
               cerrada_por = %s,
               pdf_path = COALESCE(%s, pdf_path)
         WHERE id = %s
           AND cerrada_en IS NULL
        """,
        (usuario[:50], pdf_path, int(sesion_id)),
    )
    return bool(n)


def incrementar_matches(sesion_id: int, n: int = 1) -> None:
    db.execute(
        """
        UPDATE scintela.banco_conciliacion_sesion
           SET matches_hechos = matches_hechos + %s
         WHERE id = %s
        """,
        (int(n), int(sesion_id)),
    )


def listar_sesiones(no_banco: int | None = None, limit: int = 100) -> list[dict]:
    if no_banco:
        return db.fetch_all(
            """
            SELECT id, no_banco, usuario, abierta_en, cerrada_en, cerrada_por,
                   extracto_nombre, matches_hechos, pdf_path
              FROM scintela.banco_conciliacion_sesion
             WHERE no_banco = %s
             ORDER BY COALESCE(cerrada_en, abierta_en) DESC
             LIMIT %s
            """,
            (int(no_banco), int(limit)),
        ) or []
    return db.fetch_all(
        """
        SELECT id, no_banco, usuario, abierta_en, cerrada_en, cerrada_por,
               extracto_nombre, matches_hechos, pdf_path
          FROM scintela.banco_conciliacion_sesion
         ORDER BY COALESCE(cerrada_en, abierta_en) DESC
         LIMIT %s
        """,
        (int(limit),),
    ) or []


# ─── Recuperar payload y re-correr matcher ────────────────────────────


def cargar_movs(sesion: dict) -> list[MovBanco]:
    """De la fila DB → lista MovBanco."""
    payload = sesion.get("extracto_payload") or []
    # psycopg2 RealDictCursor devuelve jsonb como str o list dependiendo del driver.
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    return [_dict_to_mov(d) for d in payload]


# ─── Bucketización del resultado del matcher ──────────────────────────


def _es_comision(cat) -> bool:
    """True si la categoría es COMISION (impuestos bancarios, comisiones, SENAE)."""
    if not cat:
        return False
    return (getattr(cat, "grupo", "") or "").upper() == "COMISION"


def bucketizar(res: ConciliacionBanco) -> dict:
    """Toma el resultado del matcher y lo divide en 3 buckets para los 3 tabs.

    Returns:
        {
          'manual_banco': list[(MovBanco, Categorizado, idx)],   # real_only NO comision
          'manual_programa': list[(MovBancsis, Categorizado, idx)],  # bancsis_only todos
          'impuestos': list[(MovBanco, Categorizado, idx)],      # real_only COMISION
          'transferencias': list[Match],                          # matches que vinieron por doc (P0)
          'sugerencias': list[Match],                             # resto de matches (P1-P4)
        }

    Nota: PASS 0 hoy NO se persiste en Match.razon, pero el matcher devuelve
    razon='Doc-ID exacto (PASS 0)'. Detectamos por substring 'PASS 0' en razon.
    """
    real_only = res.real_only or []
    bancsis_only = res.bancsis_only or []
    real_cats = res.real_only_cats or [None] * len(real_only)
    bancsis_cats = res.bancsis_only_cats or [None] * len(bancsis_only)

    manual_banco = []
    impuestos = []
    for i, mov in enumerate(real_only):
        cat = real_cats[i] if i < len(real_cats) else None
        bucket = impuestos if _es_comision(cat) else manual_banco
        bucket.append({"mov": mov, "cat": cat, "idx": i})

    manual_programa = []
    for i, mov in enumerate(bancsis_only):
        cat = bancsis_cats[i] if i < len(bancsis_cats) else None
        manual_programa.append({"mov": mov, "cat": cat, "idx": i})

    # Orden: mayor a menor por monto (la dueña pidió de mayor a menor).
    manual_banco.sort(key=lambda x: float(x["mov"].monto or 0), reverse=True)
    manual_programa.sort(key=lambda x: abs(float(x["mov"].importe or 0)), reverse=True)
    impuestos.sort(key=lambda x: float(x["mov"].monto or 0), reverse=True)

    transferencias = []
    sugerencias = []
    for m in (res.matches or []):
        razon = (m.razon or "").upper()
        if "PASS 0" in razon or "P0" in razon or "DOC-ID" in razon or "DOC ID" in razon:
            transferencias.append(m)
        else:
            sugerencias.append(m)

    return {
        "manual_banco": manual_banco,
        "manual_programa": manual_programa,
        "impuestos": impuestos,
        "transferencias": transferencias,
        "sugerencias": sugerencias,
    }


# ─── Helper top-level: cargar sesión + buckets ────────────────────────


def _cargar_historicos_pendientes(no_banco: int) -> list[dict]:
    """Históricos del banco que quedaron sin conciliar — se mezclan en el
    panel Banco del tab Manual para que la dueña los vea junto con los
    movs del extracto actual.

    TMT 2026-05-29 dueña: 'no me estan apareciendo los historicos sin
    conciliar'. Antes solo veía real_only del matcher (los del extracto
    de la sesión actual). Los históricos viven en
    scintela.banco_historicos_pendientes con conciliado_en IS NULL.
    """
    try:
        rows = db.fetch_all(
            """
            SELECT id, fecha, documento, concepto, monto, tipo, oficina, detalle
              FROM scintela.banco_historicos_pendientes
             WHERE no_banco = %s
               AND conciliado_en IS NULL
             ORDER BY ABS(monto) DESC
             LIMIT 500
            """,
            (int(no_banco),),
        ) or []
        return rows
    except Exception as e:
        _LOG.warning("_cargar_historicos_pendientes falló: %s", e)
        return []


def estado_sesion(sesion: dict, no_banco: int) -> dict:
    """De la sesión DB → buckets listos para renderizar.

    Re-corre el matcher cada vez (porque entre la apertura de la sesión y
    ahora puede haberse conciliado algo desde otra pestaña, o haber llegado
    movs PC nuevos). El matcher ya excluye los `id_transaccion` con match
    activo en `banco_conciliacion_match`.
    """
    movs = cargar_movs(sesion)
    historicos = _cargar_historicos_pendientes(no_banco)
    if not movs:
        # Sin extracto en sesión, pero puede haber históricos para conciliar.
        manual_banco_hist = [
            {"mov": _hist_to_mov_like(h), "cat": None, "idx": -1,
             "es_historico": True, "id_historico": int(h["id"])}
            for h in historicos
        ]
        return {
            "manual_banco": manual_banco_hist, "manual_programa": [],
            "impuestos": [], "transferencias": [], "sugerencias": [],
            "matcher_extracto_desde": None, "matcher_extracto_hasta": None,
        }
    try:
        res = matchear_extracto_banco(movs, no_banco=no_banco)
    except Exception as e:
        _LOG.warning("matchear_extracto_banco falló: %s", e)
        return {
            "manual_banco": [], "manual_programa": [],
            "impuestos": [], "transferencias": [], "sugerencias": [],
            "matcher_extracto_desde": None, "matcher_extracto_hasta": None,
        }
    buckets = bucketizar(res)
    # Mezclar los históricos al inicio del panel Banco (más viejos arriba).
    hist_items = [
        {"mov": _hist_to_mov_like(h), "cat": None, "idx": -1,
         "es_historico": True, "id_historico": int(h["id"])}
        for h in historicos
    ]
    buckets["manual_banco"] = hist_items + (buckets.get("manual_banco") or [])
    buckets["matcher_extracto_desde"] = res.extracto_desde
    buckets["matcher_extracto_hasta"] = res.extracto_hasta
    buckets["n_historicos_pendientes"] = len(historicos)
    return buckets


def _hist_to_mov_like(h: dict):
    """Wrap un row de banco_historicos_pendientes en algo que el template
    puede tratar como mov banco. Atributos esperados por el template:
    fecha, concepto, documento, monto, tipo.
    """
    import types
    m = types.SimpleNamespace()
    m.fecha = h.get("fecha")
    m.concepto = h.get("concepto") or ""
    m.documento = h.get("documento") or ""
    try:
        m.monto = float(h.get("monto") or 0)
    except (TypeError, ValueError):
        m.monto = 0
    m.tipo = (h.get("tipo") or "C").upper()
    return m
