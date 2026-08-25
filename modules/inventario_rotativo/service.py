"""Inventario rotativo: lo que se vende casi todas las semanas.

Contesta la pregunta de la dueña: *de lo que se vende siempre, ¿cuánto se
vende por semana, cuánto hay en bodega, cuánto viene en camino y para cuántas
semanas alcanza?* — abierto por color (default) y por tela.

Fuente única: Asinfo vía Metabase (db 2), igual que /pedidos.

    detalle_factura_cliente          → la venta semanal de las últimas 52 semanas
    saldo_producto (bodega 53)       → lo que hay en bodega
    v_saldos_comprometidos_detallado → lo pedido y todavía sin despachar
    orden_fabricacion (bodega 53)    → lo que se está tinturando
    saldo_producto_lote + lote       → el acabado (TUB/ABI) de cada producto

Fail-soft: si Metabase no contesta, devuelve `([], False)` y la pantalla
muestra el cartel. Nunca levanta.

## Las definiciones, y de dónde salen

**Qué "rota".** Un producto entra si vendió en `SEMANAS_MINIMAS` o más de las
últimas 52. Son ~290 productos: el 38% del catálogo activo y el 77% de los
kilos. El resto vende a saltones y no se stockea contra promedio — teñirlo
contra pronóstico es cómo se junta el stock parado.

**Lo pedido NO se resta de nada** (dueña 2026-08-18: *"no suma a ningún lado,
pero un número informativo"* · *"todavía no se va"*). Va con asterisco: la
mercadería sigue en bodega hasta que se despacha, y restarla acá adelantaría
una salida que puede no ocurrir esta semana. Es contexto para leer la fila
—"esto ya tiene dueño"— y no entra en la cobertura ni en el faltante.

**El punto de reposición** es el percentil 90 de la demanda ACUMULADA en una
ventana móvil de dos semanas: "en 9 de cada 10 ciclos de tintura alcanza".
Se usa el percentil empírico y no media±σ porque la demanda textil es
asimétrica y con colas gordas, y la normal subestima justo la cola que hay
que cubrir.

**Las dos semanas** no son a ojo: es el ciclo de tintura medido sobre 3.265
órdenes de formulas_app (mediana 7 días, P90 11). De ahí sale el umbral rojo
de la pantalla: si alcanza para menos de lo que tarda reponerlo, se va a
quedar sin.

**Por qué la serie se completa acá y no en SQL.** Las semanas sin venta valen
0 y tienen que estar, o el percentil se calcula sólo sobre las semanas buenas
y sale inflado. Armar esa grilla en SQL Server (52 × productos, con
PERCENTILE_CONT por ventana) tardaba más de tres minutos; traer la serie
cruda y completarla en Python tarda 1,5 s.

**El acabado (TUB/ABI)** se pide prestado a `modules.pedidos.service`: es
atributo del LOTE en Asinfo y se resuelve por producto (todos los lotes de un
producto comparten su acabado). Se importa en vez de copiar la SQL para que
las dos pantallas no puedan decir acabados distintos del mismo producto.

**Las trampas de la fuente** son las mismas tres que documenta
`modules/pedidos/service.py` — las órdenes de fabricación que se cuentan
doble por el par padre/hija, el `estado_produccion = 0` que es un cementerio
y no una cola, y las unidades. Ver allá el detalle.
"""
from __future__ import annotations

import logging
import time

from modules._lib import metabase_client

_LOG = logging.getLogger("programa_core.inventario_rotativo")

ASINFO_DB = 2

#: Bodega de Producto Terminado en Asinfo.
BODEGA_TERMINADO = 53

#: Semanas de historia que mira la pantalla.
VENTANA_SEMANAS = 52

#: Un producto "rota" si vendió en esta cantidad de las últimas 52 semanas.
SEMANAS_MINIMAS = 40

#: Lo que tarda una tintura, en semanas. Medido, no supuesto.
LEAD_SEMANAS = 2

#: "En 9 de cada 10 ciclos alcanza".
NIVEL_SERVICIO = 0.90

#: Promedio de venta semanal: la ventana corta manda (dueña 2026-08-18: "3 y 6
#: meses, más importante" — 13 y 26 semanas). La corta es la que se muestra.
SEMANAS_CORTA, SEMANAS_LARGA = 13, 26

