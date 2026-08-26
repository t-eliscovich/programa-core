"""Queries de comisiones de vendedores.

Replica MODIFICA.PRG PROCEDURE COMISION (línea 1770) — lista cobranzas
del mes filtradas por cliente.vend. TMT 2026-05-27 dueña: 'porque no
calcula las cosas' — abril mostraba cobranzas=0 porque la query antes
filtraba solo IN ('B','A'), faltaban V/W/I/J/K (cheques en distinto
estado de depósito que también son cobrados).

TMT 2026-08-03 dueña: 'las comisiones están siendo mal calculadas,
Dbase las tuvo diferentes para julio'. Eran DOS cosas, ver los
comentarios de `STATS_COBRADO` y `_CTE_CLI` abajo:
  1. faltaba el estado 'C' (cheque cobrado en caja) → $109.854,13 de
     cobranza de julio fuera de la comisión;
  2. `scintela.cliente` tiene códigos duplicados y el JOIN contaba esa
     cobranza dos veces → +$4.341,86 de más para PPR en julio.
Julio queda en $1.321.862,60 de cobranza / $13.218,63 de comisión, a
$0,04 del dBase (el resto de la diferencia contra el dBase es cartera
reasignada después del 26/04 — `CLIENTES.DBF` no se actualiza desde
esa fecha — y cheques cargados en PC después del último sync).

TMT 2026-05-27 dueña v2: 'me importa la plata que entró al banco. usar
fechad puro'. La cobranza del mes para comisión = cuándo el cheque se
depositó en banco (no cuándo el cliente lo entregó). Si un cheque
entró en abril pero se depositó el 2/5, la comisión es de MAYO. Para
corregir fechas mal cargadas, /cheques/<id>/editar ahora permite editar
fechad incluso si el cheque está depositado.

Comisión = cobranzas_mes * (pct_comision / 100). El dBase no calculaba
esto (la dueña lo hacía a mano); acá lo automatizamos.
"""

from datetime import date

import db
from filters import today_ec

# ─────────────────────────────────────────────────────────────────────
# TMT 2026-08-03 — dueña: "Las comisiones están siendo mal calculadas.
# Dbase las tuvo diferentes para julio". Tenía razón, y era UN estado.
#
# Paridad MODIFICA.PRG L1830/L1833:
#     COPY TO COMIS FOR MONTH(FECHAD)=MES .AND. YEAR(FECHAD)=YYEAR
#                       .AND. STAT $ "BWVCIK"
# El dBase cuenta la 'C'; PC la tiraba. 'C' = cheque COBRADO EN CAJA:
# ALTAS.PRG PROCEDURE PASOCAJA (L870-893) toma los cheques con NB=99
# ("EFT."), les crea la entrada de caja "CH."+CLIENTE y los marca
# STAT='C'. Es cobranza REAL — la plata entró, sólo que a caja y no al
# banco. Julio 2026: eran $109.854,13 de cobranza fuera de la comisión
# ($1.098,53 sin pagar), casi todo de PPR (60 de los 78 cheques del mes:
# es la cartera que paga en ventanilla).
#
# ⚠ Esta constante es PROPIA de comisiones y NO es
# `modules.cheques.queries.STATS_DEPOSITADO`. Allá 'C' significa otra
# cosa — "el cheque está en banco" → lockea los campos duros — y meterla
# ahí rompe la cobranza. Son dos preguntas distintas sobre el mismo
# campo: "¿está depositado?" vs. "¿entró la plata?".
#
# 'I', 'J', 'K' y 'A' no aparecen en ningún dato real (ni en el DBF ni en
# scintela.cheque); se dejan por paridad con el PRG y con la lista de
# cheques. 'W' existe en el DBF y el sync lo mapea a 'B'.
# Depositado en banco + cobrado en caja = la plata entró. Las familias salen de
# la tabla única de estados (modules/cheques/estados.py), no se re-escriben:
# tenían W/J/K, códigos de bancos viejos del DBF con 0 cheques, y cuando la
# familia cambiaba acá no se enteraba. TMT 2026-08-11.
from modules.cheques.estados import EN_BANCO as _EN_BANCO
from modules.cheques.estados import EN_CAJA as _EN_CAJA

STATS_COBRADO = _EN_BANCO + _EN_CAJA

