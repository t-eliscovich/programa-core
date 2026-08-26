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
            SELECT u.usuario, u.ts, u.codigo_cli, u.pantalla, u.dispositivo,
                   lag(u.ts) OVER (PARTITION BY u.usuario ORDER BY u.ts) AS anterior,
                   -- ⭐ Sólo cuentan como «clientes» los que HOY son de su
                   -- cartera. Así «abrió 12 de 43» y la lista de «los que no
                   -- abrió» son las dos mitades del mismo 43: si contáramos
                   -- todas las fichas que tocó, un cliente que le
                   -- reasignaron a otro vendedor a mitad de mes dejaría un
                   -- «13 de 43» que no cierra con nada.
                   (UPPER(TRIM(COALESCE(c.vend, ''))) = UPPER(TRIM(COALESCE(u.vend, ''))))
                       AS de_su_cartera
              FROM scintela.uso_pantalla u
              LEFT JOIN scintela.cliente c
                     ON UPPER(TRIM(c.codigo_cli)) = u.codigo_cli
             WHERE u.ts >= (%(desde)s AT TIME ZONE 'UTC')
               AND u.ts <  (%(hasta)s AT TIME ZONE 'UTC')
        ),
        uso AS (
            SELECT usuario,
                   count(*)                                   AS visitas,
                   count(DISTINCT {_TS_USO}::date)            AS dias,
                   count(*) FILTER (
                       WHERE anterior IS NULL
                          OR ts - anterior > INTERVAL '{CORTE_ENTRADA}')  AS entradas,
                   count(DISTINCT codigo_cli)
                       FILTER (WHERE de_su_cartera)           AS clientes,
                   count(*) FILTER (WHERE pantalla = ANY(%(papeles)s))    AS papeles,
                   count(*) FILTER (WHERE dispositivo = 'celular')        AS celular,
                   max({_TS_USO})                             AS ultima
              FROM visitas
             GROUP BY usuario
        ),
        cartera AS (
            -- Cuántos clientes tiene asignados cada vendedor. Sin esto,
            -- «abrió 12 clientes» no se puede leer: 12 sobre 40 y 12 sobre 15
            -- son dos cosas distintas.
            SELECT UPPER(TRIM(c.vend)) AS vend, count(*) AS clientes
              FROM scintela.cliente c
             WHERE c.vend IS NOT NULL AND TRIM(c.vend) <> ''
             GROUP BY 1
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
               COALESCE(k.clientes, 0)    AS cartera,
               COALESCE(x.papeles, 0)     AS papeles,
               COALESCE(x.celular, 0)     AS celular,
               COALESCE(m.movimientos, 0) AS movimientos,
               x.ultima
          FROM seguridad.usuario u
          JOIN seguridad.rol r USING (id_rol)
          LEFT JOIN uso  x ON x.usuario = u.username
          LEFT JOIN movs m ON m.usuario = u.username
          LEFT JOIN cartera k ON k.vend = UPPER(TRIM(u.vend))
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


def vend_de(usuario: str) -> str:
    """El código de vendedor del usuario. '' si no es vendedor o no existe."""
    fila = db.fetch_one(
        "SELECT UPPER(TRIM(COALESCE(vend, ''))) AS vend "
        "  FROM seguridad.usuario WHERE username = %s", (usuario,))
    return (fila or {}).get("vend") or ""


def no_abiertos(vend: str, usuario: str, desde: date, hasta: date) -> list[dict]:
    """Los clientes de su cartera cuya ficha NO abrió en el rango.

    Es la lista que convierte el informe en una conversación: no «abrió 12
    clientes» sino «estos 31 no los miró, y estos cinco le deben plata
    vencida».

    El saldo se calcula con el MISMO criterio de factura viva que usa el
    vendedor en su propia pantalla (`mi_cartera.queries`) y que la cartera de
    la oficina. Si divergen, la dueña y el vendedor discuten sobre números
    distintos.
    """
    params = ventana(desde, hasta)
    params.update({"vend": (vend or "").strip().upper(), "usuario": usuario})
    return db.fetch_all(
        """
        SELECT c.codigo_cli,
               COALESCE(NULLIF(TRIM(c.nombre), ''), c.codigo_cli) AS nombre,
               COALESCE(SUM(f.saldo), 0)                          AS saldo,
               COALESCE(SUM(CASE WHEN COALESCE(f.vencimiento, f.fecha)
                                      < CURRENT_DATE
                                 THEN f.saldo ELSE 0 END), 0)     AS vencido
          FROM scintela.cliente c
          LEFT JOIN scintela.factura f
                 ON f.codigo_cli = c.codigo_cli
                AND COALESCE(f.saldo, 0) <> 0
                AND (f.stat IS NULL OR f.stat IN ('Z','A','',' '))
                AND COALESCE(f.usuario_crea, '') <> 'asinfo-backfill'
         WHERE UPPER(TRIM(COALESCE(c.vend, ''))) = %(vend)s
           AND UPPER(TRIM(c.codigo_cli)) NOT IN (
                 SELECT codigo_cli
                   FROM scintela.uso_pantalla
                  WHERE usuario = %(usuario)s
                    AND codigo_cli IS NOT NULL
                    AND ts >= (%(desde)s AT TIME ZONE 'UTC')
                    AND ts <  (%(hasta)s AT TIME ZONE 'UTC'))
         GROUP BY c.codigo_cli, c.nombre
         ORDER BY vencido DESC, saldo DESC, c.codigo_cli
        """,
        params,
    )
