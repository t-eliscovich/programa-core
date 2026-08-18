"""Pedidos de cliente pendientes (Asinfo) cruzados con inventario y producción.

Contesta la pregunta que la dueña hace todos los días: *de lo que los clientes
pidieron y todavía no se despachó, ¿qué tenemos, qué se está tinturando y qué
falta?* — abierto por familia de tela, tela y color.

Fuente única: Asinfo vía Metabase (db 2). Programa Core no guarda pedidos.

    v_saldos_comprometidos_detallado  → lo pedido y no despachado
    saldo_producto (bodega 53)        → lo que hay en bodega
    orden_fabricacion (bodega 53)     → lo que se está tinturando

Todo fail-soft: si Metabase no contesta, las funciones devuelven listas vacías
con `disponible=False` y la pantalla muestra un cartel. Nunca levanta.

## Las tres trampas de la fuente (verificadas 2026-08-17)

1. **Las órdenes de fabricación se cuentan DOBLE.** Cada orden vive en dos
   capas: un PADRE con el total y `id_producto` NULL, e HIJAS con el detalle
   por color. `SUM(cantidad)` sobre la tabla cruda suma las dos. Nos quedamos
   con las HOJAS (las que no son padre de nadie) y que tengan producto — que
   incluye a las órdenes "solas", sin hijas.

2. **`estado_produccion = 0` no es "programado", es abandonado.** Las 894
   hijas en estado 0 cuelgan todas de un padre también en 0, promedian 660
   días de antigüedad y NINGUNA tiene un solo kilo fabricado. Son 209.516 kg
   de +60 días. Si entran a la cuenta, nunca falta nada. Contamos sólo estado
   2 (lanzada) con menos de `DIAS_PRODUCCION_VIVA` días — y aún así el estado 2
   arrastra 216 hojas de +180 días (23.121 kg) que el filtro de edad saca.

   El contraste que lo decidió: formulas_app (donde los operarios cargan los
   baños de verdad) dice 80.913 kg sin terminar de ≤15 días, contra los 73.801
   de Asinfo estado 2 ≤15 días. Coinciden dentro del 9%, y formulas_app no
   tiene NADA sin terminar de +60 días.

3. **El pedido y la factura no están en la misma unidad.** El pedido se toma
   en rollos, kilos o unidades; la factura sale SIEMPRE en kilos.
   `detalle_factura_cliente.peso` existe pero está en 0 siempre — no sirve.
   Ver `KG_POR_ROLLO` abajo.
"""
from __future__ import annotations

import logging
from datetime import date

from modules._lib import metabase_client

_LOG = logging.getLogger("programa_core.pedidos")

ASINFO_DB = 2

#: Bodega de Producto Terminado en Asinfo. Los pedidos de cliente salen todos
#: de acá (verificado: 1.218 de 1.218 líneas pendientes).
BODEGA_TERMINADO = 53

#: Un rollo son 23,5 kg. `detalle_pedido_cliente.valor_conversion` trae
#: 1/23,5 en 111.221 de 111.720 líneas, pero tiene 7 valores distintos y en 72
#: filas viene INVERTIDO (23,5 en vez de 1/23,5). Por eso el plano, y por eso
#: la pantalla dice que el kilo del pedido es estimado: el peso real recién
#: existe cuando se pesa el rollo al despacharlo.
KG_POR_ROLLO = 23.5

#: Unidades de medida de Asinfo (tabla `unidad`).
UNIDAD_UN, UNIDAD_KG, UNIDAD_ROLLO = 1, 2, 51

#: "Ahora" en hora de Ecuador. En Asinfo `GETDATE()` devuelve UTC mientras las
#: fechas se graban en hora local: usarlo pelado adelanta el día cinco horas y
#: a partir de las 19:00 EC toda la antigüedad se corre un día. Mismo patrón
#: que `_AHORA_EC` en modules/asinfo/despacho_sin_factura.py.
AHORA_EC = "DATEADD(hour, -5, GETDATE())"

#: Pedidos más viejos que esto no entran al total (decisión dueña 2026-08-17:
#: al 17/08 eran 6 pedidos / 1.774 kg, de los cuales 1.596 kg de uno solo).
#: Siguen consultables por la ruta de detalle.
DIAS_PEDIDO_MAX = 90

#: Una orden de fabricación lanzada hace más que esto no cuenta como "en
#: producción". Ver la trampa 2 del docstring.
DIAS_PRODUCCION_VIVA = 30

#: Familias que son tela vendible. El resto de `nombre_categoria_producto`
#: (TELA CRUDA, HILO, COLORANTES, AUXILIARES, COMPRAS, SERVICIOS) son insumos
#: y no se piden.
CATEGORIAS = (
    "Jersey", "Fleece", "Rib", "Poliester", "Pique",
    "Lycra", "Toper", "Franela", "Boston", "Cuellos", "Puños",
)

