"""Trae de Asinfo lo que hace falta para la pantalla de LO PARADO.

Dos consultas y un remate:

    parados()   ~2 s — producto terminado (bodega 53) con stock ≥ 20 kg y menos
                de 1 kg vendido en 12 meses, abierto en primera y segunda.
    llamados()  ~5 s — por cada TELA parada, los 12 clientes de más kilos del
                ÚLTIMO año en que alguien la compró. Normalmente es el
                corriente; si no, el anterior; y si tampoco, 2024 o 2023.
    vendido_desde() — cuánto se vendió de cada tela × color desde que se marcó.

Definiciones que NO son obvias y que hay que respetar para que la pantalla no
contradiga a otras:

⭐ **El color no entra en la lista de llamados.** Un cliente que compra Kiana
Forro compra Kiana Forro; el color se negocia. Filtrar por tela × color deja
listas de un cliente o de ninguno.

⭐ **La familia (Fleece, Jersey, Poliester) NO se usa.** Se probó: 498 de los 886
clientes del año compraron *algún* Jersey. Eso no es una lista de llamados.

⭐ **Sólo líneas de venta con cantidad > 0.** Las notas de crédito (documentos
20/451) NO restan acá: "vendió 0 kg en 12 meses" tiene que significar que no
salió, no que salió y volvió. Si se netearan, una tela que se vendió y se
devolvió entera aparecería como parada, que es justo lo contrario de la verdad.

⭐ **`VPM` (CONSUMIDOR FINAL) no es un cliente.** Es el mostrador y no se le
puede llamar. Sin excluirlo, tres de las telas más paradas mostraban un único
"candidato" que compró 3 kg y no existe.

⚠ **En Asinfo el código de 3 letras vive en `empresa.nombre_comercial`**, no en
`codigo`, que es el RUC. Ese código es el mismo `codigo_cli` con el que Programa
Core joinea todo, y por eso la lista se entrega por código: se puede pegar
contra cualquier otra pantalla del programa.

⭐ **No hay corte de años.** `anio_ok` toma el último año con compras de cada
tela, sea cual sea. Dueña 17/08/2026, sobre las telas sin candidatos: "ponemelos
como improbable pero la última fecha aunque sea de 2024". Un cliente que compró
esa tela en 2023 no es un buen candidato, pero es MÁS que un renglón vacío: es
el único que alguna vez la quiso. La pantalla lo marca como improbable con su
fecha, y quien llama decide. Cortar en dos años dejaba 7 telas sin una sola
pista teniendo el dato a mano.

⚠ **El código NO queda guardado en la factura**: sale de la ficha del cliente al
consultar. Si a un cliente le cambiaron el código, toda su historia aparece con
el nuevo. Para esta lista eso juega a favor (el código es el de hoy), pero
significa que desde acá NO se puede detectar un cambio de código.
"""

from __future__ import annotations

import logging

from modules._lib import metabase_client

_LOG = logging.getLogger("programa_core.analisis")

DB_ASINFO = 2

# Categorías que no van en esta pantalla.
#
# ⚠ `TELA CRUDA` se excluye aunque HAYA kilos suyos en la bodega de producto
# terminado (2 productos, 37 kg al 17/08/2026). Dueña: "tela cruda no debería
# estar.. es producto terminado". Estar en la bodega 53 no la convierte en
# producto terminado: es tela sin teñir que pasó por ahí, y ofrecérsela a un
# cliente de la lista no tiene sentido. El filtro por bodega no alcanza —
# también hace falta el de categoría.
_CATS = ("'AUXILIARES'", "'COLORANTES'", "'SERVICIOS'", "'COMPRAS'",
         "'PRODUCTO TERMINADO ND'", "'TELA CRUDA'")
CATS = ", ".join(_CATS)

BODEGA_TERMINADO = 53
MIN_KG = 20        # debajo de esto no vale el tiempo de revisarlo
TOPE_CLIENTES = 12  # cuántos candidatos se guardan por tela