# Fragmento SQL, armado UNA sola vez desde la constante para que las
# apariciones no puedan volver a divergir entre sí.
_IN_COBRADO = ", ".join(f"'{s}'" for s in STATS_COBRADO)

#: Los seis vendedores que cobran comisión.
#:
#: TMT 2026-05-19 v8, dueña: *"Sigo viendo muchos vendedores, no solo 6"*.
#: `scintela.vendedor` arrastra códigos legacy del backfill de `cliente.vend`
#: (BED, DJA, EDU, EVB, X, MRY…). Toda consulta que hable de "los vendedores"
#: filtra por esta lista.
#:
#: TMT 2026-08-04 — pasó de estar escrita adentro de un WHERE a ser constante
#: porque el chequeo de salud necesita LA MISMA lista: un cliente con un
#: código que no está acá se cae de todos los informes por vendedor sin que
#: nadie se entere, y su cobranza no le suma a nadie.
VENDEDORES_OFICIALES = ("PPR", "EDG", "SEP", "JQU", "FL1", "RMY")
_IN_VENDEDORES = ", ".join(f"'{v}'" for v in VENDEDORES_OFICIALES)

# ─────────────────────────────────────────────────────────────────────
# TMT 2026-08-04 — el cobro que entró a CAJA y NO tiene fila de cheque.
#
# Agregar la 'C' arriba arregló los cheques de ventanilla que PC sí tiene
# cargados. Pero en la primera semana de julio PC **no tiene la fila de
# cheque**: `dbf-import` trajo el movimiento de CAJA (`tipo='E'`,
# `concepto='CH.'+CLIENTE`, que es la convención de `ALTAS.PRG PROCEDURE
# PASOCAJA`) y nada más. El 01 y el 02/07 son 37 movimientos por
# $34.040,14 que la comisión no veía; de esos, $2.212,00 son de EDG y
# $1.305,00 de SEP.
#
# ⚠ NO se arregla cargando esos cheques a mano: **la plata ya está en
# PC**, en `scintela.caja`. Cargar el cheque la contaría DOS VECES — que
# es exactamente el error del 04/08 de madrugada ("apliqué 5 conversiones
# habiendo verificado 3, la deuda bajó dos veces"). Lo que faltaba no era
# el dato: era que la comisión mirara donde el dato está.
#
# El `NOT EXISTS` evita el doble conteo del otro lado: del 06/07 en
# adelante la pantalla de PC crea el par (caja + cheque) y el cheque ya
# suma por la rama de arriba. Se compara por (cliente, fecha, importe) y
# NO por `caja.id_cheque`, porque las filas del `dbf-import` tienen ese
# campo en NULL aunque el cheque exista: filtrar por `id_cheque IS NULL`
# contaría dos veces el 06, el 07 y el 10 de julio.
#
# Esta es, además, la rama que `scintela.cobro` prometía y nunca cumplió:
# esa tabla tiene UNA fila, del 14/03/2024, y nadie la escribe.
#
# Y una caja REVERSADA tampoco cuenta. Caso ICH (04/08): el cobro de
# $4.046,93 entró en efectivo el 30/06 y el 15/07 se pasó a depósito —
# `scintela.caja` tiene la salida `REVERSO id 299 — ES DEPOSITO` y se
# creó el cheque #100664 en el banco. Es la MISMA plata: si la caja de
# junio sigue contando y el cheque de julio también, el mes que viene
# paga comisión dos veces por un solo cobro. El dBase hace lo mismo con
# un asiento negativo el 14/07.
#
# ⚠ El match del reverso va por REGEX con borde y NO por
# `LIKE 'REVERSO id ' || id || '%%'`: con LIKE, `id 2` pesca
# "REVERSO id 299" y se cae de la comisión una caja ajena (medido:
# CH.VIL, $2.488,01).
_CAJA_COBRO_FROM = """
              FROM scintela.caja k
              JOIN cli c
                ON UPPER(TRIM(c.codigo_cli))
                 = UPPER(TRIM(SUBSTRING(k.concepto FROM 4)))
             WHERE k.tipo = 'E'
               AND k.concepto LIKE 'CH.%%'
               AND NOT EXISTS (
                     SELECT 1
                       FROM scintela.cheque chx
                      WHERE chx.no_banco = '99'
                        AND chx.fechad   = k.fecha
                        AND ROUND(chx.importe, 2) = ROUND(k.importe, 2)
                        AND UPPER(TRIM(chx.codigo_cli))
                          = UPPER(TRIM(SUBSTRING(k.concepto FROM 4))))
               AND NOT EXISTS (
                     SELECT 1
                       FROM scintela.caja kr
                      WHERE kr.tipo = 'S'
                        AND kr.concepto ~ ('^REVERSO id ' || k.id_caja
                                           || '([^0-9]|$)'))"""