#: Las que se piden por unidad y no por peso. La pantalla las muestra en
#: unidades como número principal.
CATEGORIAS_EN_UNIDADES = ("Cuellos", "Puños")


# `conv`: cuántas unidades entran en un kilo, por producto. Sale del valor más
# repetido de `valor_conversion` en las líneas de pedido en unidad — 33,33 para
# cuellos, 50 para puños. Se saca de la data y no se hardcodea porque es una
# propiedad del producto (un cuello 40 pesa distinto que un cuello 34) y al
# 17/08 cubre el 100% de las líneas pendientes en unidades.
_SQL_PENDIENTES = """
WITH conv AS (
    SELECT id_producto, valor_conversion,
           ROW_NUMBER() OVER (PARTITION BY id_producto
                              ORDER BY COUNT(*) DESC, valor_conversion DESC) AS rn
      FROM detalle_pedido_cliente
     WHERE id_unidad_venta = {un} AND valor_conversion > 1
     GROUP BY id_producto, valor_conversion
),
ped AS (
    SELECT v.id_producto,
           SUM(CASE WHEN v.id_unidad_pedido = {rollo}
                    THEN v.saldo_comprometido * {kg_rollo}
                    WHEN v.id_unidad_pedido = {kg} THEN v.saldo_comprometido
                    ELSE 0 END)                                    AS ped_kg,
           SUM(CASE WHEN v.id_unidad_pedido = {rollo}
                    THEN v.saldo_comprometido ELSE 0 END)          AS ped_rollos,
           SUM(CASE WHEN v.id_unidad_pedido = {un}
                    THEN v.saldo_comprometido ELSE 0 END)          AS ped_un,
           COUNT(DISTINCT v.numero)                                AS n_pedidos,
           COUNT(DISTINCT v.cliente)                               AS n_clientes,
           MIN(v.fecha)                                            AS mas_viejo
      FROM v_saldos_comprometidos_detallado v
     WHERE DATEDIFF(day, v.fecha, {ahora}) <= {dias_ped}
     GROUP BY v.id_producto
),
inv AS (
    SELECT id_producto, SUM(saldo) AS inv_kg
      FROM (SELECT id_producto, saldo,
                   ROW_NUMBER() OVER (PARTITION BY id_producto, id_bodega
                                      ORDER BY fecha DESC, id_saldo_producto DESC) AS rn
              FROM saldo_producto
             WHERE id_bodega = {bod}) u
     WHERE u.rn = 1 AND u.saldo > 0
     GROUP BY id_producto
),
padres AS (
    SELECT DISTINCT id_orden_fabricacion_padre AS p
      FROM orden_fabricacion
     WHERE id_orden_fabricacion_padre IS NOT NULL
),
prod AS (
    SELECT o.id_producto,
           SUM(o.cantidad - ISNULL(o.cantidad_fabricada, 0)) AS prod_kg,
           COUNT(*)                                          AS n_ordenes
      FROM orden_fabricacion o
      LEFT JOIN padres h ON h.p = o.id_orden_fabricacion
     WHERE o.id_bodega = {bod}
       AND o.estado_produccion = 2
       AND h.p IS NULL
       AND o.id_producto IS NOT NULL
       AND DATEDIFF(day, o.fecha, {ahora}) <= {dias_prod}
     GROUP BY o.id_producto
)
SELECT pr.nombre_categoria_producto                    AS categoria,
       ISNULL(sub.nombre, 'Sin tela')                  AS tela,
       pr.codigo                                       AS codigo,
       {color}                                         AS color,
       ped.ped_kg, ped.ped_rollos, ped.ped_un,
       c.valor_conversion                              AS un_por_kg,
       ped.n_pedidos, ped.n_clientes, ped.mas_viejo,
       ISNULL(inv.inv_kg, 0)                           AS inv_kg,
       ISNULL(prod.prod_kg, 0)                         AS prod_kg,
       ISNULL(prod.n_ordenes, 0)                       AS n_ordenes
  FROM ped
  JOIN producto pr ON pr.id_producto = ped.id_producto
  LEFT JOIN subcategoria_producto sub
         ON sub.id_subcategoria_producto = pr.id_subcategoria_producto
  LEFT JOIN conv c ON c.id_producto = ped.id_producto AND c.rn = 1
  LEFT JOIN inv  ON inv.id_producto = ped.id_producto
  LEFT JOIN prod ON prod.id_producto = ped.id_producto
"""