#: Una orden lanzada hace más que esto no cuenta como "en producción".
DIAS_PRODUCCION_VIVA = 30

#: Ídem `modules/pedidos/service.py`: en Asinfo `GETDATE()` devuelve UTC
#: mientras las fechas se graban en hora local.
AHORA_EC = "DATEADD(hour, -5, GETDATE())"

#: Un rollo son 23,5 kg (mismo plano que /pedidos).
KG_POR_ROLLO = 23.5

#: Unidades de medida de Asinfo (tabla `unidad`), ídem /pedidos.
UNIDAD_UN, UNIDAD_KG, UNIDAD_ROLLO = 1, 2, 51

#: Un pedido más viejo que esto no cuenta (mismo corte que /pedidos, para que
#: las dos pantallas digan el mismo número).
DIAS_PEDIDO_MAX = 90

#: Unidades por kilo de lo que se vende por pieza.
UN_POR_KG = {"Cuellos": 33.33, "Puños": 50.0}

#: La unidad en la que la dueña lee cada familia (2026-08-18): "se mide todo
#: en rollos salvo rib, cuellos y puños".
UNIDAD_POR_FAMILIA = {"Rib": "kg", "Cuellos": "un", "Puños": "un"}
UNIDAD_DEFECTO = "roll"

#: Familias que son tela vendible. El resto de `nombre_categoria_producto`
#: (TELA CRUDA, HILO, COLORANTES...) son insumos y no se stockean así.
CATEGORIAS = (
    "Jersey", "Fleece", "Rib", "Poliester", "Pique",
    "Lycra", "Toper", "Franela", "Boston", "Cuellos", "Puños",
)

#: Alcanza para menos que el lead time → rojo. Entre el lead time y una
#: semana más → ámbar. Más de esto → está durmiendo plata.
SEM_ROJO, SEM_AMBAR, SEM_SOBRA = 2.0, 3.0, 12.0

_TTL_SECS = 600
_cache: dict = {}

#: El color es el ÚLTIMO token del nombre del producto. Mismo criterio (y
#: mismos motivos) que `modules/pedidos/service.py::SQL_COLOR`.
SQL_COLOR = (
    "REVERSE(LEFT(REVERSE(LTRIM(RTRIM(pr.nombre))), "
    "CHARINDEX(' ', REVERSE(LTRIM(RTRIM(pr.nombre))) + ' ') - 1))"
)

#: Ventas por producto y semana. `w` es el número de semana absoluto desde una
#: fecha ancla fija, para poder restar semanas sin pelear con años.
_CTE_VENTAS = """
WITH v AS (
    SELECT dfc.id_producto,
           DATEDIFF(week, '2000-01-02', COALESCE(pad.fecha, fc.fecha)) AS w,
           SUM(CASE WHEN fc.id_documento IN (20, 451)
                    THEN -dfc.cantidad ELSE dfc.cantidad END) AS kg
      FROM factura_cliente fc
      JOIN detalle_factura_cliente dfc
        ON dfc.id_factura_cliente = fc.id_factura_cliente
      LEFT JOIN factura_cliente pad
        ON pad.id_factura_cliente = fc.id_factura_cliente_padre
     WHERE fc.id_documento IN (7, 251, 20, 451)
       AND fc.estado NOT IN (0, 1)
       AND COALESCE(pad.fecha, fc.fecha)
           >= DATEADD(week, -{ventana}, CAST({ahora} AS date))
       AND COALESCE(pad.fecha, fc.fecha) < CAST({ahora} AS date)
     GROUP BY dfc.id_producto,
              DATEDIFF(week, '2000-01-02', COALESCE(pad.fecha, fc.fecha))
),
cand AS (
    SELECT id_producto, COUNT(*) AS semanas_venta, SUM(kg) AS kg52
      FROM v WHERE kg > 0
     GROUP BY id_producto HAVING COUNT(*) >= {minimas}
)
"""

_SQL_SERIE = _CTE_VENTAS + """
SELECT v.id_producto, v.w, v.kg
  FROM v JOIN cand c ON c.id_producto = v.id_producto
"""

