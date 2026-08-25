"""Trae de Asinfo lo que hace falta para la pantalla de LO PARADO.

Dos consultas y un remate:

    parados()   ~2 s — lo que entra a la lista: el producto terminado que no
                vendió un kilo en 12 meses y que ADEMÁS está en la bodega desde
                hace más de 6 meses (todos sus kilos) MÁS los kilos de segunda
                de cualquier tela, se venda o no.
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

⭐ **Sólo la tela QUIETA, no la recién hecha.** Dueña 25/08/2026: *"que la
competencia tenga solo tela estancada, no tela que se hizo recientemente y no se
movió"*. "No vendió un kilo en 12 meses" se cumple solo cuando la tela recién
salió de producción: nunca tuvo la chance de venderse. Por eso al filtro de
ventas se le suma el de ANTIGÜEDAD — el rollo tiene que estar en la bodega desde
hace más de `MESES_QUIETO` meses (ver `_QUIETO`).

⭐ **La de SEGUNDA entra igual, se haya hecho cuando se haya hecho** (dueña
25/08/2026, sobre la misma pregunta: *"sí, la segunda siempre entra"*). La
antigüedad decide sólo el motivo `parado`. Un ítem cuyo stock es reciente pero
que tiene kilos SEG entra con esos kilos y nada más, igual que cualquier tela
que se vende bien y arrastra segunda.

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

# ⭐ Cuánto tiene que llevar un rollo en la bodega para que su tela cuente como
# ESTANCADA. Elegido por la dueña el 25/08/2026 entre 3, 6 y 12 meses: "6
# meses". Medio año quieto ya es tela clavada, y deja afuera la producción
# fresca sin vaciar la lista.
#
# ⚠ Cambiarlo mueve la lista, la meta de la competencia y el puntaje de cada
# tela. Es una línea, pero no es un detalle.
MESES_QUIETO = 6

# Nombre del vendedor en Asinfo → código de `scintela.vendedor`. En Asinfo el
# vendedor viene con nombre y apellido; en Programa Core la cartera se reparte
# por estas 3 letras (`cliente.vend`). ⚠ Los nombres vienen con espacios al
# final ("Ramirez Edgar "): sin `.strip()` el mapeo falla en silencio y todos
# quedan como mostrador.
VEND_PC = {
    "Proaño Patricio": "PPR", "Lopez Felipe": "FL1", "Miranda Roberto": "RMY",
    "Quintero Jose": "JQU", "Ramirez Edgar": "EDG", "Proaño Sebastián": "SEP",
}

# ⭐ "Intela" en vez de "mostrador". Dueña 17/08/2026: "intela/mostrador como un
# vendedor extra. llamalo Intela mas que mostrador". Se juntan los dos casos que
# significan lo mismo —la venta la hizo la casa—: la factura con vendedor
# "Cía. Ltda. Intela" y la que no tiene vendedor asignado. Dejarlos separados
# partía el 51,3% de las ventas en dos filas que nadie sabe leer.
# ⭐ Las DEVOLUCIONES restan (dueña 24/08/2026: "dale contemos devoluciones").
# En Asinfo la factura es el documento 7 o 251 y la nota de crédito el 20 o el
# 451 — el módulo de inventario rotativo ya lo hacía así. Sin esto un vendedor
# facturaba un saldo, sumaba los puntos, el cliente devolvía la tela y los
# puntos quedaban.
#
# ⚠ La nota de crédito se imputa al día de la factura MADRE
# (`id_factura_cliente_padre`), no al suyo: si no, una devolución de octubre de
# una venta de agosto caería fuera de la ventana y no la alcanzaría a restar.
_DOCS = "(7, 251, 20, 451)"
_KG = ("SUM(CASE WHEN fc.id_documento IN (20, 451) "
       "THEN -dfc.cantidad ELSE dfc.cantidad END)")
# ⚠ La fecha de la madre se usa SÓLO para la nota de crédito. Con un
# COALESCE pelado, una factura común que por cualquier motivo tuviera padre
# cargado se mudaría a la fecha del padre y podría salirse de la ventana de la
# competencia. Hoy no hay ninguna (0 de 195 el 21/08/2026), y por eso mismo
# conviene que no dependa de que siga siendo cierto.
_FECHA = ("CASE WHEN fc.id_documento IN (20, 451) "
          "THEN COALESCE(pad.fecha, fc.fecha) ELSE fc.fecha END")
_JOIN_PADRE = ("LEFT JOIN factura_cliente pad "
               "ON pad.id_factura_cliente = fc.id_factura_cliente_padre")

# ⭐ La CALIDAD de un kilo VENDIDO. Está en el atributo 2 de la LÍNEA de
# factura: `id_valor_atributo_2` vale 3 = PRI y 4 = SEG (verificado contra
# `valor_atributo` el 24/08/2026).
#
# ⚠ NO se puede sacar del lote como en el stock: en las líneas de factura
# `dfc.id_lote` viene en NULL. Son dos caminos distintos para el mismo dato y
# éste es el único que existe del lado de la venta.
# ⚠ En las líneas de NOTA DE CRÉDITO el atributo viene en NULL (verificado el
# 24/08/2026: 17 de 17). Sin buscarlo en la línea madre, la devolución de un
# kilo SEG se etiquetaba PRI, y en un ítem que entró sólo por su segunda eso la
# dejaba con `cuenta = false`: la venta sumaba los puntos y la devolución no los
# restaba. El fallback quedaba para el lado equivocado.
_JOIN_MADRE_LINEA = """
OUTER APPLY (SELECT TOP 1 m.id_valor_atributo_2 AS va2
               FROM detalle_factura_cliente m
              WHERE m.id_factura_cliente = fc.id_factura_cliente_padre
                AND m.id_producto = dfc.id_producto) mad"""
# TOP 1 y no un JOIN: la madre puede tener el mismo producto en dos renglones
# y un JOIN duplicaría los kilos de la devolución.
_CALIDAD_LINEA = ("CASE WHEN COALESCE(dfc.id_valor_atributo_2, mad.va2) = 4 "
                  "THEN 'SEG' ELSE 'PRI' END")

_VENDEDOR = ("CASE WHEN vv.nombre_vendedor IS NULL "
             "OR RTRIM(vv.nombre_vendedor) IN ('Cía. Ltda. Intela', '') "
             "THEN 'Intela' ELSE RTRIM(vv.nombre_vendedor) END")

# ⚠ El vendedor se busca por la factura MADRE cuando es una nota de crédito.
# Joineando por el id de la NC, `v_ventas` no la tiene y el CASE de arriba la
# manda a 'Intela': el kilo positivo le sumaba al vendedor que lo facturó y el
# negativo se le restaba al mostrador, que también compite. El vendedor se
# quedaba con los puntos de una tela que volvió.
_JOIN_VENDEDOR = """
LEFT JOIN (SELECT id_factura_cliente, MIN(id_vendedor) AS id_vendedor
           FROM v_ventas GROUP BY id_factura_cliente) vx
       ON vx.id_factura_cliente = COALESCE(fc.id_factura_cliente_padre,
                                           fc.id_factura_cliente)