#: El color es el ÚLTIMO token del nombre del producto, no "el nombre menos
#: el de la subcategoría": ese REPLACE fallaba cada vez que las dos etiquetas
#: no coincidían, que pasa seguido. `JE23RML` se llama "Jersey 1.2x2.3 RML"
#: pero su subcategoría es "Jersey 2.3" (no matcheaba nada → salía el nombre
#: entero), y `JE30CIR` es "Jersey 3.0 CIR" contra "Jersey 3" (matcheaba de
#: más → salía ".0 CIR"). Verificado sobre 10 productos de 8 telas distintas.
SQL_COLOR = (
    "REVERSE(LEFT(REVERSE(LTRIM(RTRIM(pr.nombre))), "
    "CHARINDEX(' ', REVERSE(LTRIM(RTRIM(pr.nombre))) + ' ') - 1))"
)


def _sql_pendientes() -> str:
    return _SQL_PENDIENTES.format(
        un=UNIDAD_UN, kg=UNIDAD_KG, rollo=UNIDAD_ROLLO,
        kg_rollo=KG_POR_ROLLO, bod=BODEGA_TERMINADO,
        dias_ped=DIAS_PEDIDO_MAX, dias_prod=DIAS_PRODUCCION_VIVA,
        ahora=AHORA_EC, color=SQL_COLOR,
    )


_MESES = ("ene", "feb", "mar", "abr", "may", "jun",
          "jul", "ago", "sep", "oct", "nov", "dic")


def _fecha_corta(iso: str) -> str:
    """'2026-08-05' → '5 ago'. La dueña lee la fecha, no el ISO."""
    try:
        d = date.fromisoformat(iso[:10])
    except (TypeError, ValueError):
        return ""
    return f"{d.day} {_MESES[d.month - 1]}"


def _dias_desde(iso: str, hoy: date | None = None) -> int:
    """Días entre una fecha ISO y hoy en Ecuador. 0 si la fecha no se entiende."""
    try:
        d = date.fromisoformat(iso[:10])
    except (TypeError, ValueError):
        return 0
    from filters import today_ec
    return max(0, ((hoy or today_ec()) - d).days)


def _f(valor) -> float:
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def _fila(row: dict) -> dict:
    """Una fila cruda de Asinfo → el dict que consume el template.

    `pedido_kg` unifica las tres unidades a kilos para que la resta tenga
    sentido: los rollos ya vienen multiplicados por {KG_POR_ROLLO} desde el
    SQL, y las unidades se dividen acá por las unidades-por-kilo del producto.
    """
    categoria = str(row.get("categoria") or "").strip()
    ped_un = _f(row.get("ped_un"))
    un_por_kg = _f(row.get("un_por_kg"))
    kg_de_unidades = ped_un / un_por_kg if (ped_un and un_por_kg) else 0.0

    mas_viejo = str(row.get("mas_viejo") or "")[:10]
    pedido_kg = _f(row.get("ped_kg")) + kg_de_unidades
    inv_kg = _f(row.get("inv_kg"))
    prod_kg = _f(row.get("prod_kg"))
    return {
        "categoria": categoria,
        "tela": str(row.get("tela") or "").strip(),
        "codigo": str(row.get("codigo") or "").strip(),
        "color": str(row.get("color") or "").strip(),
        "pedido_kg": round(pedido_kg, 1),
        "pedido_rollos": round(_f(row.get("ped_rollos")), 1),
        "pedido_un": round(ped_un, 1),
        "un_por_kg": round(un_por_kg, 4),
        "en_unidades": categoria in CATEGORIAS_EN_UNIDADES,
        "n_pedidos": int(_f(row.get("n_pedidos"))),
        "n_clientes": int(_f(row.get("n_clientes"))),
        "mas_viejo": mas_viejo,
        "mas_viejo_es": _fecha_corta(mas_viejo),
        "dias_espera": _dias_desde(mas_viejo),
        "inventario_kg": round(inv_kg, 1),
        "produccion_kg": round(prod_kg, 1),
        "n_ordenes": int(_f(row.get("n_ordenes"))),
        "faltan_kg": round(pedido_kg - inv_kg - prod_kg, 1),
    }


def pendientes() -> tuple[list[dict], bool]:
    """Todos los colores con pedido pendiente. Devuelve `(filas, disponible)`.

    `disponible=False` significa que Metabase no contestó — distinto de que no
    haya pedidos. La pantalla necesita distinguirlos para no mostrar "no falta
    nada" cuando en realidad no pudo preguntar.
    """
    filas, ok = metabase_client.fetch_dataset_estado(ASINFO_DB, _sql_pendientes())
    if not ok:
        _LOG.warning("pedidos: Asinfo no contestó")
        return [], False
    out = [_fila(r) for r in filas]
    return [f for f in out if f["categoria"] in CATEGORIAS], True


