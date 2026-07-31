"""Lectura y escritura del buzón de novedades (`scintela.aviso`)."""
from __future__ import annotations

import logging
import time as _time

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

# ── Feature flag de la mig 0145 ──────────────────────────────────────────────
# El deploy NO corre migraciones: se aplican con un click en **/admin/migraciones**
# (NO por AWS/SSM — dueña 2026-07-30: *"las migraciones se corren de la pantalla
# migraciones. AWS no."*). Entre que sube el código y alguien entra a esa
# pantalla hay una ventana en la que `archivado` no existe, y como `listar` es
# fail-soft el buzón se veía VACÍO — peor que el problema que vino a resolver.
# Mismo patrón que `_tiene_orden_manual()`: se pregunta a information_schema y
# la query se arma con o sin la columna.
_TIENE_ARCHIVADO: bool | None = None
_TIENE_ARCHIVADO_TS: float = 0.0
#: El "todavía no está" se re-chequea cada minuto; el "sí está" no vence nunca
#: (una columna no se va). Misma lección que la caché de Metabase (29/07): NO
#: cachear el resultado negativo tanto como el positivo — si no, después de
#: aplicar la migración por /admin/migraciones la pantalla sigue vieja hasta
#: que alguien reinicie, y nadie entiende por qué.
_TTL_SIN_COLUMNA = 60.0


def _tiene_archivado() -> bool:
    global _TIENE_ARCHIVADO, _TIENE_ARCHIVADO_TS
    if _TIENE_ARCHIVADO:
        return True
    if _TIENE_ARCHIVADO is not None and (
            _time.monotonic() - _TIENE_ARCHIVADO_TS) < _TTL_SIN_COLUMNA:
        return False
    try:
        row = db.fetch_one(
            """
            SELECT 1 AS ok FROM information_schema.columns
             WHERE table_schema = 'scintela' AND table_name = 'aviso'
               AND column_name = 'archivado'
            """,
        )
        _TIENE_ARCHIVADO = bool(row)
        _TIENE_ARCHIVADO_TS = _time.monotonic()
    except Exception:  # noqa: BLE001 -- ante la duda, la versión vieja
        return False
    return bool(_TIENE_ARCHIVADO)


# ── Feature flag de la mig 0146: leído POR PERSONA ───────────────────────────
# Mismo mecanismo (y misma razón) que el de arriba: el deploy sube el código y
# la migración la aplica alguien a mano desde /admin/migraciones, así que hay
# una ventana en la que `scintela.aviso_leido` todavía no existe. Mientras
# tanto la campanita se comporta EXACTAMENTE como antes (leído global).
_TIENE_LEIDO_USR: bool | None = None
_TIENE_LEIDO_USR_TS: float = 0.0


def _tiene_leido_por_usuario() -> bool:
    global _TIENE_LEIDO_USR, _TIENE_LEIDO_USR_TS
    if _TIENE_LEIDO_USR:
        return True
    if _TIENE_LEIDO_USR is not None and (
            _time.monotonic() - _TIENE_LEIDO_USR_TS) < _TTL_SIN_COLUMNA:
        return False
    try:
        row = db.fetch_one(
            """
            SELECT 1 AS ok FROM information_schema.tables
             WHERE table_schema = 'scintela' AND table_name = 'aviso_leido'
            """,
        )
        _TIENE_LEIDO_USR = bool(row)
        _TIENE_LEIDO_USR_TS = _time.monotonic()
    except Exception:  # noqa: BLE001 -- ante la duda, la versión vieja
        return False
    return bool(_TIENE_LEIDO_USR)


def usuario_actual() -> str:
    """Quién está mirando. Vacío si no hay request (procesos de fondo).

    Se lee acá adentro y no se pasa por parámetro a propósito: `listar()` se
    llama desde el context processor de la campanita, desde /novedades y desde
    los tests, y hacer que cada llamador se acuerde de mandar el usuario es
    justo la clase de cosa que se olvida en una y deja el bug de vuelta.
    """
    try:
        from flask import g, has_request_context

        if not has_request_context():
            return ""
        return ((g.get("user") or {}).get("username") or "")[:60]
    except Exception:  # noqa: BLE001
        return ""


