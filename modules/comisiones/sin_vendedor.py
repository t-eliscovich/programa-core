"""Cuánta cobranza no le suma a ningún vendedor.

⭐ POR QUÉ EXISTE (TMT 2026-08-04). Al cerrar julio contra el dBase apareció
que la comisión se apoya en un campo —`cliente.vend`— que **nadie vigila**. Un
cliente sin vendedor cobra igual, baja su deuda igual, y su plata simplemente
no aparece en ningún informe por vendedor. No hay error en pantalla: hay un
número que falta y nadie sabe que falta.

Medido sobre julio 2026: **$1.117.837,12 de 112 clientes sin vendedor**, contra
$1.346.321,48 de 351 clientes con vendedor. O sea, casi la mitad de la cobranza
del mes no le suma a nadie.

## Esto NO es una alarma — y la distinción es el punto

La dueña, textual: *"si hay algunos que compran directo a la fábrica sin
vendedor"*. Un cliente sin vendedor es un **estado válido**: la venta directa
existe y no le corresponde comisión a nadie. Un chequeo que lo pinte de rojo
estaría mintiendo, y peor: acostumbra a mirar rojo y seguir de largo.

Lo que sí se reporta como problema son las dos formas de **perder** un
vendedor sin querer:

* **Código que no es de nadie.** `cliente.vend` es texto libre y arrastra
  códigos legacy del backfill (BED, DJA, EDU, EVB, X, MRY…). Un cliente con
  uno de esos no está "sin vendedor": está asignado a un vendedor que no
  existe, así que se cae de todos los informes *y* nadie nota el hueco. En
  julio: 5 clientes, $11.400,11.
* **Un cliente que TENÍA vendedor y lo perdió.** Eso no es venta directa, es
  una edición que salió mal. Pasó, y en silencio: cuatro fichas mudas
  (ADO/OVB/UDA/LCP) rechazaban el guardado con "Nombre requerido" y **no
  guardaban nada**, así que la asignación se perdía sin un solo mensaje.

## De dónde sale el "antes"

`scintela.cliente` guarda el estado, no la historia — no hay tabla de
versiones. Lo único que recuerda es `bitacora_acciones`, que graba el payload
del formulario en cada `clientes.editar`. Comparando el `vend` de una edición
con el de la edición anterior del MISMO código se ve la transición
"tenía → quedó vacío".

⚠️ Sus límites, para que nadie lea de más: sólo ve lo que pasó **por la
pantalla de clientes** (el `dbf-import` no deja bitácora), y sólo desde que la
bitácora existe. Un cliente editado una sola vez no tiene "anterior" y no puede
delatar nada. Es lo que hay; decir que cubre más sería inventar.
"""
from __future__ import annotations

from datetime import date

import db

from .queries import _IN_COBRADO, _IN_VENDEDORES, VENDEDORES_OFICIALES  # noqa: F401

# ---------------------------------------------------------------------------
# La cifra
# ---------------------------------------------------------------------------
# Se mide sobre `cheque` solamente, y no sobre las tres fuentes que suma la
# comisión (cheque + cobro + caja). A propósito:
#   · `scintela.cobro` tiene UNA fila, del 14/03/2024, y nadie la escribe.
#   · la rama de caja de la comisión existe para los cobros que el import trajo
#     SIN su cheque; acá el número no se compara contra la comisión, se usa
#     para dimensionar. Sumarla obligaría a arrastrar el anti-join del doble
#     conteo a un lugar donde no aporta nada.
# Si algún día esto se compara peso a peso contra la comisión, hay que usar
# `comisiones.queries.cobranza_periodo` y no ampliar esta query.
_SQL_CIFRA = f"""
WITH cli AS (
    -- Una fila por código. `scintela.cliente` tuvo códigos duplicados y un
    -- JOIN crudo contaba la cobranza dos veces (le infló $4.341,86 a PPR en
    -- julio). Hoy hay un índice único que lo impide, pero la query no se
    -- apoya en eso.
    SELECT DISTINCT ON (UPPER(TRIM(codigo_cli)))
           UPPER(TRIM(codigo_cli)) AS codigo_cli,
           UPPER(TRIM(COALESCE(vend,''))) AS vend,
           nombre
      FROM scintela.cliente
     WHERE COALESCE(TRIM(codigo_cli),'') <> ''
     ORDER BY UPPER(TRIM(codigo_cli)), id_cliente
)
SELECT CASE WHEN c.vend IN ({_IN_VENDEDORES}) THEN 'con_vendedor'
            WHEN c.vend = ''                  THEN 'sin_vendedor'
            ELSE 'codigo_desconocido' END          AS clase,
       COUNT(DISTINCT c.codigo_cli)                AS n_clientes,
       COUNT(*)                                    AS n_cheques,
       ROUND(COALESCE(SUM(ch.importe), 0), 2)      AS total
  FROM scintela.cheque ch
  JOIN cli c ON c.codigo_cli = UPPER(TRIM(ch.codigo_cli))
 WHERE UPPER(TRIM(COALESCE(ch.stat,''))) IN ({_IN_COBRADO})
   AND ch.fechad BETWEEN %(desde)s AND %(hasta)s
 GROUP BY 1
"""

CON_VENDEDOR = "con_vendedor"
SIN_VENDEDOR = "sin_vendedor"
CODIGO_DESCONOCIDO = "codigo_desconocido"
_CLASES = (CON_VENDEDOR, SIN_VENDEDOR, CODIGO_DESCONOCIDO)