def total_en_unidad(kilos: float, unidad: str, en_unidades: bool,
                    un_por_kg: float = 0.0) -> tuple[float, int, str]:
    """Un total en kilos leído en la unidad pedida.

    Devuelve `(valor, decimales, etiqueta)`. Para los subtotales de tela y de
    familia, que sólo existen en kilos y por eso sí se convierten.
    """
    if unidad != "alt":
        return round(kilos), 0, "kg"
    if en_unidades:
        if not un_por_kg:
            return round(kilos), 0, "kg"
        return round(kilos * un_por_kg), 0, "un"
    # Rollos ENTEROS (dueña 2026-08-18: "no puede haber medios"). Un pedido o
    # faltante positivo nunca se muestra como 0 rollos: medio rollo sube a 1.
    r = round(kilos / KG_POR_ROLLO)
    if kilos > 0 and r == 0:
        r = 1
    return int(r), 0, "roll"


def por_categoria(filas: list[dict]) -> list[dict]:
    """Resumen por familia de tela, para las pestañas. Ordenado por faltante."""
    acc: dict[str, dict] = {}
    for f in filas:
        b = acc.setdefault(f["categoria"], {
            "categoria": f["categoria"], "colores": 0, "pedido_kg": 0.0,
            "inventario_kg": 0.0, "produccion_kg": 0.0,
            "faltan_kg": 0.0, "colores_faltan": 0,
            "pedido_un": 0.0, "pedido_rollos": 0.0,
            "en_unidades": f["en_unidades"], "un_por_kg": 0.0,
        })
        b["colores"] += 1
        b["pedido_kg"] += f["pedido_kg"]
        b["pedido_un"] += f["pedido_un"]
        b["pedido_rollos"] += f["pedido_rollos"]
        # El factor un/kg de la familia sale de un producto, no de dividir los
        # totales: los totales están redondeados y el cociente se corre lo
        # justo para que el subtotal de la tela y el de la familia difieran en
        # una unidad (459 vs 460).
        b["un_por_kg"] = b["un_por_kg"] or f["un_por_kg"]
        b["inventario_kg"] += f["inventario_kg"]
        b["produccion_kg"] += f["produccion_kg"]
        if f["faltan_kg"] > 0:
            b["faltan_kg"] += f["faltan_kg"]
            b["colores_faltan"] += 1
    for b in acc.values():
        for k in ("pedido_kg", "pedido_un", "pedido_rollos", "inventario_kg",
                  "produccion_kg", "faltan_kg"):
            b[k] = round(b[k], 1)
        # El faltante de la familia, leído en la unidad en que se pide. El
        # PEDIDO no se convierte: ya lo tenemos en rollos/unidades desde Asinfo.
        b["faltan_d"], b["dec"], b["u"] = total_en_unidad(
            b["faltan_kg"], "alt", b["en_unidades"], b["un_por_kg"])
        if b["en_unidades"]:
            b["pedido_d"] = round(b["pedido_un"])
        else:
            b["pedido_d"] = (round(b["pedido_rollos"]) if b["pedido_rollos"]
                             else round(b["pedido_kg"] / KG_POR_ROLLO))
    return sorted(acc.values(), key=lambda b: -b["faltan_kg"])


def por_tela(filas: list[dict], categoria: str,
             solo_faltan: bool = False) -> list[dict]:
    """Las filas de una familia, agrupadas por tela.

    Adentro de cada tela, los colores van ordenados por faltante descendente:
    lo que falta arriba. Es el orden que pidió la dueña para /informes/traza —
    la pantalla ordena por lo que MOVIÓ, no alfabéticamente.

    ⚠ `solo_faltan` va APAGADO por defecto y así tiene que quedarse. Probamos
    encenderlo —de 649 colores pedidos sólo 232 necesitan tela, y los otros 417
    llenaban la pantalla de filas que dicen "ok"— y la dueña lo revirtió el
    mismo día: *"tenés que mostrar todos los pedidos, no escondas los ok"*. La
    pantalla es el estado de TODO lo pedido, no una lista de tareas: ver que un
    color está cubierto es parte de lo que se viene a mirar. El filtro queda
    disponible como opción explícita (`?falta=1`), nunca como default.
    """
    acc: dict[str, dict] = {}
    for f in filas:
        if f["categoria"] != categoria:
            continue
        if solo_faltan and f["faltan_kg"] <= 0:
            continue
        b = acc.setdefault(f["tela"], {
            "tela": f["tela"], "filas": [], "pedido_kg": 0.0,
            "pedido_rollos": 0.0, "pedido_un": 0.0, "faltan_kg": 0.0,
        })
        b["filas"].append(f)
        b["pedido_kg"] += f["pedido_kg"]
        b["pedido_rollos"] += f["pedido_rollos"]
        b["pedido_un"] += f["pedido_un"]
        if f["faltan_kg"] > 0:
            b["faltan_kg"] += f["faltan_kg"]
    for b in acc.values():
        b["filas"].sort(key=lambda f: -f["faltan_kg"])
        for k in ("pedido_kg", "pedido_rollos", "pedido_un", "faltan_kg"):
            b[k] = round(b[k], 1)
    return sorted(acc.values(), key=lambda b: -b["faltan_kg"])