LEFT JOIN v_vendedor vv ON vv.id_vendedor = vx.id_vendedor"""


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
    -- ⚠ ACÁ las devoluciones NO netean, y es a propósito: esta consulta decide
    -- quién ENTRA a la lista, y "no salió en 12 meses" tiene que significar que
    -- la tela no se movió. Una que salió y volvió sí se movió. Lo que sí netea
    -- es lo que PUNTÚA (`_sql_vendido`) y la dificultad de cada tela
    -- (`SQL_VENTA_TELA`).
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
-- ⭐ LOS ROLLOS QUE YA ESTABAN. Un rollo es VIEJO si la bodega lo tenía en su
-- foto antes del corte de {MESES_QUIETO} meses. `saldo_producto_lote` es una
-- foto DIARIA por rollo, así que si el rollo existía ese día, tiene una fila
-- ese día: no hace falta la fecha de fabricación, que en Asinfo no está en el
-- lote.
--
-- ⚠ Se pregunta por EXISTENCIA antes del corte y no por `MIN(fecha)`: el MIN
-- obliga a recorrer la historia entera de la bodega, y esto se conforma con las
-- filas viejas. Da lo mismo si el rollo entró, salió y volvió: lo que importa
-- es que estos kilos no son producción fresca.
, viejo AS (
    SELECT DISTINCT id_producto, id_lote
    FROM saldo_producto_lote
    WHERE id_bodega = {BODEGA_TERMINADO} AND saldo > 0
      AND fecha < DATEADD(month, -{MESES_QUIETO}, GETDATE())
),
-- Desde cuándo hay foto de esta bodega. Es el CONTROL de la regla de arriba:
-- si la foto no llega hasta el corte, "no estaba hace {MESES_QUIETO} meses" no
-- significa que la tela sea nueva, significa que no se sabe — y en ese caso no
-- se filtra nada (ver `_QUIETO`). Sin este control, un día que Asinfo purgue
-- historia la lista entera se vaciaría sola y parecería una buena noticia.
hist AS (
    SELECT MIN(fecha) AS desde FROM saldo_producto_lote
    WHERE id_bodega = {BODEGA_TERMINADO}
),
lote_ult AS (
    SELECT s.id_producto, s.id_lote, s.saldo,
           ROW_NUMBER() OVER (PARTITION BY s.id_producto, s.id_lote
                              ORDER BY s.fecha DESC) rn
    FROM saldo_producto_lote s WHERE s.id_bodega = {BODEGA_TERMINADO}
),
cal AS (
    SELECT p.nombre_subcategoria_producto AS subcategoria,
           RIGHT(RTRIM(p.codigo), 3)      AS color,
           SUM(CASE WHEN v.codigo = 'SEG' THEN 0 ELSE lu.saldo END) AS kg_primera,
           SUM(CASE WHEN v.codigo = 'SEG' THEN lu.saldo ELSE 0 END) AS kg_segunda,
           -- ⭐ TUBULAR o ABIERTA (dueña 25/08/2026: "agregar si es tubular o
           -- abierta"). Los kilos NO se separan —siguen sumados en una sola
           -- fila— pero la hoja tiene que decir de qué forma es, que es lo
           -- primero que pregunta el cliente por teléfono.
           -- ⚠ Abiertas por CALIDAD además de por forma. Un ítem que entró a
           -- la lista sólo por su segunda muestra únicamente sus kilos SEG, así
           -- que su forma tiene que contar sobre esos mismos kilos: sumando
           -- todo, la fila diría "TUB 4.350" arriba de un stock de 833.
           SUM(CASE WHEN t.codigo = 'TUB' AND ISNULL(v.codigo, '') <> 'SEG'
                    THEN lu.saldo ELSE 0 END) AS kg_tub_pri,
           SUM(CASE WHEN t.codigo = 'TUB' AND v.codigo = 'SEG'
                    THEN lu.saldo ELSE 0 END) AS kg_tub_seg,
           SUM(CASE WHEN t.codigo = 'ABI' AND ISNULL(v.codigo, '') <> 'SEG'
                    THEN lu.saldo ELSE 0 END) AS kg_abi_pri,
           SUM(CASE WHEN t.codigo = 'ABI' AND v.codigo = 'SEG'
                    THEN lu.saldo ELSE 0 END) AS kg_abi_seg,
           -- Los kilos que llevan más de {MESES_QUIETO} meses en la bodega.
           SUM(CASE WHEN vj.id_lote IS NOT NULL THEN lu.saldo ELSE 0 END)
                                                              AS kg_viejo
    FROM lote_ult lu
    JOIN producto p ON p.id_producto = lu.id_producto
    JOIN lote l     ON l.id_lote     = lu.id_lote
    LEFT JOIN valor_atributo v ON v.id_valor_atributo = l.id_valor_atributo_2
    -- ⚠ Los slots del LOTE no siguen el número del atributo: la calidad
    -- (atributo 2) está en `id_valor_atributo_2`, pero la forma (atributo 1,
    -- TUB/ABI) está en `id_valor_atributo_3` y el COLOR (atributo 3) en el
    -- `_1`. Medido el 25/08/2026 sobre lotes de cinco telas. Por eso se filtra
    -- por el CÓDIGO del valor y no por su id: si mañana el slot cambia, la
    -- consulta deja de encontrarlo y da 0 — no confunde una cosa con otra.
    LEFT JOIN valor_atributo t ON t.id_valor_atributo = l.id_valor_atributo_3
    LEFT JOIN viejo vj ON vj.id_producto = lu.id_producto
                      AND vj.id_lote    = lu.id_lote
    WHERE lu.rn = 1 AND lu.saldo > 0
    GROUP BY p.nombre_subcategoria_producto, RIGHT(RTRIM(p.codigo), 3)
)"""