# ─────────────────────────────────────────────────────────────────────
# TMT 2026-08-03 — `scintela.cliente` tiene 25 códigos DUPLICADOS
# (3.973 filas / 3.948 códigos): dos empresas distintas comparten el
# mismo código de 3 letras. Ej. 'GUF' = "ASOTEXMANA NUBE FAJARDO BORJA"
# (RUC 1591718165001) Y "GUADALUPE FIALLOS" (RUC 1801968924001), las dos
# de PPR. Un `JOIN scintela.cliente` crudo devuelve 2 filas por cheque y
# **cuenta la cobranza dos veces**: en julio 2026 le inflaba a PPR
# $4.341,86 de cobranza y $23.795,07 de ventas.
#
# El dBase NO tiene el problema: `CLIENTE $ GRUPO` (MODIFICA.PRG L1836)
# es un test de substring, matchea una sola vez aunque el código esté
# repetido en el grupo. Deduplicar es paridad con el PRG, no un criterio
# nuevo.
#
# Desempate: gana la fila QUE TIENE vendedor (si una está en blanco no
# sirve para comisiones), y después por nombre para que sea determinista
# — sin `ORDER BY` completo el resultado cambia entre corridas.
#
# Los cheques de las dos empresas ya vienen mezclados bajo un solo
# `cheque.codigo_cli`: separarlos requiere limpiar los códigos repetidos
# en `scintela.cliente`. Hasta entonces esto es lo correcto.
# ─────────────────────────────────────────────────────────────────────
# ⭐ EL MES, COMO RANGO DE FECHAS Y NO COMO `EXTRACT`
#
# TMT 2026-08-26 (dueña): *"algo más que dure mucho tiempo y podamos bajar"*.
#
# Todas estas consultas filtraban el mes así:
#
#     WHERE EXTRACT(YEAR FROM ch.fechad)  = %(yy)s
#       AND EXTRACT(MONTH FROM ch.fechad) = %(mm)s
#
# Envolver la columna en una función tiene dos costos, y el segundo es el caro:
#
#   1. Ningún índice se puede usar — y `scintela.cheque` ni siquiera tenía uno
#      sobre `fechad`, que es la fecha con la que se cuenta TODA la cobranza
#      (migración 0230). Se recorrían los 60.000 cheques enteros en cada carga
#      del Inicio del vendedor.
#   2. El planificador queda CIEGO: no sabe estimar cuántas filas caen en un
#      mes, calcula 1 donde hay 379, y con esa cuenta elige un nested loop
#      contra el CTE de clientes — 1,5 millones de comparaciones.
#
# Medido el 26/08 contra una base sembrada a escala de producción (3.986
# clientes, 84.000 facturas, 60.000 cheques), el detalle de cobranza de un
# vendedor pasó de **549 ms a 5 ms**, con el resultado igual fila por fila. Lo
# único que cambió es la forma de preguntar por el mes.
#
# ⚠ El rango va `>= primero del mes` y `< primero del mes SIGUIENTE`, nunca
# `BETWEEN` con el último día: si alguna de estas columnas algún día guarda una
# hora, el `BETWEEN` se come el último día del mes y no lo nota nadie hasta que
# la comisión no cierra.


def _rango_mes(anio: int, mes: int) -> tuple[date, date]:
    """El primer día del mes y el primero del siguiente."""
    return date(int(anio), int(mes), 1), _primero_del_siguiente(int(anio), int(mes))


def _rango_hasta_mes(anio: int, hasta_mes: int) -> tuple[date, date]:
    """De enero a `hasta_mes` INCLUSIVE, del mismo año."""
    return date(int(anio), 1, 1), _primero_del_siguiente(int(anio), int(hasta_mes))


def _params_mes(anio: int, mes: int) -> dict:
    """El mes, listo para pegar en el diccionario de parámetros del SQL."""
    desde, hasta = _rango_mes(anio, mes)
    return {"f_desde": desde, "f_hasta": hasta}