_SQL_FICHA = _CTE_VENTAS + """,
ped AS (
    SELECT v.id_producto,
           SUM(CASE WHEN v.id_unidad_pedido = {rollo}
                    THEN v.saldo_comprometido * {kg_rollo}
                    WHEN v.id_unidad_pedido = {kg} THEN v.saldo_comprometido
                    ELSE 0 END) AS ped_kg,
           SUM(CASE WHEN v.id_unidad_pedido = {un}
                    THEN v.saldo_comprometido ELSE 0 END) AS ped_un
      FROM v_saldos_comprometidos_detallado v
     WHERE DATEDIFF(day, v.fecha, {ahora}) <= {dias_ped}
     GROUP BY v.id_producto
),
inv AS (
    SELECT id_producto, SUM(saldo) AS inv_kg
      FROM (SELECT id_producto, saldo,
                   ROW_NUMBER() OVER (PARTITION BY id_producto, id_bodega
                                      ORDER BY fecha DESC,
                                               id_saldo_producto DESC) AS rn
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
           COUNT(*) AS n_ordenes
      FROM orden_fabricacion o
      LEFT JOIN padres h ON h.p = o.id_orden_fabricacion
     WHERE o.id_bodega = {bod}
       AND o.estado_produccion = 2
       AND h.p IS NULL
       AND o.id_producto IS NOT NULL
       AND o.cantidad > ISNULL(o.cantidad_fabricada, 0)
       AND DATEDIFF(day, o.fecha, {ahora}) <= {dias_prod}
     GROUP BY o.id_producto
)
SELECT c.id_producto,
       pr.nombre_categoria_producto AS categoria,
       ISNULL(sub.nombre, 'Sin tela') AS tela,
       RTRIM(pr.codigo) AS codigo,
       {color} AS color,
       c.semanas_venta, c.kg52,
       ISNULL(inv.inv_kg, 0) AS inv_kg,
       ISNULL(ped.ped_kg, 0) AS ped_kg,
       ISNULL(ped.ped_un, 0) AS ped_un,
       ISNULL(prod.prod_kg, 0) AS prod_kg,
       ISNULL(prod.n_ordenes, 0) AS n_ordenes
  FROM cand c
  JOIN producto pr ON pr.id_producto = c.id_producto
  LEFT JOIN subcategoria_producto sub
         ON sub.id_subcategoria_producto = pr.id_subcategoria_producto
  LEFT JOIN inv  ON inv.id_producto  = c.id_producto
  LEFT JOIN ped  ON ped.id_producto  = c.id_producto
  LEFT JOIN prod ON prod.id_producto = c.id_producto
"""


def _sql_serie() -> str:
    return _SQL_SERIE.format(
        ventana=VENTANA_SEMANAS, minimas=SEMANAS_MINIMAS, ahora=AHORA_EC
    )


def _sql_ficha() -> str:
    return _SQL_FICHA.format(
        ventana=VENTANA_SEMANAS, minimas=SEMANAS_MINIMAS, ahora=AHORA_EC,
        bod=BODEGA_TERMINADO, dias_prod=DIAS_PRODUCCION_VIVA, color=SQL_COLOR,
        un=UNIDAD_UN, kg=UNIDAD_KG, rollo=UNIDAD_ROLLO,
        kg_rollo=KG_POR_ROLLO, dias_ped=DIAS_PEDIDO_MAX,
    )


def _f(valor) -> float:
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def percentil(valores: list[float], q: float) -> float:
    """Percentil empírico con interpolación lineal. `[]` → 0."""
    if not valores:
        return 0.0
    xs = sorted(valores)
    if len(xs) == 1:
        return xs[0]
    k = q * (len(xs) - 1)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def unidad_de(familia: str) -> str:
    return UNIDAD_POR_FAMILIA.get(familia, UNIDAD_DEFECTO)


def a_unidad(kilos: float, unidad: str, familia: str) -> float:
    """Kilos leídos en la unidad de esa familia.

    Los rollos van ENTEROS (dueña: "no puede haber medios") y algo positivo
    nunca se muestra como 0: medio rollo sube a 1, o la pantalla diría que no
    falta nada cuando falta.
    """
    if unidad == "kg":
        return round(kilos, 1)
    if unidad == "un":
        return round(kilos * UN_POR_KG.get(familia, 33.33))
    r = round(kilos / KG_POR_ROLLO)
    if kilos > 0 and r == 0:
        r = 1
    return float(r)