# ⭐ QUÉ ENTRA A LA LISTA. Dos motivos distintos, y conviene no confundirlos:
#
#   parado  — la tela × color entera está quieta hace 12 meses. Entran TODOS sus
#             kilos, de primera y de segunda.
#   segunda — la tela se vende bien, pero tiene kilos de SEGUNDA calidad. Dueña
#             18/08/2026: "agreguemos toda la tela de segunda a la competencia".
#             ⚠ Entran SÓLO los kilos de segunda. Esos mismos ítems tienen
#             61.272 kg de primera que salen solos: meterlos sería inflar la
#             competencia con tela que ya se vende, y la meta dejaría de
#             significar nada.
#
# Medido al 18/08/2026: 344 parados (36.720 kg) + 363 con segunda suelta
# (15.709 kg) = 707 ítems y 52.428 kg.
#
# ⭐ Y desde el 25/08/2026 el motivo `parado` pide una tercera cosa: que la tela
# esté QUIETA hace rato. Ver `_QUIETO`.

# ⭐ QUIETA: hay al menos {MIN_KG} kg de esta tela × color que ya estaban en la
# bodega antes del corte de {MESES_QUIETO} meses.
#
# ⚠ Las otras dos ramas son "no se sabe", y las dos dejan pasar a propósito. Se
# excluye sólo con PRUEBA de que la tela es reciente, nunca por falta de dato:
#
#   · `hist.desde > corte` — la foto de la bodega no llega hasta el corte
#     (Asinfo purgó historia, o la bodega es nueva). Nadie puede decir qué había
#     hace {MESES_QUIETO} meses.
#   · `cal.subcategoria IS NULL` — el ítem no tiene ni un lote en la foto por
#     rollo. Son los kilos que hoy separan a `saldo_producto` de
#     `saldo_producto_lote` (0,006% al 17/08/2026): existen en el stock pero no
#     tienen rollo con el cual fecharse.
_QUIETO = (f"(hist.desde > DATEADD(month, -{MESES_QUIETO}, GETDATE())"
           f" OR cal.subcategoria IS NULL"
           f" OR ISNULL(cal.kg_viejo, 0) >= {MIN_KG})")