def _params_hasta_mes(anio: int, hasta_mes: int) -> dict:
    """Lo mismo, para las consultas que van de enero a un mes."""
    desde, hasta = _rango_hasta_mes(anio, hasta_mes)
    return {"f_desde": desde, "f_hasta": hasta}


def _primero_del_siguiente(anio: int, mes: int) -> date:
    """Diciembre rueda al año que viene: sin esto, el mes 13 revienta."""
    return date(anio + 1, 1, 1) if int(mes) >= 12 else date(anio, int(mes) + 1, 1)


_CTE_CLI = """
        cli AS (
            SELECT DISTINCT ON (codigo_cli)
                   codigo_cli, vend, nombre
              FROM scintela.cliente
             ORDER BY codigo_cli,
                      (CASE WHEN COALESCE(TRIM(vend), '') = '' THEN 1 ELSE 0 END),
                      nombre
        )"""


def lista(*, anio: int | None = None, mes: int | None = None) -> list[dict]:
    """Lista vendedores con totales del mes elegido.

    Si `anio`/`mes` no se pasan → usa el mes en curso.

    Cada fila incluye:
        codigo, nombre, pct_comision, activo,
        n_clientes, cobranzas_mes, ventas_mes, comision_mes,
        ventas_kg (kilos facturados del mes).
    """
    hoy = today_ec()
    yy = int(anio) if anio else hoy.year
    mm = int(mes) if mes else hoy.month

    return db.fetch_all(
        f"""
        WITH{_CTE_CLI},
        -- TMT 2026-05-19 v8 — dueña: "N° clientes tiene que decir num
        -- de clientes que se estan sumando para esa comisión". Antes
        -- contaba todos los clientes con cliente.vend = código. Ahora
        -- cuenta solo los clientes DISTINCT que cobraron este mes
        -- (sea cheque o efectivo) y que tienen el vendedor asignado.
        cobros_mes AS (
            SELECT UPPER(TRIM(c.vend))        AS codigo,
                   ch.codigo_cli              AS codigo_cli
              FROM scintela.cheque ch
              JOIN cli c ON c.codigo_cli = ch.codigo_cli
             WHERE ch.fechad >= %(f_desde)s
               AND ch.fechad < %(f_hasta)s
               AND ch.stat IN ({_IN_COBRADO})
               AND c.vend IS NOT NULL AND TRIM(c.vend) <> ''
            UNION
            SELECT UPPER(TRIM(c.vend))        AS codigo,
                   co.codigo_cli              AS codigo_cli
              FROM scintela.cobro co
              JOIN cli c ON c.codigo_cli = co.codigo_cli
             WHERE co.fecha >= %(f_desde)s
               AND co.fecha < %(f_hasta)s
               AND UPPER(COALESCE(co.tipo_doc, '')) NOT LIKE '%%CHE%%'
               AND c.vend IS NOT NULL AND TRIM(c.vend) <> ''
            UNION
            SELECT UPPER(TRIM(c.vend))        AS codigo,
                   c.codigo_cli               AS codigo_cli
            {_CAJA_COBRO_FROM}
               AND k.fecha >= %(f_desde)s
               AND k.fecha < %(f_hasta)s
               AND c.vend IS NOT NULL AND TRIM(c.vend) <> ''
        ),
        clientes_por_vend AS (
            SELECT codigo, COUNT(DISTINCT codigo_cli) AS n_clientes
              FROM cobros_mes
             GROUP BY codigo
        ),
        cobranzas_mes AS (
            -- TMT 2026-05-19 v8 — dueña: "todas las cobranzas (cheques o
            -- depositos de efectivos) del mes". Hasta ahora solo contábamos
            -- cheques acreditados (stat B/A). Ahora sumamos también los
            -- cobros NO-cheque de scintela.cobro (efectivo, transferencias,
            -- depósitos directos) para que la comisión cubra el cobro real.
            --
            -- Paridad MODIFICA.PRG L1834:
            --   FOR MONTH(FECHAD)=MES AND YEAR(FECHAD)=YYEAR AND STAT $ 'BWVCIK'
            -- En PC: cheque.stat IN (B, A) = depositados/acreditados.
            --
            -- Cheques acreditados:
            SELECT UPPER(TRIM(c.vend))            AS codigo,
                   COALESCE(SUM(ch.importe), 0)   AS total
              FROM scintela.cheque ch
              JOIN cli c ON c.codigo_cli = ch.codigo_cli
             WHERE ch.fechad >= %(f_desde)s
               AND ch.fechad < %(f_hasta)s
               AND ch.stat IN ({_IN_COBRADO})
               AND c.vend IS NOT NULL AND TRIM(c.vend) <> ''
             GROUP BY UPPER(TRIM(c.vend))
            UNION ALL
            -- Cobros no-cheque (efectivo / transferencia / depósito directo)
            -- registrados en scintela.cobro. Filtramos cheques para no doble
            -- contar — ya se cuentan arriba via scintela.cheque.
            SELECT UPPER(TRIM(c.vend))            AS codigo,
                   COALESCE(SUM(co.valor), 0)     AS total
              FROM scintela.cobro co
              JOIN cli c ON c.codigo_cli = co.codigo_cli
             WHERE co.fecha >= %(f_desde)s
               AND co.fecha < %(f_hasta)s
               AND UPPER(COALESCE(co.tipo_doc, '')) NOT LIKE '%%CHE%%'
               AND c.vend IS NOT NULL AND TRIM(c.vend) <> ''
             GROUP BY UPPER(TRIM(c.vend))
            UNION ALL
            -- Cobros que entraron a CAJA sin fila de cheque. Ver
            -- `_CAJA_COBRO_FROM`: la plata ya está en PC, lo que faltaba
            -- era mirarla.
            SELECT UPPER(TRIM(c.vend))            AS codigo,
                   COALESCE(SUM(k.importe), 0)    AS total
            {_CAJA_COBRO_FROM}
               AND k.fecha >= %(f_desde)s
               AND k.fecha < %(f_hasta)s
               AND c.vend IS NOT NULL AND TRIM(c.vend) <> ''
             GROUP BY UPPER(TRIM(c.vend))
        ),
        cobranzas_total AS (
            -- Sumar ambas fuentes por vendedor.
            SELECT codigo, SUM(total) AS total
              FROM cobranzas_mes
             GROUP BY codigo
        ),
        ventas_mes AS (
            -- Bonus PC: facturas emitidas del mes por vendedor (no del PRG).
            --
            -- TMT 2026-08-18 dueña: *"en ventas y en cobranzas puedes
            -- poner los kg abajo"*. Las metas de venta se miden EN KILOS
            -- (ver mi_cartera.queries), así que la pantalla habla esa
            -- unidad: `factura.kg` del mes, exacto.
            --
            -- ⚠ COBRANZAS quedó SIN kilos, a propósito. La cobranza es
            -- plata y no tiene kilos propios: habría que imputarle los
            -- de la factura que pagó, prorrateando por `chequesxfact`.
            -- Medido el 18/08 sobre agosto: de los $602.828 cobrados,
            -- $259.971 (165 de 476 cheques) son cheques del
            -- `dbf-import` que nunca tuvieron factura vinculada — el
            -- dBase no guardaba ese vínculo. El prorrateo daba 36.640 kg
            -- contra 82.000 kg vendidos: menos de la mitad, al lado de
            -- una cobranza que casi iguala a la venta. Se le ofreció a
            -- la dueña estimar el hueco al $/kg del mes (66.280 kg) y
            -- decidió que **no**: kilos sólo donde el dato es exacto.
            -- Si algún día se vinculan esos cheques (ver
            -- /admin/dbase/abonos-historicos), volver a proponerlo.
            SELECT UPPER(TRIM(c.vend))            AS codigo,
                   COALESCE(SUM(f.importe), 0)    AS total,
                   COALESCE(SUM(f.kg), 0)         AS kg
              FROM scintela.factura f
              JOIN cli c ON c.codigo_cli = f.codigo_cli
             WHERE f.fecha >= %(f_desde)s
               AND f.fecha < %(f_hasta)s
               AND (f.stat IS NULL OR f.stat <> 'X')
               AND c.vend IS NOT NULL AND TRIM(c.vend) <> ''
             GROUP BY UPPER(TRIM(c.vend))
        )
        SELECT v.codigo,
               v.nombre,
               v.pct_comision,
               v.activo,
               COALESCE(cv.n_clientes, 0)                                    AS n_clientes,
               COALESCE(co.total, 0)                                         AS cobranzas_mes,
               COALESCE(ve.total, 0)                                         AS ventas_mes,
               COALESCE(ve.kg, 0)                                            AS ventas_kg,
               ROUND(COALESCE(co.total, 0)
                     * COALESCE(v.pct_comision, 0) / 100.0, 2)::numeric      AS comision_mes
          FROM scintela.vendedor v
          LEFT JOIN clientes_por_vend cv ON cv.codigo = v.codigo
          LEFT JOIN cobranzas_total    co ON co.codigo = v.codigo
          LEFT JOIN ventas_mes         ve ON ve.codigo = v.codigo
         -- Los 6 oficiales, de la constante (ver VENDEDORES_OFICIALES).
         WHERE v.codigo IN ({_IN_VENDEDORES})
         ORDER BY comision_mes DESC, cobranzas_mes DESC, v.codigo
        """,
        _params_mes(yy, mm),
    ) or []