def reset_cache() -> None:
    """Olvida los flags — para después de aplicar la migración, sin reiniciar."""
    global _TIENE_ARCHIVADO, _TIENE_ARCHIVADO_TS
    global _TIENE_LEIDO_USR, _TIENE_LEIDO_USR_TS
    _TIENE_ARCHIVADO = None
    _TIENE_ARCHIVADO_TS = 0.0
    _TIENE_LEIDO_USR = None
    _TIENE_LEIDO_USR_TS = 0.0

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
    hay = _tiene_archivado()
    # mig 0146 — leído por PERSONA. Sin la tabla (o fuera de un request, p. ej.
    # un proceso de fondo), se cae al comportamiento viejo: leído global.
    usr = usuario_actual() if _tiene_leido_por_usuario() else ""
    where, params = [], []
    if usr:
        # El JOIN va primero en el SQL, así que su parámetro va primero.
        params.append(usr)
    if hay:
        where.append("archivado" if archivados else "NOT archivado")
    elif archivados:
        return []                      # sin la columna no hay archivados
    if solo_no_leidos:
        # `leido` (la columna vieja) = leído por TODOS, la línea de base de lo
        # que ya estaba leído antes de la 0146; `al.usuario` = lo leyó ESTA
        # persona. Que Tamara abra la campanita ya no apaga la de Federico.
        where.append("NOT leido AND al.usuario IS NULL" if usr else "NOT leido")
    if fuente:
        where.append("fuente = %s")
        params.append(fuente)
    if nivel:
        where.append("nivel = %s")
        params.append(nivel)
    params.append(int(limite))
    col_id = "aviso.id_aviso" if usr else "id_aviso"
    try:
        filas = db.fetch_all(
            f"""
            SELECT {col_id} AS id_aviso,
                   fuente, nivel, titulo, detalle, importe, cantidad,
                   url,
                   {"(leido OR al.usuario IS NOT NULL) AS leido" if usr else "leido"},
                   {"archivado" if hay else "FALSE AS archivado"},
                   -- La hora, en ECUADOR. El server corre en UTC (5 h
                   -- adelante): sin el AT TIME ZONE, el cierre de ventas de las
                   -- 18:00 se mostraba estampado 23:01 (verificado en vivo el
                   -- 30/07, primer aviso real del buzón). Mismo criterio que
                   -- today_ec() en el resto del programa.
                   TO_CHAR(creado_en AT TIME ZONE 'America/Guayaquil',
                           'DD/MM HH24:MI') AS cuando,
                   TO_CHAR(creado_en AT TIME ZONE 'America/Guayaquil',
                           'YYYY-MM-DD HH24:MI') AS creado_en
              FROM scintela.aviso
             {'''LEFT JOIN scintela.aviso_leido al
                         ON al.id_aviso = aviso.id_aviso
                        AND al.usuario = %s''' if usr else ""}
             {("WHERE " + " AND ".join(where)) if where else ""}
             ORDER BY creado_en DESC, {col_id} DESC
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
    """Marca como leído — para QUIEN lo está mirando, no para todos (mig 0146).

    TMT 2026-07-31 (dueña): *"si yo la leo igual le tienen que aparecer no
    leídas a federico andres y alex"*. Antes esto era un UPDATE de la fila y el
    primero que abría la campanita se la apagaba al resto.
    """
    usr = usuario_actual() if _tiene_leido_por_usuario() else ""
    try:
        if usr:
            # Una fila por (aviso, persona). Re-abrir la campanita no duplica
            # nada ni pisa la fecha de la primera vez que la vio.
            db.execute(
                f"""
                INSERT INTO scintela.aviso_leido (id_aviso, usuario)
                SELECT id_aviso, %s FROM scintela.aviso
                 WHERE NOT leido
                       {" AND fuente = %s" if fuente else ""}
                       {" AND NOT archivado" if _tiene_archivado() else ""}
                ON CONFLICT (id_aviso, usuario) DO NOTHING
                """,
                (usr, fuente) if fuente else (usr,),
            )
        elif fuente:
            db.execute(
                "UPDATE scintela.aviso SET leido = TRUE "
                " WHERE NOT leido AND fuente = %s", (fuente,))
        else:
            db.execute("UPDATE scintela.aviso SET leido = TRUE WHERE NOT leido")
        return 1
    except Exception:  # noqa: BLE001
        return 0


def n_no_leidos() -> int:
    usr = usuario_actual() if _tiene_leido_por_usuario() else ""
    try:
        if usr:
            row = db.fetch_one(
                """
                SELECT COUNT(*) AS n
                  FROM scintela.aviso
                  LEFT JOIN scintela.aviso_leido al
                         ON al.id_aviso = aviso.id_aviso AND al.usuario = %s
                 WHERE NOT leido AND al.usuario IS NULL
                """
                + (" AND NOT archivado" if _tiene_archivado() else ""),
                (usr,))
        else:
            row = db.fetch_one(
                "SELECT COUNT(*) AS n FROM scintela.aviso WHERE NOT leido"
                + (" AND NOT archivado" if _tiene_archivado() else ""))
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
    if not _tiene_archivado():
        _LOG.warning("archivar: falta la migracion 0145")
        return False
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