_SIN_VENTA = f"(stk.stock_kg >= {MIN_KG} AND ISNULL(ven.kg_12m, 0) < 1)"
_ES_PARADO = f"({_SIN_VENTA} AND {_QUIETO})"

# ⚠ La tela RECIÉN HECHA que no vendió nada. No es una tela parada: nunca tuvo
# la chance de venderse. Sale marcada —y no filtrada en el WHERE— porque el
# refresh necesita verla para APAGAR de la lista a la que ya había entrado por
# este motivo antes del 25/08 (ver `queries.actualizar`).
_ES_NUEVA = f"(CASE WHEN {_SIN_VENTA} AND NOT {_QUIETO} THEN 1 ELSE 0 END)"

SQL_PARADOS = _STOCK + _CALIDAD + f"""
SELECT stk.subcategoria, stk.color, stk.categoria,
       CASE WHEN {_ES_PARADO} THEN stk.stock_kg
            ELSE ISNULL(cal.kg_segunda, 0) END      AS stock_kg,
       ven.ultima_venta, ISNULL(ven.kg_12m, 0)      AS kg_12m,
       CASE WHEN {_ES_PARADO} THEN ISNULL(cal.kg_primera, 0)
            ELSE 0 END                              AS kg_primera,
       ISNULL(cal.kg_segunda, 0)                    AS kg_segunda,
       -- Los kilos por forma van sobre el MISMO universo que `stock_kg`: todo
       -- si la tela × color está parada entera, sólo la segunda si entró por
       -- eso. Así kg_tubular + kg_abierta suma stock_kg y las dos líneas de la
       -- hoja cierran con el total de la fila.
       CASE WHEN {_ES_PARADO}
            THEN ISNULL(cal.kg_tub_pri, 0) + ISNULL(cal.kg_tub_seg, 0)
            ELSE ISNULL(cal.kg_tub_seg, 0) END      AS kg_tubular,
       CASE WHEN {_ES_PARADO}
            THEN ISNULL(cal.kg_abi_pri, 0) + ISNULL(cal.kg_abi_seg, 0)
            ELSE ISNULL(cal.kg_abi_seg, 0) END      AS kg_abierta,
       -- Las cuatro combinaciones, para que la hoja pueda abrir el color en
       -- una línea por forma Y calidad. En un ítem que entró sólo por su
       -- segunda las dos de primera dan 0 solas: esos kilos no están en la
       -- lista.
       CASE WHEN {_ES_PARADO} THEN ISNULL(cal.kg_tub_pri, 0) ELSE 0 END AS kg_tub_pri,
       ISNULL(cal.kg_tub_seg, 0)                    AS kg_tub_seg,
       CASE WHEN {_ES_PARADO} THEN ISNULL(cal.kg_abi_pri, 0) ELSE 0 END AS kg_abi_pri,
       ISNULL(cal.kg_abi_seg, 0)                    AS kg_abi_seg,
       CASE WHEN {_ES_PARADO} THEN 'parado' ELSE 'segunda' END AS motivo,
       -- 1 = tela recién hecha que todavía no vendió nada. Con kilos de
       -- segunda entra igual (por su SEG); sin ellos, `stock_kg` da 0 y el
       -- refresh la deja afuera.
       {_ES_NUEVA}                                  AS nueva,
       ISNULL(cal.kg_viejo, 0)                      AS kg_viejo,
       -- Lo que hay en la bodega, sin importar por qué motivo entró ni si
       -- entró. Es con lo que la pantalla puede decir cuántos kilos quedaron
       -- afuera por ser tela reciente.
       stk.stock_kg                                 AS stock_bodega
FROM stk
CROSS JOIN hist
LEFT JOIN ven
  ON ven.subcategoria = stk.subcategoria AND ven.color = stk.color
LEFT JOIN cal
  ON cal.subcategoria = stk.subcategoria AND cal.color = stk.color
WHERE {_SIN_VENTA} OR ISNULL(cal.kg_segunda, 0) > 0
ORDER BY 4 DESC
"""