def estado(alcanza: float) -> str:
    """El semáforo. Ver SEM_ROJO/SEM_AMBAR/SEM_SOBRA."""
    if alcanza < SEM_ROJO:
        return "rojo"
    if alcanza < SEM_AMBAR:
        return "ambar"
    if alcanza > SEM_SOBRA:
        return "sobra"
    return "ok"


def _fila(ficha: dict, semanas: dict[int, float], ultima: int) -> dict:
    """Una ficha + su serie semanal → el dict que consume el template."""
    familia = str(ficha.get("categoria") or "").strip()
    unidad = unidad_de(familia)

    # Grilla completa: las semanas sin venta valen 0 y tienen que estar.
    # Una devolución puede dejar la semana en negativo; para el percentil se
    # trunca a 0 (una semana de devoluciones no es "demanda negativa"), pero
    # el promedio sí la deja restar, que es lo que se vendió de verdad.
    crudo = [semanas.get(w, 0.0) for w in range(ultima - VENTANA_SEMANAS + 1, ultima + 1)]
    kg = [max(x, 0.0) for x in crudo]

    ventanas = [
        sum(kg[max(0, i - LEAD_SEMANAS + 1):i + 1]) for i in range(len(kg))
    ]
    punto_kg = percentil(ventanas, NIVEL_SERVICIO)

    sem_kg = sum(crudo[-SEMANAS_CORTA:]) / SEMANAS_CORTA
    sem_larga_kg = sum(crudo[-SEMANAS_LARGA:]) / SEMANAS_LARGA
    inv_kg = _f(ficha.get("inv_kg"))
    prod_kg = _f(ficha.get("prod_kg"))
    # Los cuellos y puños se piden por unidad; la factura sale siempre en kilos.
    ped_kg = _f(ficha.get("ped_kg")) + (
        _f(ficha.get("ped_un")) / UN_POR_KG[familia] if familia in UN_POR_KG else 0.0
    )
    # `ped_kg` NO entra acá a propósito: es informativo (ver el docstring).
    disponible_kg = inv_kg + prod_kg
    falta_kg = max(punto_kg - disponible_kg, 0.0)

    # Sin venta en las últimas 13 semanas no se puede dividir: el producto
    # rota en el año pero se apagó hace poco. No es rojo — es "no sé".
    alcanza = disponible_kg / sem_kg if sem_kg > 0 else -1.0

    return {
        "familia": familia,
        "tela": str(ficha.get("tela") or "").strip(),
        "color": str(ficha.get("color") or "").strip(),
        "codigo": str(ficha.get("codigo") or "").strip(),
        "unidad": unidad,
        "semanas_venta": int(_f(ficha.get("semanas_venta"))),
        "sem_kg": round(sem_kg, 1),
        "sem_larga_kg": round(sem_larga_kg, 1),
        "stock_kg": round(inv_kg, 1),
        "pedido_kg": round(ped_kg, 1),
        "proceso_kg": round(prod_kg, 1),
        "punto_kg": round(punto_kg, 1),
        "falta_kg": round(falta_kg, 1),
        "n_ordenes": int(_f(ficha.get("n_ordenes"))),
        "alcanza": round(alcanza, 2),
        "estado": estado(alcanza) if alcanza >= 0 else "ok",
        # los mismos números en la unidad que se lee
        "sem": a_unidad(sem_kg, unidad, familia),
        "stock": a_unidad(inv_kg, unidad, familia),
        "pedido": a_unidad(ped_kg, unidad, familia),
        "proceso": a_unidad(prod_kg, unidad, familia),
        "falta": a_unidad(falta_kg, unidad, familia),
    }


def _marcar_acabado(filas: list[dict]) -> None:
    """Le pega el acabado a cada fila. Fail-soft: sin acabado, columna vacía.

    La fuente es `modules.pedidos.service`, no una SQL propia: el acabado ya
    está resuelto ahí y dos consultas paralelas se desincronizan sin que nadie
    se entere hasta que las dos pantallas dicen cosas distintas del mismo
    producto.
    """
    try:
        from modules.pedidos.service import acabados_por_producto

        mapa = acabados_por_producto()
    except Exception:  # noqa: BLE001 — el acabado es una columna, no la pantalla
        _LOG.warning("inventario rotativo: no pude traer el acabado", exc_info=True)
        mapa = {}
    for f in filas:
        f["acabado"] = mapa.get(f["codigo"], "")