def cifra(desde: date, hasta: date) -> dict[str, dict]:
    """Cobranza del período partida en tres: con vendedor, sin, y código raro.

    Devuelve SIEMPRE las tres clases, aunque estén en cero. Una clase ausente
    obliga a quien muestra el número a inventar el caso vacío, y ahí es donde
    se cuela un `KeyError` en la pantalla que menos se mira.
    """
    filas = db.fetch_all(_SQL_CIFRA, {"desde": desde, "hasta": hasta}) or []
    por_clase = {
        c: {"clase": c, "n_clientes": 0, "n_cheques": 0, "total": 0.0}
        for c in _CLASES
    }
    for f in filas:
        por_clase[f["clase"]] = {
            "clase": f["clase"],
            "n_clientes": int(f["n_clientes"] or 0),
            "n_cheques": int(f["n_cheques"] or 0),
            "total": float(f["total"] or 0),
        }
    return por_clase


def porcentaje_sin_vendedor(datos: dict[str, dict]) -> float:
    """Qué parte de la cobranza del período no le suma a ningún vendedor.

    Cuenta el código desconocido junto con el vacío: para el informe por
    vendedor los dos son lo mismo — plata que no aparece.
    """
    total = sum(v["total"] for v in datos.values())
    if total <= 0:
        return 0.0
    sin = datos[SIN_VENDEDOR]["total"] + datos[CODIGO_DESCONOCIDO]["total"]
    return round(sin * 100.0 / total, 1)


# ---------------------------------------------------------------------------
# Los clientes con un código que no es de ningún vendedor
# ---------------------------------------------------------------------------
_SQL_CODIGO_DESCONOCIDO = f"""
SELECT UPPER(TRIM(c.codigo_cli))              AS codigo_cli,
       c.nombre,
       UPPER(TRIM(COALESCE(c.vend,'')))       AS vend,
       COUNT(ch.id_cheque)                    AS n_cheques,
       ROUND(COALESCE(SUM(ch.importe), 0), 2) AS total
  FROM scintela.cliente c
  LEFT JOIN scintela.cheque ch
         ON UPPER(TRIM(ch.codigo_cli)) = UPPER(TRIM(c.codigo_cli))
        AND UPPER(TRIM(COALESCE(ch.stat,''))) IN ({_IN_COBRADO})
        AND ch.fechad BETWEEN %(desde)s AND %(hasta)s
 WHERE COALESCE(TRIM(c.vend),'') <> ''
   AND UPPER(TRIM(c.vend)) NOT IN ({_IN_VENDEDORES})
 GROUP BY 1, 2, 3
HAVING COUNT(ch.id_cheque) > 0
 ORDER BY total DESC
"""


def codigos_desconocidos(desde: date, hasta: date) -> list[dict]:
    """Clientes que cobraron y tienen un `vend` que no es de los seis.

    Sólo los que MOVIERON plata en el período: hay ~1.500 fichas viejas con
    códigos legacy y listarlas todas convierte el reporte en un directorio.
    """
    return db.fetch_all(_SQL_CODIGO_DESCONOCIDO, {"desde": desde, "hasta": hasta}) or []


# ---------------------------------------------------------------------------
# Los que TENÍAN vendedor y lo perdieron  — esto sí es una alarma
# ---------------------------------------------------------------------------
_SQL_PERDIERON_VENDEDOR = """
WITH ed AS (
    SELECT ts,
           UPPER(TRIM(COALESCE(payload->>'codigo_cli',''))) AS codigo_cli,
           UPPER(TRIM(COALESCE(payload->>'vend','')))       AS vend,
           usuario
      FROM scintela.bitacora_acciones
     WHERE accion IN ('clientes.editar', 'clientes.nuevo')
       AND COALESCE(status_http, 200) < 400
       AND COALESCE(payload->>'codigo_cli','') <> ''
       AND ts >= %(desde_ts)s
),
seq AS (
    SELECT ed.*, LAG(vend) OVER (PARTITION BY codigo_cli ORDER BY ts) AS antes
      FROM ed
),
perdidas AS (
    SELECT DISTINCT ON (codigo_cli)
           codigo_cli, antes, ts, usuario
      FROM seq
     WHERE vend = '' AND COALESCE(antes,'') <> ''
     ORDER BY codigo_cli, ts DESC
)
SELECT p.codigo_cli, p.antes AS vendedor_anterior, p.ts AS cuando, p.usuario,
       c.nombre,
       UPPER(TRIM(COALESCE(c.vend,''))) AS vend_hoy
  FROM perdidas p
  LEFT JOIN scintela.cliente c
         ON UPPER(TRIM(c.codigo_cli)) = p.codigo_cli
 -- Si ya se lo devolvieron, no hay nada que avisar. El chequeo corre todos
 -- los días: sin esto seguiría gritando por algo que alguien ya arregló.
 WHERE COALESCE(TRIM(c.vend),'') = ''
 ORDER BY p.ts DESC
"""


def perdieron_vendedor(desde_ts: date) -> list[dict]:
    """Clientes que tenían vendedor, quedaron sin él, y siguen así."""
    return db.fetch_all(_SQL_PERDIERON_VENDEDOR, {"desde_ts": desde_ts}) or []