# ⚠ Los CANDIDATOS no miran la antigüedad, y no hace falta: son clientes por
# TELA, y una tela sólo se muestra con sus candidatos al lado si está en la
# lista. Sumarle acá el filtro de antigüedad obligaría a traer también
# la foto por rollo a esta consulta —la más cara de las tres— para no cambiar ni
# un renglón de lo que se ve.
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
       CAST({_FECHA} AS date)          AS fecha,
       {_VENDEDOR}                     AS vendedor,
       {_CALIDAD_LINEA}                AS calidad,
       {_KG}                           AS kg
FROM factura_cliente fc
JOIN detalle_factura_cliente dfc ON dfc.id_factura_cliente = fc.id_factura_cliente
JOIN producto pr ON pr.id_producto = dfc.id_producto
{_JOIN_PADRE}{_JOIN_MADRE_LINEA}
{_JOIN_VENDEDOR}
WHERE fc.id_documento IN {_DOCS} AND fc.estado NOT IN (0, 1)
  AND dfc.cantidad > 0 AND {_FECHA} >= '{desde}'
  AND pr.nombre_categoria_producto NOT IN ({CATS})
GROUP BY pr.nombre_subcategoria_producto, RIGHT(RTRIM(pr.codigo), 3),
         CAST({_FECHA} AS date), {_VENDEDOR}, {_CALIDAD_LINEA}
"""


SQL_VENTA_TELA = f"""
-- Cuántos kilos vendió la fábrica de cada TELA en los últimos 12 meses. Es lo
-- único que hace falta para saber si esa tela es fácil o difícil de colocar:
-- los kilos parados divididos por lo que se vende al mes dan "cuántos meses de
-- venta hay quietos", y de ahí sale el puntaje.
--
-- ⚠ Por TELA, sin color, igual que los candidatos. El color se negocia; medir
-- la dificultad por tela × color daría 732 números que nadie puede recordar y
-- un color raro de una tela que sale bien quedaría marcado como imposible.
-- ⭐ Acá las devoluciones SÍ netean: si una tela se vendió y volvió, no se
-- vendió. Con la devolución adentro la tela parece más fácil de colocar de lo
-- que es y se le da MENOS puntaje del que corresponde.
-- ⚠ La SEG va aparte: es lo que hace falta para saber qué tan común es vender
-- segunda y poder ponerle puntaje propio (dueña 24/08/2026).
SELECT pr.nombre_subcategoria_producto AS subcategoria,
       {_KG}                           AS kg_12m,
       SUM(CASE WHEN COALESCE(dfc.id_valor_atributo_2, mad.va2) = 4
                THEN CASE WHEN fc.id_documento IN (20, 451)
                          THEN -dfc.cantidad ELSE dfc.cantidad END
                ELSE 0 END)            AS kg_seg_12m,
       COUNT(DISTINCT fc.id_empresa)   AS clientes_12m