# El detalle usa el CÓDIGO de producto y no el id interno porque es lo que la
# dueña lee y lo que va en la URL. Se sanitiza a [A-Z0-9-] antes de interpolar:
# `fetch_dataset` no toma parámetros posicionales en el resto del código y no
# vamos a estrenar acá una firma que los fakes de los tests no declaran.
_SQL_DETALLE_PEDIDOS = """
SELECT v.numero, v.cliente, ISNULL(e.nombre_comercial, '') AS codigo_cliente,
       v.fecha, v.saldo_comprometido AS cantidad, v.id_unidad_pedido AS unidad,
       p.estado
  FROM v_saldos_comprometidos_detallado v
  JOIN producto pr ON pr.id_producto = v.id_producto
  JOIN pedido_cliente p ON p.id_pedido_cliente = v.id_pedido_cliente
  LEFT JOIN empresa e ON e.id_empresa = p.id_empresa
 WHERE pr.codigo = '{codigo}'
 ORDER BY v.fecha
"""

_SQL_DETALLE_ORDENES = """
WITH padres AS (
    SELECT DISTINCT id_orden_fabricacion_padre AS p
      FROM orden_fabricacion
     WHERE id_orden_fabricacion_padre IS NOT NULL
)
SELECT o.numero, o.fecha, o.cantidad,
       ISNULL(o.cantidad_fabricada, 0) AS fabricada,
       DATEDIFF(day, o.fecha, {ahora}) AS dias
  FROM orden_fabricacion o
  LEFT JOIN padres h ON h.p = o.id_orden_fabricacion
  JOIN producto pr ON pr.id_producto = o.id_producto
 WHERE pr.codigo = '{codigo}'
   AND o.id_bodega = {bod}
   AND o.estado_produccion = 2
   AND h.p IS NULL
   AND DATEDIFF(day, o.fecha, {ahora}) <= {dias_prod}
 ORDER BY o.fecha DESC
"""


def codigo_seguro(codigo: str) -> str:
    """Deja sólo lo que puede ser un código de producto de Asinfo.

    Los códigos son alfanuméricos con guiones (`FE96CAF`, `TC-JE-3.8-AB`). Todo
    lo demás se cae: es lo único que separa la URL de una inyección, porque el
    código se interpola en la SQL.
    """
    return "".join(
        c for c in (codigo or "").strip().upper()
        if c.isalnum() or c in "-."
    )[:40]


def detalle_color(codigo: str) -> tuple[dict | None, list[dict], list[dict], bool]:
    """`(ficha, pedidos, órdenes, disponible)` de un color.

    `ficha` es la fila de `pendientes()` de ese código — None si el color no
    tiene pedido pendiente (se puede llegar por URL a cualquier código).
    """
    seguro = codigo_seguro(codigo)
    if not seguro:
        return None, [], [], True

    filas, ok = pendientes()
    ficha = next((f for f in filas if f["codigo"] == seguro), None)

    peds, ok_ped = metabase_client.fetch_dataset_estado(
        ASINFO_DB, _SQL_DETALLE_PEDIDOS.format(codigo=seguro))
    ords, ok_ord = metabase_client.fetch_dataset_estado(
        ASINFO_DB, _SQL_DETALLE_ORDENES.format(
            codigo=seguro, bod=BODEGA_TERMINADO,
            dias_prod=DIAS_PRODUCCION_VIVA, ahora=AHORA_EC))

    return (
        ficha,
        [_fila_pedido(r) for r in peds],
        [_fila_orden(r) for r in ords],
        bool(ok and ok_ped and ok_ord),
    )


def _fila_pedido(row: dict) -> dict:
    unidad = int(_f(row.get("unidad")))
    cantidad = _f(row.get("cantidad"))
    fecha = str(row.get("fecha") or "")[:10]
    return {
        "numero": str(row.get("numero") or "").strip(),
        "cliente": str(row.get("cliente") or "").strip(),
        "codigo_cliente": str(row.get("codigo_cliente") or "").strip(),
        "fecha": fecha,
        "fecha_es": _fecha_corta(fecha),
        "dias": _dias_desde(fecha),
        "cantidad": round(cantidad, 1),
        "unidad": unidad,
        "es_rollo": unidad == UNIDAD_ROLLO,
        "es_unidad": unidad == UNIDAD_UN,
        "kg": round(cantidad * KG_POR_ROLLO, 1) if unidad == UNIDAD_ROLLO
             else (round(cantidad, 1) if unidad == UNIDAD_KG else None),
    }