def rotativo(_ahora=time.monotonic) -> tuple[list[dict], bool]:
    """Los productos que rotan, con stock y producción. `(filas, disponible)`.

    `disponible=False` significa que Metabase no contestó — distinto de que no
    haya nada que reponer. La pantalla los tiene que distinguir para no decir
    "está todo bien" cuando en realidad no pudo preguntar.
    """
    ahora = _ahora()
    if _cache.get("filas") is not None and ahora - _cache.get("t", 0) < _TTL_SECS:
        return _cache["filas"], True

    fichas, ok = metabase_client.fetch_dataset_estado(ASINFO_DB, _sql_ficha())
    if not ok:
        _LOG.warning("inventario rotativo: Asinfo no contestó (ficha)")
        return [], False
    serie, ok = metabase_client.fetch_dataset_estado(
        ASINFO_DB, _sql_serie(), max_results=100000
    )
    if not ok:
        _LOG.warning("inventario rotativo: Asinfo no contestó (serie)")
        return [], False

    por_producto: dict = {}
    ultima = 0
    for r in serie:
        w = int(_f(r.get("w")))
        por_producto.setdefault(r.get("id_producto"), {})[w] = _f(r.get("kg"))
        ultima = max(ultima, w)

    filas = [
        _fila(f, por_producto.get(f.get("id_producto"), {}), ultima)
        for f in fichas
        if str(f.get("categoria") or "").strip() in CATEGORIAS
    ]
    _marcar_acabado(filas)
    filas.sort(key=lambda x: (x["alcanza"] < 0, x["alcanza"]))
    _cache.update(filas=filas, t=ahora)
    return filas, True


def unidad_del_grupo(grupo: list[dict]) -> tuple[str, str]:
    """La unidad en que se lee el subtotal, y la familia con que convertirlo.

    Manda la que pesa más: un color que es casi todo Fleece se lee en rollos
    aunque traiga un Rib colgando. Empates a favor de los rollos, que es la
    unidad de casi todo.
    """
    peso: dict[str, float] = {}
    fam: dict[str, str] = {}
    for f in grupo:
        peso[f["unidad"]] = peso.get(f["unidad"], 0.0) + f["sem_kg"]
        fam.setdefault(f["unidad"], f["familia"])
    if not peso:
        return UNIDAD_DEFECTO, ""
    u = max(peso, key=lambda k: (peso[k], k == UNIDAD_DEFECTO))
    return u, fam[u]


def agrupar(filas: list[dict], por: str) -> list[dict]:
    """Bloques por color (default) o por tela.

    El bloque con más faltante va primero: es por donde hay que empezar a
    teñir.

    El subtotal va en UNA sola unidad (dueña 2026-08-18: "poneme todo en
    rollos salvo los cuellos, rib y puños" · "o en rollo o en kg, no me
    conviertas ambos"). Cuando el bloque mezcla —un color que junta Fleece con
    Rib— se usa la unidad que manda por peso, no las dos: dos términos en un
    encabezado son dos números para leer donde alcanza uno.
    """
    clave = "color" if por == "color" else "tela"
    dentro = "tela" if por == "color" else "color"
    grupos: dict[str, list[dict]] = {}
    for f in filas:
        grupos.setdefault(f[clave], []).append(f)

    bloques = []
    for nombre, grupo in grupos.items():
        grupo = sorted(grupo, key=lambda x: (x["alcanza"] < 0, x["alcanza"]))
        sem_kg = round(sum(x["sem_kg"] for x in grupo), 1)
        falta_kg = round(sum(x["falta_kg"] for x in grupo), 1)
        u, fam = unidad_del_grupo(grupo)
        bloques.append({
            "nombre": nombre,
            "etiqueta": dentro,
            "filas": grupo,
            "n": len(grupo),
            "unidad": u,
            "sem": a_unidad(sem_kg, u, fam),
            "falta": a_unidad(falta_kg, u, fam),
            "sem_kg": sem_kg,
            "falta_kg": falta_kg,
            "familias": sorted({x["familia"] for x in grupo}),
        })
    bloques.sort(key=lambda b: (-b["falta_kg"], -b["sem_kg"]))
    return bloques