def por_codigo(codigo: str) -> dict | None:
    return db.fetch_one(
        """
        SELECT codigo, nombre, pct_comision, activo,
               fecha_crea, fecha_actualiza, usuario_actualiza
          FROM scintela.vendedor
         WHERE codigo = UPPER(TRIM(%s))
        """,
        (codigo,),
    )


def actualizar_pct(codigo: str, pct: float, usuario: str = "web") -> int:
    """Actualiza el % de comisión de un vendedor."""
    return db.execute(
        """
        UPDATE scintela.vendedor
           SET pct_comision      = %s,
               fecha_actualiza   = CURRENT_TIMESTAMP,
               usuario_actualiza = %s
         WHERE codigo = UPPER(TRIM(%s))
        """,
        (pct, usuario[:30], codigo),
    )


def actualizar_nombre(codigo: str, nombre: str, usuario: str = "web") -> int:
    return db.execute(
        """
        UPDATE scintela.vendedor
           SET nombre            = %s,
               fecha_actualiza   = CURRENT_TIMESTAMP,
               usuario_actualiza = %s
         WHERE codigo = UPPER(TRIM(%s))
        """,
        (nombre[:100], usuario[:30], codigo),
    )


def cobranzas_detalle(codigo: str, *, anio: int, mes: int) -> list[dict]:
    """Detalle de cobranzas del mes para un vendedor.

    TMT 2026-05-19 v8 — incluye cheques acreditados (stat B/A) Y
    cobros no-cheque (efectivo, transferencia, depósito directo) de
    scintela.cobro, replicando el cálculo de comisión del PRG ampliado
    al pedido de la dueña.
    """
    return db.fetch_all(
        f"""
        WITH{_CTE_CLI}
        -- Cheques acreditados
        SELECT 'CHE'                          AS origen,
               ch.id_cheque                   AS id_origen,
               ch.no_cheque                   AS doc,
               ch.fechad                      AS fecha,
               ch.importe                     AS importe,
               ch.codigo_cli                  AS codigo_cli,
               COALESCE(c.nombre, '')         AS cliente,
               COALESCE(b.nombre, ch.banco)   AS banco,
               ch.stat                        AS stat
          FROM scintela.cheque ch
          JOIN cli c  ON c.codigo_cli = ch.codigo_cli
          LEFT JOIN scintela.banco b ON b.no_banco = ch.no_banco
         WHERE ch.fechad >= %(f_desde)s
           AND ch.fechad < %(f_hasta)s
           AND ch.stat IN ({_IN_COBRADO})
           AND UPPER(TRIM(c.vend)) = UPPER(TRIM(%(codigo)s))
        UNION ALL
        -- Cobros no-cheque (efectivo / transferencia / depósito directo).
        SELECT COALESCE(UPPER(co.tipo_doc), 'EFE') AS origen,
               co.id_cobro                        AS id_origen,
               CAST(co.no_docpago AS text)        AS doc,
               co.fecha                           AS fecha,
               co.valor                           AS importe,
               co.codigo_cli                      AS codigo_cli,
               COALESCE(c.nombre, '')             AS cliente,
               co.banco                           AS banco,
               ''                                 AS stat
          FROM scintela.cobro co
          JOIN cli c ON c.codigo_cli = co.codigo_cli
         WHERE co.fecha >= %(f_desde)s
           AND co.fecha < %(f_hasta)s
           AND UPPER(COALESCE(co.tipo_doc, '')) NOT LIKE '%%CHE%%'
           AND UPPER(TRIM(c.vend)) = UPPER(TRIM(%(codigo)s))
        UNION ALL
        -- Cobros de CAJA sin fila de cheque (ver `_CAJA_COBRO_FROM`).
        -- Aparecen en el detalle con su propio origen para que el
        -- vendedor pueda ver de dónde sale cada peso: el total de la
        -- pantalla ES la suma de esta lista.
        SELECT 'CAJA'                         AS origen,
               k.id_caja                      AS id_origen,
               k.concepto                     AS doc,
               k.fecha                        AS fecha,
               k.importe                      AS importe,
               c.codigo_cli                   AS codigo_cli,
               COALESCE(c.nombre, '')         AS cliente,
               'CAJA'                         AS banco,
               'C'                            AS stat
        {_CAJA_COBRO_FROM}
           AND k.fecha >= %(f_desde)s
           AND k.fecha < %(f_hasta)s
           AND UPPER(TRIM(c.vend)) = UPPER(TRIM(%(codigo)s))
         ORDER BY fecha, id_origen
        """,
        {"codigo": codigo, **_params_mes(anio, mes)},
    ) or []