FROM factura_cliente fc
JOIN detalle_factura_cliente dfc ON dfc.id_factura_cliente = fc.id_factura_cliente
JOIN producto pr ON pr.id_producto = dfc.id_producto
{_JOIN_PADRE}{_JOIN_MADRE_LINEA}
WHERE fc.id_documento IN {_DOCS} AND fc.estado NOT IN (0, 1)
  AND dfc.cantidad > 0 AND RTRIM(pr.codigo) <> 'SRLG'
  AND {_FECHA} >= DATEADD(month, -12, GETDATE())
  AND pr.nombre_categoria_producto NOT IN ({CATS})
GROUP BY pr.nombre_subcategoria_producto
"""


SQL_SHARE = f"""
-- Cuánto vende cada vendedor de cada GRUPO. Es lo que reparte la meta: sin
-- esto, el ranking lo gana el de cartera más grande todos los meses.
-- Ventana: los últimos 12 meses. Más atrás mezcla carteras que ya cambiaron de
-- dueño; menos, y un mes flojo desfigura el reparto.
SELECT pr.nombre_categoria_producto AS categoria,
       {_VENDEDOR}                  AS vendedor,
       SUM(dfc.cantidad)            AS kg
FROM factura_cliente fc
JOIN detalle_factura_cliente dfc ON dfc.id_factura_cliente = fc.id_factura_cliente
JOIN producto pr ON pr.id_producto = dfc.id_producto
{_JOIN_VENDEDOR}
WHERE fc.id_documento IN (7, 251) AND fc.estado NOT IN (0, 1)
  AND dfc.cantidad > 0
  AND fc.fecha >= DATEADD(month, -12, GETDATE())
  AND pr.nombre_categoria_producto NOT IN ({CATS})
GROUP BY pr.nombre_categoria_producto, {_VENDEDOR}
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
    """Lo que hay en la bodega para la lista, con dos banderas ya resueltas.

    `nueva`  — el stock de esa tela × color es producción reciente: no vendió
               nada en 12 meses porque hace menos de `MESES_QUIETO` meses que
               existe. No es tela estancada.
    `entra`  — si va a la lista. Una tela nueva CON kilos de segunda entra por
               esos kilos (`motivo = 'segunda'`); sin ellos no entra, y sale con
               `stock_kg` en 0 porque no tiene ni un kilo que mostrar.

    Las dos vienen marcadas y no filtradas: el refresh necesita ver también las
    que NO entran para poder apagar de la lista a las que ya estaban antes de
    que existiera esta regla.
    """
    filas = _filas(SQL_PARADOS)
    for f in filas:
        f["nueva"] = bool(int(f.get("nueva") or 0))
        f["entra"] = float(f.get("stock_kg") or 0) > 0
    return filas


def llamados() -> list[dict]:
    filas = _filas(SQL_LLAMADOS)
    for f in filas:
        f["vend_pc"] = VEND_PC.get((f.get("vendedor") or "").strip())
    return filas


def vendido_desde(desde: str) -> list[dict]:
    filas = _filas(_sql_vendido(desde))
    for f in filas:
        f["vend_pc"] = VEND_PC.get((f.get("vendedor") or "").strip())
    return filas


def venta_por_tela() -> dict[str, dict]:
    """Kilos vendidos en los últimos 12 meses, por tela.

    `{tela: {"kg": total, "seg": kilos de SEGUNDA}}`, los dos NETOS de
    devoluciones. La SEG va aparte para poder medir qué tan común es venderla.
    """
    return {f["subcategoria"]: {"kg": float(f["kg_12m"] or 0),
                                "seg": float(f.get("kg_seg_12m") or 0)}
            for f in _filas(SQL_VENTA_TELA) if f.get("subcategoria")}


def share_por_grupo() -> list[dict]:
    """El % que cada vendedor pesa dentro de cada grupo. Suma 100 por grupo."""
    filas = _filas(SQL_SHARE)
    tot: dict[str, float] = {}
    for f in filas:
        tot[f["categoria"]] = tot.get(f["categoria"], 0) + float(f["kg"] or 0)
    for f in filas:
        t = tot.get(f["categoria"]) or 1
        f["pct"] = 100 * float(f["kg"] or 0) / t
        f["vend_pc"] = VEND_PC.get((f.get("vendedor") or "").strip())
    return filas