def _fila_orden(row: dict) -> dict:
    cantidad = _f(row.get("cantidad"))
    fabricada = _f(row.get("fabricada"))
    fecha = str(row.get("fecha") or "")[:10]
    return {
        "numero": str(row.get("numero") or "").strip(),
        "fecha": fecha,
        "fecha_es": _fecha_corta(fecha),
        "cantidad": round(cantidad, 1),
        "fabricada": round(fabricada, 1),
        "dias": int(_f(row.get("dias"))),
        "avance": round(fabricada / cantidad * 100) if cantidad else 0,
    }


def repetidos(pedidos: list[dict]) -> list[str]:
    """Códigos de cliente que pidieron ESTE color más de una vez sin recibirlo.

    Señal de pedido atrasado que no cuesta nada calcular: si el mismo cliente
    volvió a pedir lo mismo y las dos veces siguen pendientes, lo más probable
    es que el primero nunca haya salido. Al 17/08 el caso testigo es KAM, que
    pidió FE96CAF el 05 y el 14 de agosto.
    """
    vistos: dict[str, int] = {}
    for p in pedidos:
        clave = p["codigo_cliente"] or p["cliente"]
        if clave:
            vistos[clave] = vistos.get(clave, 0) + 1
    return sorted(k for k, n in vistos.items() if n > 1)


def telas_sin_faltante(filas: list[dict], categoria: str) -> list[str]:
    """Telas de la familia donde ningún color necesita tela.

    Sólo se usa cuando el usuario pidió expresamente ver únicamente lo que
    falta (`?falta=1`): ahí las telas enteras cubiertas se nombran en una línea
    para que se sepa que existen y no parezca que desaparecieron.
    """
    con, sin = set(), set()
    for f in filas:
        if f["categoria"] != categoria:
            continue
        (con if f["faltan_kg"] > 0 else sin).add(f["tela"])
    return sorted(sin - con)


#: ⚠ La pantalla se lee SIEMPRE en la unidad en que el cliente pide — rollos
#: para las telas, unidades para cuellos y puños. Hubo un toggle kg/rollos y la
#: dueña lo sacó el mismo día: *"todo rollos, no pongamos kg"*. Los kilos se
#: siguen calculando (son la única forma de restar bodega y producción contra
#: el pedido, que vienen en kg) pero no se muestran en ningún lado.
UNIDADES = ("alt",)


def etiqueta_unidad(unidad: str, en_unidades: bool) -> str:
    if unidad != "alt":
        return "kg"
    return "un" if en_unidades else "roll"


def _factor(fila: dict, unidad: str) -> float | None:
    """Cuánto multiplicar un kilo para leerlo en la unidad pedida.

    None = ese producto no se puede expresar en la unidad alternativa (un
    cuello sin factor de conversión cargado). El template lo muestra en kg
    antes que inventar un número.
    """
    if unidad != "alt":
        return 1.0
    if fila["en_unidades"]:
        return fila["un_por_kg"] or None
    return 1.0 / KG_POR_ROLLO


def en_unidad(filas: list[dict], unidad: str) -> list[dict]:
    """Agrega a cada fila los cuatro números ya convertidos y su etiqueta.

    La conversión se hace acá y no en el template para que sea testeable y
    para que la pantalla no haga aritmética. Los kilos siguen estando en la
    fila: la unidad alternativa es una LECTURA del mismo dato, no otro dato.

    ⭐ El PEDIDO no se reconvierte: se muestra el número original que vino de
    Asinfo (`pedido_rollos` / `pedido_un`). Pasar por kilos y volver arrastra
    el redondeo — 212 unidades de C34BLA volvían como 213,3 porque sus 6,36 kg
    se habían guardado como 6,4. Bodega y producción sí se convierten, porque
    de esas sólo existen los kilos.
    """
    for f in filas:
        k = _factor(f, unidad)
        usa_alt = k is not None and unidad == "alt"
        k = k or 1.0
        # Rollos ENTEROS (dueña 2026-08-18): nada de medios. Cuellos y puños en
        # unidades enteras; kilos enteros.
        for campo, destino in (("inventario_kg", "inventario_d"),
                               ("produccion_kg", "produccion_d")):
            f[destino] = round(f[campo] * k)
        # El faltante POSITIVO nunca se muestra como 0 (medio rollo sube a 1),
        # o diría "ok" sobre algo que falta.
        fk = round(f["faltan_kg"] * k)
        if f["faltan_kg"] > 0 and fk == 0:
            fk = 1
        f["faltan_d"] = fk
        if usa_alt and f["en_unidades"]:
            f["pedido_d"] = round(f["pedido_un"])
        elif usa_alt:
            # Rollos nativos si los hay; si el pedido vino en kg (p.ej. Rib, que
            # se pide por peso) se pasa a rollos, que si no mostraría 0.
            pr = (round(f["pedido_rollos"]) if f["pedido_rollos"]
                  else round(f["pedido_kg"] / KG_POR_ROLLO))
            if f["pedido_kg"] > 0 and pr == 0:
                pr = 1
            f["pedido_d"] = pr
        else:
            f["pedido_d"] = round(f["pedido_kg"])
        f["u"] = etiqueta_unidad(unidad if usa_alt else "kg", f["en_unidades"])
        f["decimales"] = 0
    return filas