def cobranzas_por_cliente_anio(codigo: str, *, anio: int,
                               hasta_mes: int = 12) -> list[dict]:
    """Cobranza acreditada del AÑO, agregada por (mes, cliente). UNA query.

    Misma definición que `cobranzas_detalle` —mismos estados, mismo CTE de
    deduplicación, mismo filtro de no-cheque— pero sumada en la base en vez
    de traer fila por fila.

    ⭐ Existe por una regresión del 2026-08-03: la lista "mes a mes" del
    portal del vendedor llamaba al detalle completo del mes UNA VEZ POR MES
    para armar la columna. Con 8 meses cargados eso son 8 UNION sobre
    cheque+cobro por cada visita a la pantalla: **3.190 ms** contra 162 ms
    del Inicio. La dueña, 2026-08-04: *"también está súper lento"*.

    Se devuelve el desglose POR CLIENTE y no un total por mes a propósito:
    la comisión canónica es la suma de las comisiones redondeadas de cada
    cliente (ver `mi_cartera.queries.comision_mes`). Si acá se sumara todo
    el mes de una y recién después se aplicara el %, la lista mes a mes
    volvería a diferir del desglose en centavos — que es exactamente el bug
    del $7,73 vs $7,74 que ya se arregló una vez.
    """
    return db.fetch_all(
        f"""
        WITH{_CTE_CLI},
        mov AS (
            SELECT EXTRACT(MONTH FROM ch.fechad)::int AS mes,
                   TRIM(ch.codigo_cli)                AS codigo_cli,
                   ch.importe                         AS importe
              FROM scintela.cheque ch
              JOIN cli c ON c.codigo_cli = ch.codigo_cli
             WHERE ch.fechad >= %(f_desde)s
               AND ch.fechad < %(f_hasta)s
               AND ch.stat IN ({_IN_COBRADO})
               AND UPPER(TRIM(c.vend)) = UPPER(TRIM(%(codigo)s))
            UNION ALL
            SELECT EXTRACT(MONTH FROM co.fecha)::int AS mes,
                   TRIM(co.codigo_cli)               AS codigo_cli,
                   co.valor                          AS importe
              FROM scintela.cobro co
              JOIN cli c ON c.codigo_cli = co.codigo_cli
             WHERE co.fecha >= %(f_desde)s
               AND co.fecha < %(f_hasta)s
               AND UPPER(COALESCE(co.tipo_doc, '')) NOT LIKE '%%CHE%%'
               AND UPPER(TRIM(c.vend)) = UPPER(TRIM(%(codigo)s))
            UNION ALL
            SELECT EXTRACT(MONTH FROM k.fecha)::int AS mes,
                   TRIM(c.codigo_cli)                AS codigo_cli,
                   k.importe                         AS importe
            {_CAJA_COBRO_FROM}
               AND k.fecha >= %(f_desde)s
               AND k.fecha < %(f_hasta)s
               AND UPPER(TRIM(c.vend)) = UPPER(TRIM(%(codigo)s))
        )
        SELECT mes, codigo_cli, COALESCE(SUM(importe), 0) AS cobrado
          FROM mov
         GROUP BY mes, codigo_cli
         ORDER BY mes DESC, cobrado DESC
        """,
        {"codigo": codigo, **_params_hasta_mes(anio, hasta_mes)},
    ) or []


