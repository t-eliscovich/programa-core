"""Lectura y escritura del buzón de novedades (`scintela.aviso`)."""
from __future__ import annotations

import logging

import db

_LOG = logging.getLogger("programa_core.avisos")

# Nombre lindo de cada fuente — es lo que se ve en la pantalla y en el filtro.
FUENTES = {
    "ventas": "Ventas",
    "tejeduria": "Tejeduría",
    "quimicos": "Químicos",
    "importaciones": "Importaciones",
}

NIVELES = ("ok", "alerta", "error")

ICONOS = {"ok": "✅", "alerta": "⚠️", "error": "⛔"}


def avisar(*, fuente: str, titulo: str, detalle: str | None = None,
           nivel: str = "ok", importe=None, cantidad: int | None = None,
           url: str | None = None, clave: str | None = None) -> bool:
    """Deja un aviso. Devuelve True si entró, False si ya estaba o falló.

    `clave` hace el aviso idempotente: los procesos de fondo reintentan lo mismo
    cada N minutos y sin clave el buzón se llenaría de repetidos.
    """
    if nivel not in NIVELES:
        nivel = "ok"
    try:
        row = db.fetch_one(
            """
            INSERT INTO scintela.aviso
                   (fuente, nivel, titulo, detalle, importe, cantidad, url, clave)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (clave) DO NOTHING
            RETURNING id_aviso
            """,
            ((fuente or "")[:40], nivel, (titulo or "")[:200],
             (detalle or None), importe, cantidad,
             (url or None), (clave or None)),
        )
        return bool(row)
    except Exception as e:  # noqa: BLE001 -- avisar nunca rompe al que avisa
        _LOG.warning("no pude dejar el aviso (%s / %s): %s", fuente, titulo, e)
        return False


def listar(*, solo_no_leidos: bool = True, limite: int = 30,
           fuente: str | None = None, nivel: str | None = None) -> list[dict]:
    """Los avisos más nuevos primero, con `icono` y `cuando` ya resueltos."""
    where, params = [], []
    if solo_no_leidos:
        where.append("NOT leido")
    if fuente:
        where.append("fuente = %s")
        params.append(fuente)
    if nivel:
        where.append("nivel = %s")
        params.append(nivel)
    params.append(int(limite))
    try:
        filas = db.fetch_all(
            f"""
            SELECT id_aviso, fuente, nivel, titulo, detalle, importe, cantidad,
                   url, leido,
                   TO_CHAR(creado_en, 'DD/MM HH24:MI') AS cuando,
                   TO_CHAR(creado_en, 'YYYY-MM-DD HH24:MI') AS creado_en
              FROM scintela.aviso
             {("WHERE " + " AND ".join(where)) if where else ""}
             ORDER BY creado_en DESC, id_aviso DESC
             LIMIT %s
            """,
            tuple(params),
        ) or []
    except Exception:  # noqa: BLE001 -- la campanita nunca rompe una pantalla
        return []
    for f in filas:
        f["icono"] = ICONOS.get(f.get("nivel"), "•")
        f["fuente_label"] = FUENTES.get(f.get("fuente"), f.get("fuente") or "")
    return filas


def marcar_leidos(fuente: str | None = None) -> int:
    try:
        if fuente:
            db.execute(
                "UPDATE scintela.aviso SET leido = TRUE "
                " WHERE NOT leido AND fuente = %s", (fuente,))
        else:
            db.execute("UPDATE scintela.aviso SET leido = TRUE WHERE NOT leido")
        return 1
    except Exception:  # noqa: BLE001
        return 0


def n_no_leidos() -> int:
    try:
        row = db.fetch_one(
            "SELECT COUNT(*) AS n FROM scintela.aviso WHERE NOT leido")
        return int((row or {}).get("n") or 0)
    except Exception:  # noqa: BLE001
        return 0