# Los pedidos de TODOS los colores en una sola consulta. Son ~1.200 líneas al
# 17/08: traerlas juntas y agruparlas en memoria sale más barato que una
# consulta por fila desplegada, y hace que la flechita abra al instante sin
# pegarle a Asinfo de nuevo.
_SQL_PEDIDOS_TODOS = """
SELECT pr.codigo, v.numero, v.cliente,
       ISNULL(e.nombre_comercial, '') AS codigo_cliente,
       v.fecha, v.saldo_comprometido AS cantidad, v.id_unidad_pedido AS unidad
  FROM v_saldos_comprometidos_detallado v
  JOIN producto pr ON pr.id_producto = v.id_producto
  JOIN pedido_cliente p ON p.id_pedido_cliente = v.id_pedido_cliente
  LEFT JOIN empresa e ON e.id_empresa = p.id_empresa
 WHERE DATEDIFF(day, v.fecha, {ahora}) <= {dias}
 ORDER BY pr.codigo, v.fecha
"""


def pedidos_por_color() -> dict[str, list[dict]]:
    """`{codigo de producto: [pedidos]}` de todo lo pendiente. {} si falla."""
    filas, ok = metabase_client.fetch_dataset_estado(
        ASINFO_DB, _SQL_PEDIDOS_TODOS.format(ahora=AHORA_EC, dias=DIAS_PEDIDO_MAX))
    if not ok:
        return {}
    out: dict[str, list[dict]] = {}
    for r in filas:
        cod = str(r.get("codigo") or "").strip()
        if cod:
            out.setdefault(cod, []).append(_fila_pedido(r))
    return out


# ── Cortes nuevos (2026-08-18): color, cliente, categoría ────────────────────
# La dueña pidió mirar lo mismo por cinco cortes, en el orden de su Excel
# "Detalle de Pedidos": Color, Tipo de tela, Acabado, Cliente, Categoría. El
# acabado no tiene fuente limpia a nivel producto en Asinfo (es atributo de
# LOTE y el pedido no tiene lote), así que queda afuera por ahora.

def nombres_color() -> dict[str, str]:
    """`{código de color de 3 letras: nombre}` del catálogo local.

    Sale de `scintela.tinto_costos` (`cod` → `color`), que el sync de colores
    de Asinfo ya mantiene. NO se le pega a Asinfo: es una lectura local barata.
    Fail-soft: `{}` si la tabla no está o la consulta falla (p.ej. vista_local
    sin catálogo cargado) — ahí el color se muestra por su código pelado.
    """
    try:
        import db
        rows = db.fetch_all(
            "SELECT UPPER(TRIM(cod)) AS cod, color FROM scintela.tinto_costos "
            "WHERE cod IS NOT NULL AND TRIM(cod) <> ''"
        )
        return {r["cod"]: (r.get("color") or "").strip()
                for r in (rows or []) if r.get("cod")}
    except Exception:  # noqa: BLE001 — fail-soft: sin catálogo, código pelado
        _LOG.warning("pedidos: no pude leer el catálogo de colores", exc_info=True)
        return {}