def ventas_detalle(codigo: str, *, anio: int, mes: int) -> list[dict]:
    """Detalle de facturas emitidas del mes para un vendedor."""
    return db.fetch_all(
        f"""
        WITH{_CTE_CLI}
        SELECT f.id_factura, f.numf, f.numf_completo, f.fecha, f.vencimiento,
               f.importe, f.saldo, f.stat,
               f.codigo_cli,
               COALESCE(c.nombre, '')   AS cliente
          FROM scintela.factura f
          JOIN cli c ON c.codigo_cli = f.codigo_cli
         WHERE f.fecha >= %(f_desde)s
           AND f.fecha < %(f_hasta)s
           AND (f.stat IS NULL OR f.stat <> 'X')
           AND UPPER(TRIM(c.vend)) = UPPER(TRIM(%(codigo)s))
         ORDER BY f.fecha, f.id_factura
        """,
        {"codigo": codigo, **_params_mes(anio, mes)},
    ) or []


def crear(codigo: str, nombre: str, pct: float = 0, usuario: str = "web") -> dict:
    """Alta manual de vendedor (no auto-backfilleado)."""
    cod = codigo.strip().upper()[:3]
    if not cod:
        raise ValueError("Código requerido (3 letras).")
    return db.execute_returning(
        """
        INSERT INTO scintela.vendedor
            (codigo, nombre, pct_comision, fecha_crea, usuario_actualiza, fecha_actualiza)
        VALUES (%s, %s, %s, CURRENT_DATE, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (codigo) DO NOTHING
        RETURNING codigo
        """,
        (cod, nombre[:100], pct, usuario[:30]),
    ) or {"codigo": cod}