# Nombre del vendedor en Asinfo → código de `scintela.vendedor`. En Asinfo el
# vendedor viene con nombre y apellido; en Programa Core la cartera se reparte
# por estas 3 letras (`cliente.vend`). ⚠ Los nombres vienen con espacios al
# final ("Ramirez Edgar "): sin `.strip()` el mapeo falla en silencio y todos
# quedan como mostrador.
VEND_PC = {
    "Proaño Patricio": "PPR", "Lopez Felipe": "FL1", "Miranda Roberto": "RMY",
    "Quintero Jose": "JQU", "Ramirez Edgar": "EDG", "Proaño Sebastián": "SEP",
}

# El stock actual de cada producto: la última foto de `saldo_producto`.
_STOCK = f"""
WITH ult AS (
    SELECT s.id_producto, s.saldo,
           ROW_NUMBER() OVER (PARTITION BY s.id_producto ORDER BY s.fecha DESC) rn
    FROM saldo_producto s WHERE s.id_bodega = {BODEGA_TERMINADO}
),
stk AS (
    -- `categoria` es el GRUPO de producto (Fleece, Jersey, Poliester) y
    -- `subcategoria` el SUBGRUPO, que en la pantalla se llama "tela".
    SELECT p.nombre_subcategoria_producto AS subcategoria,
           RIGHT(RTRIM(p.codigo), 3)      AS color,
           MIN(p.nombre_categoria_producto) AS categoria,
           SUM(u.saldo)                   AS stock_kg
    FROM ult u JOIN producto p ON p.id_producto = u.id_producto
    WHERE u.rn = 1 AND p.nombre_categoria_producto NOT IN ({CATS})
    GROUP BY p.nombre_subcategoria_producto, RIGHT(RTRIM(p.codigo), 3)
),
ven AS (
    SELECT pr.nombre_subcategoria_producto AS subcategoria,
           RIGHT(RTRIM(pr.codigo), 3)      AS color,
           MAX(CAST(fc.fecha AS date))     AS ultima_venta,
           SUM(CASE WHEN fc.fecha >= DATEADD(month, -12, GETDATE())
                    THEN dfc.cantidad ELSE 0 END) AS kg_12m
    FROM factura_cliente fc
    JOIN detalle_factura_cliente dfc ON dfc.id_factura_cliente = fc.id_factura_cliente
    JOIN producto pr ON pr.id_producto = dfc.id_producto
    WHERE fc.id_documento IN (7, 251) AND fc.estado NOT IN (0, 1)
      AND dfc.cantidad > 0 AND RTRIM(pr.codigo) <> 'SRLG'
      AND pr.nombre_categoria_producto NOT IN ({CATS})
    GROUP BY pr.nombre_subcategoria_producto, RIGHT(RTRIM(pr.codigo), 3)
)"""

# Primera / segunda calidad. En Asinfo NO es una propiedad del producto: es el
# ATRIBUTO 2 del LOTE (`valor_atributo`: PRI = PRIMERA, SEG = SEGUNDA). Por eso
# un mismo tela × color puede tener kilos de las dos, y por eso son dos columnas
# y no una — 26 de los ítems parados tienen de las dos.
#
# ⚠ Sale de `saldo_producto_lote`, otra tabla que la del resto de la pantalla.
# Las dos cierran (333.634 vs 333.613 kg en la bodega 53, 0,006%), pero si un
# día se despegan, los kilos por calidad van a dejar de sumar el total de la
# fila. Ese es el síntoma a mirar.
_CALIDAD = f"""
, lote_ult AS (
    SELECT s.id_producto, s.id_lote, s.saldo,
           ROW_NUMBER() OVER (PARTITION BY s.id_producto, s.id_lote
                              ORDER BY s.fecha DESC) rn
    FROM saldo_producto_lote s WHERE s.id_bodega = {BODEGA_TERMINADO}
),
cal AS (
    SELECT p.nombre_subcategoria_producto AS subcategoria,
           RIGHT(RTRIM(p.codigo), 3)      AS color,
           SUM(CASE WHEN v.codigo = 'SEG' THEN 0 ELSE lu.saldo END) AS kg_primera,
           SUM(CASE WHEN v.codigo = 'SEG' THEN lu.saldo ELSE 0 END) AS kg_segunda
    FROM lote_ult lu
    JOIN producto p ON p.id_producto = lu.id_producto
    JOIN lote l     ON l.id_lote     = lu.id_lote
    LEFT JOIN valor_atributo v ON v.id_valor_atributo = l.id_valor_atributo_2
    WHERE lu.rn = 1 AND lu.saldo > 0
    GROUP BY p.nombre_subcategoria_producto, RIGHT(RTRIM(p.codigo), 3)
)"""

