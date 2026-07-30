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
    # TMT 2026-07-30 (dueña): las compras LOCALES de hilo (HY, EP) se cargan
    # solas al recibirlas y avisan acá. Nombre en castellano llano, sin jerga.
    "hilo-local": "Hilo local",
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
        # `execute_returning`, NO `fetch_one`: fetch_one no COMMITEA y psycopg2
        # le hace rollback a la transacción implícita al devolver la conexión
        # al pool. TMT 2026-07-30: por eso el buzón estaba VACÍO desde que se
        # estrenó — las dos importaciones AI que se cargaron solas quedaron en
        # el historial del automático y la campanita nunca dijo nada. Y como
        # avisar() es fail-soft, no había ni un error para mirar.
        row = db.execute_returning(
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
           fuente: str | None = None, nivel: str | None = None,
           archivados: bool = False) -> list[dict]:
    """Los avisos más nuevos primero, con `icono` y `cuando` ya resueltos.

    Por defecto NO trae los archivados (mig 0145): un aviso que ya no aplica se
    saca de la vista pero la fila queda, y `archivados=True` la vuelve a mostrar.
    """
    where, params = [], []
    where.append("archivado" if archivados else "NOT archivado")
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
                   url, leido, archivado,
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
            "SELECT COUNT(*) AS n FROM scintela.aviso "
            " WHERE NOT leido AND NOT archivado")
        return int((row or {}).get("n") or 0)
    except Exception:  # noqa: BLE001
        return 0


def archivar(id_aviso: int, usuario: str = "web", *, deshacer: bool = False) -> bool:
    """Saca (o devuelve) un aviso de la lista. Devuelve True si tocó una fila.

    TMT 2026-07-30 (dueña: *"sigo teniendo esto como notificación"*). Un aviso
    puede quedar OBSOLETO por el arreglo que él mismo provocó — los dos
    «Tejedor sin reconocer» de las 21:27 dejaron de ser ciertos a las 22:04,
    cuando el programa aprendió a leer el apellido mal tipeado. "Leído" no
    alcanzaba: leído es *lo vi*, archivado es *esto ya no es una novedad*.

    No borra: la fila queda y se puede deshacer.
    """
    try:
        db.execute(
            """
            UPDATE scintela.aviso
               SET archivado     = %s,
                   archivado_en  = CASE WHEN %s THEN NULL ELSE now() END,
                   archivado_por = CASE WHEN %s THEN NULL ELSE %s END
             WHERE id_aviso = %s
            """,
            (not deshacer, deshacer, deshacer, (usuario or "web")[:60],
             int(id_aviso)),
        )
        return True
    except Exception as e:  # noqa: BLE001 -- la campanita nunca rompe nada
        _LOG.warning("no pude archivar el aviso %s: %s", id_aviso, e)
        return False