def cobranza_periodo(vend: str, desde, hasta) -> float:
    """Cobranza acreditada de un vendedor entre dos fechas, inclusive.

    ⭐ Existe para que el PORTAL DEL VENDEDOR y la pantalla de la oficina no
    puedan contar distinto. Hasta el 04/08 `mi_cartera.queries.cobrado` tenía
    su propia lista de estados —**sin la 'C'**— y ninguna rama de caja, así
    que el vendedor veía en el KPI una comisión más chica que la del detalle
    que la propia pantalla le mostraba abajo (el detalle sí reusa
    `cobranzas_detalle`). El total tiene que SER la suma del desglose.

    Mismas tres fuentes que la comisión: cheques acreditados, cobros
    no-cheque de `scintela.cobro`, y cobros de CAJA sin fila de cheque.
    """
    row = db.fetch_one(
        f"""
        WITH{_CTE_CLI}
        SELECT COALESCE((
            SELECT SUM(ch.importe)
              FROM scintela.cheque ch
              JOIN cli c ON c.codigo_cli = ch.codigo_cli
             WHERE UPPER(TRIM(COALESCE(c.vend, ''))) = UPPER(TRIM(%(vend)s))
               AND ch.fechad BETWEEN %(desde)s AND %(hasta)s
               AND ch.stat IN ({_IN_COBRADO})
        ), 0) + COALESCE((
            SELECT SUM(co.valor)
              FROM scintela.cobro co
              JOIN cli c ON c.codigo_cli = co.codigo_cli
             WHERE UPPER(TRIM(COALESCE(c.vend, ''))) = UPPER(TRIM(%(vend)s))
               AND co.fecha BETWEEN %(desde)s AND %(hasta)s
               AND UPPER(COALESCE(co.tipo_doc, '')) NOT LIKE '%%CHE%%'
        ), 0) + COALESCE((
            SELECT SUM(k.importe)
            {_CAJA_COBRO_FROM}
               AND UPPER(TRIM(COALESCE(c.vend, ''))) = UPPER(TRIM(%(vend)s))
               AND k.fecha BETWEEN %(desde)s AND %(hasta)s
        ), 0) AS total
        """,
        {"vend": vend, "desde": desde, "hasta": hasta},
    )
    return float((row or {}).get("total") or 0)