SQL_PARADOS = _STOCK + _CALIDAD + f"""
SELECT stk.subcategoria, stk.color, stk.categoria, stk.stock_kg,
       ven.ultima_venta, ISNULL(ven.kg_12m, 0) AS kg_12m,
       ISNULL(cal.kg_primera, 0) AS kg_primera,
       ISNULL(cal.kg_segunda, 0) AS kg_segunda
FROM stk LEFT JOIN ven
  ON ven.subcategoria = stk.subcategoria AND ven.color = stk.color
LEFT JOIN cal
  ON cal.subcategoria = stk.subcategoria AND cal.color = stk.color
WHERE stk.stock_kg >= {MIN_KG} AND ISNULL(ven.kg_12m, 0) < 1
ORDER BY stk.stock_kg DESC
"""

SQL_LLAMADOS = _STOCK + f"""
, telas AS (
    SELECT DISTINCT stk.subcategoria
    FROM stk LEFT JOIN ven
      ON ven.subcategoria = stk.subcategoria AND ven.color = stk.color
    WHERE stk.stock_kg >= {MIN_KG} AND ISNULL(ven.kg_12m, 0) < 1
),
compras AS (
    SELECT pr.nombre_subcategoria_producto AS subcategoria,
           YEAR(fc.fecha)                  AS anio,
           fc.id_empresa                   AS id_cliente,
           MAX(RTRIM(e.nombre_comercial))  AS codigo,
           MAX(RTRIM(e.nombre_fiscal))     AS nombre,
           SUM(dfc.cantidad)               AS kg,
           MAX(CAST(fc.fecha AS date))     AS ultima_compra,
           COUNT(DISTINCT RIGHT(RTRIM(pr.codigo), 3)) AS colores
    FROM factura_cliente fc
    JOIN detalle_factura_cliente dfc ON dfc.id_factura_cliente = fc.id_factura_cliente
    JOIN producto pr ON pr.id_producto = dfc.id_producto
    JOIN empresa e   ON e.id_empresa   = fc.id_empresa
    JOIN telas t     ON t.subcategoria = pr.nombre_subcategoria_producto
    WHERE fc.id_documento IN (7, 251) AND fc.estado NOT IN (0, 1)
      AND dfc.cantidad > 0
      AND RTRIM(e.nombre_comercial) <> 'VPM'
    GROUP BY pr.nombre_subcategoria_producto, YEAR(fc.fecha), fc.id_empresa
),
ctx AS (
    SELECT id_empresa, provincia, vendedor,
           ROW_NUMBER() OVER (PARTITION BY id_empresa ORDER BY fecha DESC) rn
    FROM (
        SELECT fc.id_empresa, fc.fecha,
               ISNULL(pv.nombre, '(sin provincia)')        AS provincia,
               ISNULL(ve.nombre_vendedor, '(sin vendedor)') AS vendedor
        FROM factura_cliente fc
        LEFT JOIN direccion_empresa de ON de.id_direccion_empresa = fc.id_direccion_empresa
        LEFT JOIN ciudad ci    ON ci.id_ciudad = de.id_ciudad
        LEFT JOIN provincia pv ON pv.id_provincia = ci.id_provincia
        LEFT JOIN (SELECT id_factura_cliente, MIN(id_vendedor) AS id_vendedor
                   FROM v_ventas GROUP BY id_factura_cliente) vv
               ON vv.id_factura_cliente = fc.id_factura_cliente
        LEFT JOIN v_vendedor ve ON ve.id_vendedor = vv.id_vendedor
        WHERE fc.id_documento IN (7, 251) AND fc.estado NOT IN (0, 1)
    ) x
),
anio_ok AS (SELECT subcategoria, MAX(anio) AS anio FROM compras GROUP BY subcategoria),
rank AS (
    SELECT c.*, ROW_NUMBER() OVER (PARTITION BY c.subcategoria ORDER BY c.kg DESC) rn,
           COUNT(*) OVER (PARTITION BY c.subcategoria) AS clientes_total
    FROM compras c JOIN anio_ok a
      ON a.subcategoria = c.subcategoria AND a.anio = c.anio
)
SELECT r.subcategoria, r.anio, r.codigo, r.nombre, r.kg, r.ultima_compra,
       r.colores, r.clientes_total,
       ISNULL(x.provincia, '(sin provincia)') AS provincia,
       ISNULL(x.vendedor, '(sin vendedor)')   AS vendedor
FROM rank r LEFT JOIN ctx x ON x.id_empresa = r.id_cliente AND x.rn = 1
WHERE r.rn <= {TOPE_CLIENTES}
ORDER BY r.subcategoria, r.kg DESC
"""