def por_color(filas: list[dict]) -> list[dict]:
    """Los pedidos agrupados por COLOR (código de 3 letras + nombre).

    Cada color trae sus variantes (una por producto/tela) ya con pedido, stock,
    producción y faltante en la unidad en que se pide, y el color entero se
    ordena por lo que más falta. El código es el grande y el nombre el chico
    (pedido de la dueña 2026-08-18).
    """
    nombres = nombres_color()
    acc: dict[str, list[dict]] = {}
    for f in filas:
        acc.setdefault(f["color"], []).append(f)
    out = []
    for cod, fs in acc.items():
        en_unidad(fs, "alt")
        fs.sort(key=lambda x: -x["faltan_kg"])
        # "Desde" del color = el pedido que MÁS espera entre sus telas (igual que
        # la columna Desde del corte por tela).
        viejo = max(fs, key=lambda x: x["dias_espera"])
        out.append({
            "codigo": cod,
            "nombre": nombres.get(cod.upper(), ""),
            "u": fs[0]["u"],
            "variants": fs,
            "mas_viejo_es": viejo["mas_viejo_es"],
            "dias_espera": viejo["dias_espera"],
            "pedido": sum(x["pedido_d"] for x in fs),
            "stock": sum(x["inventario_d"] for x in fs),
            "prod": sum(x["produccion_d"] for x in fs),
            "falta": sum(x["faltan_d"] for x in fs),
        })
    # Lo que más falta arriba; desempate por código para que el orden sea estable.
    return sorted(out, key=lambda g: (-g["falta"], g["codigo"]))


def por_cliente() -> list[dict]:
    """Total pedido por CLIENTE (código), de todo lo pendiente, mayor primero.

    ⚠ El total MEZCLA unidades (rollos, kilos y unidades de cuellos/puños) tal
    como viene de Asinfo — igual que el Excel de la dueña. No se puede unificar
    en una sola cifra sin bajar a producto, que es lo que hace el corte Color.
    """
    acc: dict[str, dict] = {}
    for peds in pedidos_por_color().values():
        for p in peds:
            clave = p["codigo_cliente"] or p["cliente"]
            if not clave:
                continue
            g = acc.setdefault(clave, {
                "cliente": clave, "nombre": p["cliente"],
                "pedido": 0.0, "lineas": 0,
            })
            g["pedido"] += p["cantidad"]
            g["lineas"] += 1
    for g in acc.values():
        g["pedido"] = round(g["pedido"], 1)
    return sorted(acc.values(), key=lambda g: (-g["pedido"], g["cliente"]))


# ── Acabado (TUB/ABI) por producto (2026-08-18) ──────────────────────────────
# La dueña necesita ver si el pedido es abierto o tubular. El acabado NO está en
# la línea de pedido, pero SÍ es un atributo del producto terminado: se resuelve
# igual que en stock_asinfo_lote (EAV del lote, `id_atributo = 1`), agrupado por
# producto (todos los lotes de un producto comparten su acabado). Consulta
# APARTE y fail-soft: si Asinfo no contesta o la SQL falla, se devuelve {} y la
# pantalla simplemente no muestra el punto — nunca se cae. Estampado queda
# afuera a pedido de la dueña.
_SQL_ACABADO = """
WITH attr AS (
    SELECT u.id_lote,
           MAX(CASE WHEN va.id_atributo = 1 THEN va.nombre END) AS acabado
      FROM lote l
      UNPIVOT (idv FOR slot IN (
          id_valor_atributo_1, id_valor_atributo_2, id_valor_atributo_3,
          id_valor_atributo_4, id_valor_atributo_5, id_valor_atributo_6,
          id_valor_atributo_7, id_valor_atributo_8, id_valor_atributo_9,
          id_valor_atributo_10)) u
      JOIN valor_atributo va ON va.id_valor_atributo = u.idv
     GROUP BY u.id_lote
)
SELECT pr.codigo, MAX(a.acabado) AS acabado
  FROM saldo_producto_lote spl
  JOIN producto pr ON pr.id_producto = spl.id_producto
  JOIN attr a ON a.id_lote = spl.id_lote
 WHERE spl.id_bodega = {bod} AND a.acabado IS NOT NULL
 GROUP BY pr.codigo
"""


def acabados_por_producto() -> dict[str, str]:
    """`{código de producto: 'TUB' | 'ABI' | ...}`. `{}` si falla (fail-soft)."""
    try:
        filas, ok = metabase_client.fetch_dataset_estado(
            ASINFO_DB, _SQL_ACABADO.format(bod=BODEGA_TERMINADO))
        if not ok:
            return {}
        out = {}
        for r in filas:
            cod = str(r.get("codigo") or "").strip()
            aca = str(r.get("acabado") or "").strip().upper()
            if cod and aca:
                out[cod] = aca
        return out
    except Exception:  # noqa: BLE001 — sin acabado, la pantalla sigue andando
        _LOG.warning("pedidos: no pude traer el acabado", exc_info=True)
        return {}


def marcar_acabado(filas: list[dict]) -> list[dict]:
    """Agrega `acabado` a cada fila (vacío si no se pudo resolver)."""
    m = acabados_por_producto()
    for f in filas:
        f["acabado"] = m.get(f["codigo"], "")
    return filas