def resumen(filas: list[dict]) -> dict:
    """Los tres números del encabezado."""
    alerta = [f for f in filas if f["estado"] == "rojo"]
    return {
        "n": len(filas),
        "n_alerta": len(alerta),
        "kg_falta": round(sum(f["falta_kg"] for f in alerta)),
        "familias": sorted({f["familia"] for f in filas}),
    }


# ── Las que todavía no entran ────────────────────────────────────────────────
#
# La lista pide vender en 40 de las últimas 52 semanas. Un producto que
# empezó a venderse hace menos de medio año no puede llegar a 40 ni vendiendo
# todas las semanas: queda afuera por aritmética, no porque no rote. Sin este
# renglón la pantalla parece decir "esto es todo lo que hay" cuando en
# realidad hay stock que no está mirando.

#: Medio año en semanas. Ver arriba: 26 < 40, así que "primera venta dentro de
#: las últimas 26 semanas" implica "no está en la lista", sin tener que
#: cruzarlo contra ella.
SEMANAS_NUEVO = 26

_CAT_SQL = ", ".join(f"'{c}'" for c in CATEGORIAS)

#: Productos CON stock en bodega 53 cuya PRIMERA venta de la historia cae
#: dentro de las últimas `SEMANAS_NUEVO` semanas. El `MIN` se calcula sólo
#: sobre los que tienen stock (el `IN (SELECT ...)`), que son unos cientos:
#: agrupar la tabla de detalle entera no hace falta y no termina más.
_SQL_NUEVOS = """
WITH inv AS (
    SELECT id_producto, SUM(saldo) AS inv_kg
      FROM (SELECT id_producto, saldo,
                   ROW_NUMBER() OVER (PARTITION BY id_producto, id_bodega
                                      ORDER BY fecha DESC,
                                               id_saldo_producto DESC) AS rn
              FROM saldo_producto
             WHERE id_bodega = {bod}) u
     WHERE u.rn = 1 AND u.saldo > 0
     GROUP BY id_producto
),
vta AS (
    SELECT dfc.id_producto,
           MIN(COALESCE(pad.fecha, fc.fecha)) AS primera
      FROM factura_cliente fc
      JOIN detalle_factura_cliente dfc
        ON dfc.id_factura_cliente = fc.id_factura_cliente
      LEFT JOIN factura_cliente pad
        ON pad.id_factura_cliente = fc.id_factura_cliente_padre
     WHERE fc.id_documento IN (7, 251)
       AND fc.estado NOT IN (0, 1)
       AND dfc.id_producto IN (SELECT id_producto FROM inv)
     GROUP BY dfc.id_producto
)
SELECT COUNT(*) AS n, SUM(inv.inv_kg) AS kg
  FROM inv
  JOIN vta ON vta.id_producto = inv.id_producto
  JOIN producto pr ON pr.id_producto = inv.id_producto
 WHERE LTRIM(RTRIM(pr.nombre_categoria_producto)) IN ({cats})
   AND vta.primera >= DATEADD(week, -{nuevo}, CAST({ahora} AS date))
"""

_cache_nuevos: dict = {}


def _sql_nuevos() -> str:
    return _SQL_NUEVOS.format(
        bod=BODEGA_TERMINADO, cats=_CAT_SQL, nuevo=SEMANAS_NUEVO, ahora=AHORA_EC
    )


def nuevos(_ahora=time.monotonic) -> dict:
    """Cuántas telas × color quedan afuera por ser nuevas, y cuántos kilos.

    `{"n": 0, "kg": 0.0}` cuando no hay ninguna, y `{}` cuando Asinfo no
    contestó: la pantalla no puede decir "no falta nada" si no pudo preguntar.
    """
    ahora = _ahora()
    if _cache_nuevos.get("dato") is not None and ahora - _cache_nuevos.get("t", 0) < _TTL_SECS:
        return _cache_nuevos["dato"]

    filas, ok = metabase_client.fetch_dataset_estado(ASINFO_DB, _sql_nuevos())
    if not ok:
        _LOG.warning("inventario rotativo: Asinfo no contestó (nuevos)")
        return {}

    fila = filas[0] if filas else {}
    dato = {"n": int(_f(fila.get("n"))), "kg": round(_f(fila.get("kg")), 1)}
    _cache_nuevos.update(dato=dato, t=ahora)
    return dato