def _sql_vendido(desde: str) -> str:
    """Ventas por tela × color y por DÍA desde `desde`.

    Por día y no por total porque cada fila de la cohorte tiene su propia fecha
    de marcado: las viejas llevan más tiempo corrido que las nuevas y medirlas
    con la misma vara le regalaría kilos a las viejas. El volumen se mantiene
    chico solo — son telas que por definición casi no se venden.
    """
    return f"""
SELECT pr.nombre_subcategoria_producto AS subcategoria,
       RIGHT(RTRIM(pr.codigo), 3)      AS color,
       CAST(fc.fecha AS date)          AS fecha,
       SUM(dfc.cantidad)               AS kg
FROM factura_cliente fc
JOIN detalle_factura_cliente dfc ON dfc.id_factura_cliente = fc.id_factura_cliente
JOIN producto pr ON pr.id_producto = dfc.id_producto
WHERE fc.id_documento IN (7, 251) AND fc.estado NOT IN (0, 1)
  AND dfc.cantidad > 0 AND fc.fecha >= '{desde}'
  AND pr.nombre_categoria_producto NOT IN ({CATS})
GROUP BY pr.nombre_subcategoria_producto, RIGHT(RTRIM(pr.codigo), 3),
         CAST(fc.fecha AS date)
"""


def _filas(sql: str) -> list[dict]:
    filas, ok = metabase_client.fetch_dataset_estado(DB_ASINFO, sql, max_results=20000)
    if not ok:
        # ⚠ fail-CLOSED: `fetch_dataset` devuelve [] tanto si no hay filas como
        # si Metabase se cayó. Tratar el segundo caso como "no hay parados"
        # borraría la pantalla entera y parecería una buena noticia.
        raise RuntimeError("Metabase no contestó — no se actualizó nada")
    return filas


def parados() -> list[dict]:
    return _filas(SQL_PARADOS)


def llamados() -> list[dict]:
    filas = _filas(SQL_LLAMADOS)
    for f in filas:
        f["vend_pc"] = VEND_PC.get((f.get("vendedor") or "").strip())
    return filas


def vendido_desde(desde: str) -> list[dict]:
    return _filas(_sql_vendido(desde))
