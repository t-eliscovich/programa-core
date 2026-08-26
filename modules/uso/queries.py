"""Las consultas de la pantalla de uso.

Dos fuentes, y se leen juntas:

* `scintela.uso_pantalla` — lo que el vendedor MIRA (migración 0232).
* `scintela.bitacora_acciones` — lo que el vendedor CAMBIA (migración 0004).

Fechas
------
Las dos columnas de tiempo guardan cosas distintas y hay que tratarlas
distinto, o los días salen corridos cinco horas (el servidor corre en UTC y
Ecuador está en UTC−5, ver `filters.today_ec`):

* `uso_pantalla.ts` es `timestamptz`: `AT TIME ZONE 'America/Guayaquil'` la
  pasa a hora de Ecuador y listo.
* `bitacora_acciones.ts` es `timestamp` a secas, escrito por
  `CURRENT_TIMESTAMP`, o sea en la zona con la que corre la sesión de
  Postgres. Por eso primero se la interpreta en `current_setting('TimeZone')`
  —la misma con la que se escribió, sea cual sea— y recién ahí se la pasa a
  Ecuador. Adivinar que es UTC funcionaría hoy y se rompería el día que
  alguien toque el parámetro del servidor.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import db

from .registro import PAPELES

#: Cuánto silencio separa dos usos. Más de esto y contamos que "volvió a
#: entrar": el vendedor abre la app, mira tres pantallas y la cierra.
CORTE_ENTRADA = "30 minutes"

#: Hora de Ecuador, como la escribe Postgres.
EC = "America/Guayaquil"

#: La visita, en hora de Ecuador.
_TS_USO = f"(ts AT TIME ZONE '{EC}')"

#: El movimiento de la bitácora, en hora de Ecuador (ver el docstring).
_TS_BITA = f"((ts AT TIME ZONE current_setting('TimeZone')) AT TIME ZONE '{EC}')"

#: El mismo valor pero como timestamptz, para comparar contra el rango.
_TSTZ_BITA = "(ts AT TIME ZONE current_setting('TimeZone'))"


def ventana(desde: date, hasta: date) -> dict:
    """Los dos bordes del rango, en UTC y sin zona, listos para la consulta.

    `desde` y `hasta` son días de Ecuador y los dos entran: de las 00:00 del
    primero a las 24:00 del último.
    """
    return {
        "desde": datetime.combine(desde, time.min) + timedelta(hours=5),
        "hasta": datetime.combine(hasta + timedelta(days=1), time.min) + timedelta(hours=5),
    }


def resumen(desde: date, hasta: date) -> list[dict]:
    """Una fila por vendedor: cuánto entró, qué miró y cuánto cambió.

    Salen TODOS los vendedores, también los que no abrieron la app en el
    rango — que es justamente lo que se quiere ver.
    """
    params = ventana(desde, hasta)
    params["papeles"] = sorted(PAPELES)
    return db.fetch_all(
        f"""
        WITH visitas AS (
            SELECT usuario, ts, codigo_cli, pantalla, dispositivo,
                   lag(ts) OVER (PARTITION BY usuario ORDER BY ts) AS anterior
              FROM scintela.uso_pantalla
             WHERE ts >= (%(desde)s AT TIME ZONE 'UTC')
               AND ts <  (%(hasta)s AT TIME ZONE 'UTC')
        ),
        uso AS (
            SELECT usuario,
                   count(*)                                   AS visitas,
                   count(DISTINCT {_TS_USO}::date)            AS dias,
                   count(*) FILTER (
                       WHERE anterior IS NULL
                          OR ts - anterior > INTERVAL '{CORTE_ENTRADA}')  AS entradas,
                   count(DISTINCT codigo_cli)                 AS clientes,
                   count(*) FILTER (WHERE pantalla = ANY(%(papeles)s))    AS papeles,
                   count(*) FILTER (WHERE dispositivo = 'celular')        AS celular,
                   max({_TS_USO})                             AS ultima
              FROM visitas
             GROUP BY usuario
        ),
        movs AS (
            SELECT usuario, count(*) AS movimientos
              FROM scintela.bitacora_acciones
             -- El primer corte es sobre la columna cruda para que entre por
             -- el índice; el margen de un día lo cubre cualquier zona.
             WHERE ts >= %(desde)s - INTERVAL '1 day'
               AND {_TSTZ_BITA} >= (%(desde)s AT TIME ZONE 'UTC')
               AND {_TSTZ_BITA} <  (%(hasta)s AT TIME ZONE 'UTC')
             GROUP BY usuario
        )
        SELECT u.username            AS usuario,
               COALESCE(u.vend, '')  AS vend,
               r.nombre_rol          AS rol,
               u.activo,
               COALESCE(x.visitas, 0)     AS visitas,
               COALESCE(x.dias, 0)        AS dias,
               COALESCE(x.entradas, 0)    AS entradas,
               COALESCE(x.clientes, 0)    AS clientes,
               COALESCE(x.papeles, 0)     AS papeles,
               COALESCE(x.celular, 0)     AS celular,
               COALESCE(m.movimientos, 0) AS movimientos,
               x.ultima
          FROM seguridad.usuario u
          JOIN seguridad.rol r USING (id_rol)
          LEFT JOIN uso  x ON x.usuario = u.username
          LEFT JOIN movs m ON m.usuario = u.username
         WHERE u.vend IS NOT NULL AND TRIM(u.vend) <> ''
         ORDER BY u.activo DESC, COALESCE(x.visitas, 0) DESC, u.username
        """,
        params,
    )


def pantallas(desde: date, hasta: date, usuario: str | None = None) -> list[dict]:
    """Qué pantallas se abrieron y cuántas veces, de la más usada a la menos."""
    params = ventana(desde, hasta)
    params["usuario"] = usuario or None
    return db.fetch_all(
        """
        SELECT pantalla,
               count(*)                  AS visitas,
               count(DISTINCT usuario)   AS usuarios
          FROM scintela.uso_pantalla
         WHERE ts >= (%(desde)s AT TIME ZONE 'UTC')
           AND ts <  (%(hasta)s AT TIME ZONE 'UTC')
           AND (%(usuario)s IS NULL OR usuario = %(usuario)s)
         GROUP BY pantalla
         ORDER BY visitas DESC, pantalla
        """,
        params,
    )


def por_dia(usuario: str, desde: date, hasta: date) -> list[dict]:
    """Día por día de un vendedor: a qué hora abrió, cuánto miró, qué imprimió."""
    params = ventana(desde, hasta)
    params["usuario"] = usuario
    params["papeles"] = sorted(PAPELES)
    return db.fetch_all(
        f"""
        SELECT {_TS_USO}::date                AS dia,
               count(*)                       AS visitas,
               count(DISTINCT codigo_cli)     AS clientes,
               count(*) FILTER (WHERE pantalla = ANY(%(papeles)s)) AS papeles,
               min({_TS_USO})                 AS primera,
               max({_TS_USO})                 AS ultima
          FROM scintela.uso_pantalla
         WHERE usuario = %(usuario)s
           AND ts >= (%(desde)s AT TIME ZONE 'UTC')
           AND ts <  (%(hasta)s AT TIME ZONE 'UTC')
         GROUP BY 1
         ORDER BY 1 DESC
        """,
        params,
    )


def clientes(usuario: str, desde: date, hasta: date, limite: int = 50) -> list[dict]:
    """A qué clientes les abrió la ficha, y cuántas veces."""
    params = ventana(desde, hasta)
    params.update({"usuario": usuario, "limite": limite})
    return db.fetch_all(
        f"""
        SELECT u.codigo_cli,
               COALESCE(c.nombre, '')  AS nombre,
               count(*)                AS veces,
               max({_TS_USO})          AS ultima
          FROM scintela.uso_pantalla u
          LEFT JOIN scintela.cliente c
                 ON UPPER(TRIM(c.codigo_cli)) = u.codigo_cli
         WHERE u.usuario = %(usuario)s
           AND u.codigo_cli IS NOT NULL
           AND u.ts >= (%(desde)s AT TIME ZONE 'UTC')
           AND u.ts <  (%(hasta)s AT TIME ZONE 'UTC')
         GROUP BY u.codigo_cli, c.nombre
         ORDER BY veces DESC, u.codigo_cli
         LIMIT %(limite)s
        """,
        params,
    )


def movimientos(usuario: str, desde: date, hasta: date, limite: int = 300) -> list[dict]:
    """Todo lo que hizo, mezclado: lo que miró y lo que cambió.

    `tipo` vale 'miro' o 'hizo'. La pantalla los dibuja distinto.
    """
    params = ventana(desde, hasta)
    params.update({"usuario": usuario, "limite": limite})
    return db.fetch_all(
        f"""
        SELECT * FROM (
            SELECT {_TS_USO}     AS cuando,
                   'miro'::text  AS tipo,
                   pantalla,
                   ruta,
                   codigo_cli,
                   NULL::text    AS detalle
              FROM scintela.uso_pantalla
             WHERE usuario = %(usuario)s
               AND ts >= (%(desde)s AT TIME ZONE 'UTC')
               AND ts <  (%(hasta)s AT TIME ZONE 'UTC')
            UNION ALL
            SELECT {_TS_BITA}    AS cuando,
                   'hizo'::text  AS tipo,
                   accion        AS pantalla,
                   ruta,
                   NULL::varchar AS codigo_cli,
                   COALESCE(NULLIF(resumen, ''),
                            NULLIF(CONCAT_WS(' ', entidad, id_entidad), '')) AS detalle
              FROM scintela.bitacora_acciones
             WHERE usuario = %(usuario)s
               AND ts >= %(desde)s - INTERVAL '1 day'
               AND {_TSTZ_BITA} >= (%(desde)s AT TIME ZONE 'UTC')
               AND {_TSTZ_BITA} <  (%(hasta)s AT TIME ZONE 'UTC')
        ) todo
         ORDER BY cuando DESC
         LIMIT %(limite)s
        """,
        params,
    )
