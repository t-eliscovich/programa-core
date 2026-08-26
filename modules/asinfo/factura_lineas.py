"""Lo que se llevó el cliente en una factura, renglón por renglón (Asinfo).

TMT 2026-08-25 (dueña): *"cuando clickeen en el programa, les aparezca qué
llevaron en ese número de factura"*. Programa Core guarda la PLATA de la
factura (importe, abono, saldo) pero nunca guardó la MERCADERÍA: qué tela, qué
color, cuántos rollos. Eso vive en Asinfo y se trae por el puente de Metabase.

## Lo que hay que saber de la factura de Asinfo (verificado 25/08/2026)

1. **Hay un renglón por ROLLO, no por tela.** La 001-099-000182419 tiene 14
   renglones que son 7 telas: cuatro rollos de Fleece 96 Perchado Fresa
   (21,75 + 21,10 + 21,05 + 21,70 kg), dos pedazos de Cuellos T40 Eléctrico
   (1,45 y 0,90). Por eso acá se AGRUPA y se cuenta: mostrar los 14 renglones
   crudos es la factura de Asinfo, no la respuesta a "¿qué se llevó?".

2. **`fc.total` NO es lo que paga el cliente**: es el bruto ANTES del
   descuento. En esa factura vale 2.282,71 y el cliente paga 2.144,72. La
   aritmética, medida contra 35 facturas del 21/08 sin una sola diferencia:

       bruto     = Σ precio_linea            = fc.total
       descuento = Σ descuento_linea         = fc.descuento
       IVA       = (bruto − descuento) × 15% = fc.impuesto
       a pagar   = bruto − descuento + IVA

3. **El descuento viene en CASCADA** (`porcentaje_descuento` y
   `porcentaje_descuento_2`): 5% y después 14% sobre lo que quedó. Por eso el
   pie muestra los dos por separado y no un porcentaje sumado, que no existe.

4. **El color y la calidad no viven en una columna fija.** El renglón trae
   cinco pares `id_atributo_N` / `id_valor_atributo_N`, y el ORDEN lo pone el
   producto. En las telas de hoy el color cae en el slot 1 y la calidad en el
   2, pero eso es una coincidencia del maestro, no una garantía: se busca por
   el ID DEL ATRIBUTO (3 = Color, 2 = Calidad), no por la posición.

5. **El "código" es el del COLOR, no el del producto.** TMT 25/08: *"código
   solo es el código de color, no el conjunto"*. `producto.codigo` es el
   conjunto (AL12BLA = Alemania 1.2 + BLAnco); el código suelto vive en
   `valor_atributo.codigo` del atributo Color (BLA). Se usa ése, y si el
   renglón no trae color se cae a las tres últimas letras del producto, que
   es de donde lo saca `analisis/asinfo_parado`.

6. **No todo renglón es tela.** El SERVICIO DE LOGISTICA entra como una línea
   más con `cantidad = 1` — que es una unidad, no un kilo. Sumarlo daría kilos
   de más (el mismo error que ya se pagó en `dia_despacho`). Acá va aparte,
   sin rollos y sin kilos.

7. **No toda fila de `factura_cliente` es una factura.** En la misma tabla
   viven las NOTAS DE CRÉDITO (`id_documento = 17`) y las DEVOLUCIONES
   (`id_documento = 20`), con su propia numeración (001-099-0000117xx contra
   001-099-0001826xx del mismo día). Y la nota de crédito **no tiene kilos**:
   acredita PLATA, así que cada renglón viene con `cantidad = 1` —una unidad,
   la misma trampa del punto 6— y el importe entero metido en el precio. Al
   contarlos como kilos, la 001-099-000011795 (−377,30 de SPI) decía *"4
   rollos · 4,00 kg"* con un precio de 196,84 el kilo. Por eso el bloque
   pregunta QUÉ DOCUMENTO es y, si es nota de crédito, no muestra ni rollos
   ni kilos. TMT 2026-08-26 (dueña): *"kilos está mal"*.

   La red que lo prueba: los kilos del bloque tienen que dar los mismos que
   `scintela.factura.kg`. Medido sobre las 243 facturas cacheadas el
   26/08/2026 — factura 206/206 ✓, devolución 14/14 ✓, nota de crédito
   **0 de 23**.

Fail-soft, como todo lo que cuelga de Asinfo: si Metabase no contesta, la
pantalla lo dice y la ficha de la factura sigue viva. Nunca levanta.
"""
from __future__ import annotations

import logging
import re
import threading
import time

_LOG = logging.getLogger("programa_core.asinfo.factura_lineas")

#: La base de Asinfo dentro de Metabase.
DB_ASINFO = 2

#: El número SRI: 001-099-000182419. Es lo único que se interpola en el SQL,
#: así que se valida ENTERO (no recortado: un `[:17]` previo se comería la cola
#: y dejaría pasar basura sin decirlo).
_NUMERO_RE = re.compile(r"^\d{3}-\d{3}-\d{9}$")

#: Los atributos de Asinfo que nos importan (tabla `atributo`).
ATRIBUTO_CALIDAD = 2
ATRIBUTO_COLOR = 3

#: Un renglón de esta categoría es un servicio, no mercadería.
CATEGORIA_SERVICIOS = "SERVICIOS"

#: Los documentos de Asinfo que caen en `factura_cliente` (tabla `documento`).
#: La factura y la devolución mueven MERCADERÍA —tienen rollos y kilos—; la
#: nota de crédito mueve PLATA y no tiene ninguno de los dos. Ver el punto 7.
DOC_FACTURA = 7
DOC_NOTA_CREDITO = 17
DOC_DEVOLUCION = 20

_DOCS = {
    DOC_FACTURA: "factura",
    DOC_NOTA_CREDITO: "nota-credito",
    DOC_DEVOLUCION: "devolucion",
}

#: El rótulo del bloque. Vive acá, y no en cada template, porque son TRES
#: pantallas (oficina, vendedor y cliente) y ya está escrito que las tres
#: tienen que decir lo mismo: si el cliente y el vendedor vieran dos detalles
#: distintos de la misma factura, la discusión no se puede tener.
#:
#: TMT 2026-08-26 (dueña): *"no me gusta el título. Que se llame detalle"*.
#: La nota de crédito y la devolución se siguen nombrando: ahí el título es
#: lo único que dice que ese papel NO es una venta.
TITULOS = {
    "nota-credito": "Nota de crédito",
    "devolucion": "Qué devolvió",
}
TITULO_DEFAULT = "Detalle"

#: IVA vigente. El mismo 15% que usa la lista de precios.
IVA = 0.15

#: La forma de lo que se guarda en `scintela.factura_detalle`. Se sube cuando
#: `_agrupar` empieza a devolver algo nuevo: las filas viejas dejan de leerse y
#: se reescriben solas la próxima vez que alguien mire esa factura. Sale más
#: barato que acordarse de vaciar la tabla en cada deploy — y olvidarse deja
#: pantallas mostrando la mitad de los datos sin que nada falle.
FORMATO = 5

#: Cuánto vale la foto. Una factura vieja no cambia nunca; una de hoy puede
#: recibir un renglón más en los minutos siguientes a emitirse.
_TTL_SEGS = 600.0
_TOPE_CACHE = 200

_CACHE: dict[str, tuple[float, dict]] = {}
_CANDADO = threading.Lock()

#: Cuándo corrió la última precarga (monotonic). 0 = nunca.
_ULTIMA_PRECARGA = 0.0

#: El relleno de las facturas VIEJAS: cuántas por vuelta, cada cuánto, y hasta
#: dónde para atrás. Ver `precargar_faltantes`.
_FALTANTES_LOTE = 120
_FALTANTES_CADA = 120.0
_FALTANTES_DIAS = 180
_ULTIMO_RELLENO = 0.0


def reset_cache() -> None:
    """Olvida lo cacheado EN MEMORIA (la base es la caché de verdad)."""
    global _ULTIMA_PRECARGA, _ULTIMO_RELLENO
    with _CANDADO:
        _CACHE.clear()
    _ULTIMA_PRECARGA = 0.0
    _ULTIMO_RELLENO = 0.0


# ---------------------------------------------------------------------------
# La caché que sobrevive al deploy
# ---------------------------------------------------------------------------
# TMT 2026-08-25 (dueña): *"el que se llevó carga lento"*. Medido: 630-780 ms.
# No es el SQL — la pregunta más tonta posible contra Asinfo (las columnas de
# una tabla) tarda 590-690 ms igual. Es el peaje fijo del puente.
#
# Una factura emitida no cambia, así que la respuesta se guarda en la base y no
# se vuelve a preguntar. La caché en memoria sigue adelante porque es más
# rápida todavía y ahorra el viaje a Postgres.
def _de_la_base(numero: str) -> dict | None:
    """Lo guardado, o None. Nunca levanta: es una caché, no una fuente."""
    try:
        import db

        fila = db.fetch_one(
            "SELECT datos FROM scintela.factura_detalle WHERE numero = %s",
            (numero,))
    except Exception as e:  # noqa: BLE001 — sin base, se le pregunta a Asinfo
        _LOG.warning("leyendo la caché de %s: %s", numero, e)
        return None
    datos = (fila or {}).get("datos")
    if not isinstance(datos, dict) or datos.get("estado") != "ok":
        return None
    return datos if datos.get("formato") == FORMATO else None


def _guardar(numero: str, res: dict) -> None:
    """Guarda SÓLO el éxito. Un 'no pude preguntar' guardado es una mentira
    que dura para siempre — el error que costó el balance del 29/07."""
    if res.get("estado") != "ok":
        return
    try:
        import json

        import db

        db.execute(
            "INSERT INTO scintela.factura_detalle (numero, datos) "
            "VALUES (%s, %s::jsonb) "
            "ON CONFLICT (numero) DO UPDATE "
            "   SET datos = EXCLUDED.datos, fecha_crea = now()",
            (numero, json.dumps(res)))
    except Exception as e:  # noqa: BLE001 — no poder guardar no rompe la vista
        _LOG.warning("guardando la caché de %s: %s", numero, e)


def _marcar_sin_datos(numero: str) -> None:
    """Deja escrito que Asinfo NO tiene esta factura.

    Sólo lo llama el relleno, y sólo cuando el puente contestó BIEN una
    pregunta que nombraba a esta factura: ahí el silencio es una respuesta
    ("no la conozco"), no un error de red. Las facturas viejas del dBase nunca
    van a estar en Asinfo, y sin esta marca el relleno se quedaría preguntando
    por las mismas para siempre, sin avanzar nunca hacia las que sí están.

    La pantalla NO lee esta marca (`_de_la_base` sólo devuelve el éxito): quien
    abra una de éstas le vuelve a preguntar a Asinfo y recién ahí se le dice
    que no hay detalle. Que la marca sea sólo una anotación del relleno es a
    propósito — un "no hay nada" guardado para siempre es el error que ya costó
    el balance del 29/07, y acá no puede llegar a ninguna pantalla.
    """
    try:
        import json

        import db

        db.execute(
            "INSERT INTO scintela.factura_detalle (numero, datos) "
            "VALUES (%s, %s::jsonb) ON CONFLICT (numero) DO NOTHING",
            (numero, json.dumps({"estado": "sin-datos", "formato": FORMATO})))
    except Exception as e:  # noqa: BLE001 — el relleno nunca rompe nada
        _LOG.warning("marcando sin-datos %s: %s", numero, e)


def _faltantes(limite: int, dias: int) -> list[str]:
    """Las facturas de los últimos `dias` que todavía no tienen su detalle.

    De la más nueva a la más vieja: si alguien va a abrir una factura vieja,
    es mucho más probable que sea la del mes pasado que la del año pasado.

    Se dejan afuera los últimos 3 días, que son de `precargar`: esa trae el día
    entero de una sola vez y además REESCRIBE, que es lo que hace falta
    mientras una factura recién emitida todavía puede recibir un renglón más.
    """
    from datetime import timedelta

    import db
    from filters import today_ec

    hoy = today_ec()
    filas = db.fetch_all(
        "SELECT f.numf_completo AS numero "
        "  FROM scintela.factura f "
        "  LEFT JOIN scintela.factura_detalle d ON d.numero = f.numf_completo "
        " WHERE d.numero IS NULL "
        "   AND f.fecha >= %s AND f.fecha < %s "
        "   AND f.numf_completo ~ '^[0-9]{3}-[0-9]{3}-[0-9]{9}$' "
        " ORDER BY f.fecha DESC, f.id_factura DESC "
        " LIMIT %s",
        (hoy - timedelta(days=max(1, int(dias))), hoy - timedelta(days=3),
         int(limite)))
    return [(f.get("numero") or "").strip() for f in filas]


def precargar_faltantes(limite: int = _FALTANTES_LOTE,
                        dias: int = _FALTANTES_DIAS,
                        cada_secs: float = _FALTANTES_CADA) -> int:
    """El detalle de las facturas VIEJAS, de a lotes, hasta que no falte ninguna.

    TMT 2026-08-26 (dueña): *"qué se llevó tarda mucho en cargarse"* — mirando
    una factura de MAYO. `precargar` calienta los últimos 3 días, así que la
    factura de esta semana abre en milésimas; la de hace tres meses pagaba los
    650 ms del puente igual que el primer día.

    Así que el calentador, además, va llenando la historia para atrás: cada dos
    minutos pregunta por hasta 120 facturas que todavía no tienen detalle —una
    sola pregunta, el mismo peaje de 650 ms que pagaría UNA— y las guarda. En
    unas horas quedan cubiertos los últimos seis meses y nadie vuelve a
    esperar. Cuando no falta ninguna, la vuelta es una consulta a Postgres que
    no devuelve nada y ni siquiera cruza el puente.

    Las que Asinfo no conoce (las viejas del dBase) quedan marcadas para que el
    lote siguiente mire facturas nuevas y no las mismas de siempre.
    """
    global _ULTIMO_RELLENO

    from modules._lib import metabase_client

    ahora = time.monotonic()
    if cada_secs and _ULTIMO_RELLENO and (ahora - _ULTIMO_RELLENO) < cada_secs:
        return 0
    if not metabase_client.disponible():
        return 0
    try:
        numeros = [n for n in _faltantes(limite, dias) if _NUMERO_RE.match(n)]
    except Exception as e:  # noqa: BLE001 — sin base, no hay nada que rellenar
        _LOG.warning("buscando facturas sin detalle: %s", e)
        return 0
    _ULTIMO_RELLENO = ahora
    if not numeros:
        return 0

    lista = ", ".join(f"'{n}'" for n in numeros)
    try:
        filas, ok = metabase_client.fetch_dataset_estado(
            DB_ASINFO, _sql_where(f"fc.numero IN ({lista})"), max_results=20000)
    except Exception as e:  # noqa: BLE001 — fail-soft, como todo el puente
        _LOG.warning("relleno del detalle falló: %s", e)
        return 0
    if not ok:
        return 0

    por_numero: dict[str, list[dict]] = {}
    for f in filas:
        por_numero.setdefault((f.get("numero") or "").strip(), []).append(f)

    guardadas = 0
    for numero in numeros:
        suyas = por_numero.get(numero)
        if suyas:
            _guardar(numero, {"estado": "ok", "formato": FORMATO,
                              **_agrupar(suyas)})
            guardadas += 1
        else:
            _marcar_sin_datos(numero)
    _LOG.info("relleno del detalle: %s de %s facturas", guardadas, len(numeros))
    return guardadas


def precargar(dias: int = 3, cada_secs: float = 1800.0) -> int:
    """El detalle de TODAS las facturas de los últimos días, en UNA consulta.

    Lo llama el calentador (`modules/_lib/warmup.py`). Con esto, la factura que
    alguien va a mirar hoy ya está en la base antes de que la clickee: las que
    se miran son las de esta semana.

    Se cobra el mismo peaje de 650 ms que una factura sola, así que traer 200
    es gratis al lado de que 200 personas paguen 650 ms cada una.

    Reescribe lo que ya estaba: si una factura de ayer se corrigió en Asinfo,
    la caché se entera sola dentro de la media hora.
    """
    global _ULTIMA_PRECARGA

    from datetime import timedelta

    from filters import today_ec
    from modules._lib import metabase_client

    ahora = time.monotonic()
    if cada_secs and _ULTIMA_PRECARGA and (ahora - _ULTIMA_PRECARGA) < cada_secs:
        return 0
    if not metabase_client.disponible():
        return 0
    desde = (today_ec() - timedelta(days=max(0, int(dias)))).isoformat()
    try:
        filas, ok = metabase_client.fetch_dataset_estado(
            DB_ASINFO, _sql_where(f"fc.fecha >= '{desde}'"), max_results=20000)
    except Exception as e:  # noqa: BLE001 — fail-soft, como todo el puente
        _LOG.warning("precargar falló: %s", e)
        return 0
    if not ok or not filas:
        return 0

    por_numero: dict[str, list[dict]] = {}
    for f in filas:
        por_numero.setdefault((f.get("numero") or "").strip(), []).append(f)

    guardadas = 0
    for numero, suyas in por_numero.items():
        if not _NUMERO_RE.match(numero):
            continue
        res = {"estado": "ok", "formato": FORMATO, **_agrupar(suyas)}
        _guardar(numero, res)
        with _CANDADO:
            if len(_CACHE) < _TOPE_CACHE:
                _CACHE[numero] = (ahora, res)
        guardadas += 1
    _ULTIMA_PRECARGA = ahora
    _LOG.info("precarga del detalle de facturas: %s facturas desde %s",
              guardadas, desde)
    return guardadas


def _slot(atributo: int) -> str:
    """El `id_valor_atributo_N` cuyo `id_atributo_N` es el que se busca.

    Ver el punto 4 de arriba: la posición no es fija, así que se pregunta por
    el atributo y no por el slot.
    """
    ramas = " ".join(
        f"WHEN dfc.id_atributo_{i} = {atributo} THEN dfc.id_valor_atributo_{i}"
        for i in range(1, 6)
    )
    return f"(CASE {ramas} END)"


def _sql_where(condicion: str) -> str:
    """La consulta, con el WHERE que le pidan.

    Una factura sola y un día entero salen de la MISMA consulta: preguntarle a
    Asinfo cuesta 650 ms fijos, así que traer las 200 facturas de un día sale
    lo mismo que traer una — y es exactamente lo que hace `precargar`.
    """
    return f"""
SELECT LTRIM(RTRIM(ISNULL(fc.numero, '')))                       AS numero,
       LTRIM(RTRIM(ISNULL(pr.nombre_subcategoria_producto, ''))) AS tela,
       ISNULL(NULLIF(LTRIM(RTRIM(ISNULL(col.codigo, ''))), ''),
              RIGHT(RTRIM(ISNULL(pr.codigo, '')), 3))            AS codigo,
       LTRIM(RTRIM(ISNULL(pr.nombre_comercial, '')))             AS producto,
       LTRIM(RTRIM(ISNULL(pr.nombre_categoria_producto, '')))    AS categoria,
       LTRIM(RTRIM(ISNULL(col.nombre, '')))                      AS color,
       LTRIM(RTRIM(ISNULL(cal.descripcion, ISNULL(cal.nombre, '')))) AS calidad,
       fc.id_documento                                           AS doc,
       dfc.cantidad                                              AS cantidad,
       dfc.precio                                                AS precio,
       dfc.precio_linea                                          AS bruto,
       dfc.descuento_linea                                       AS descuento,
       dfc.porcentaje_descuento                                  AS pct1,
       dfc.porcentaje_descuento_2                                AS pct2
  FROM factura_cliente fc
  JOIN detalle_factura_cliente dfc
    ON dfc.id_factura_cliente = fc.id_factura_cliente
  JOIN producto pr ON pr.id_producto = dfc.id_producto
  LEFT JOIN valor_atributo col ON col.id_valor_atributo = {_slot(ATRIBUTO_COLOR)}
  LEFT JOIN valor_atributo cal ON cal.id_valor_atributo = {_slot(ATRIBUTO_CALIDAD)}
 WHERE {condicion}
   AND fc.estado <> 0
 ORDER BY fc.numero, pr.nombre_subcategoria_producto, col.nombre
"""


def _sql(numero: str) -> str:
    """Una factura."""
    return _sql_where(f"fc.numero = '{numero}'")


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _calidad_es(texto) -> str:
    """PRI / PRIMERA → Primera. El maestro escribe en mayúscula y en código."""
    t = (texto or "").strip()
    if t.upper().startswith("PRI"):
        return "Primera"
    if t.upper().startswith("SEG"):
        return "Segunda"
    return t.capitalize()


def _tipo_doc(filas: list[dict]) -> str:
    """`factura`, `nota-credito`, `devolucion` u `otro`. Ver el punto 7.

    Se mira el primer renglón que traiga un número: todos los renglones son
    del mismo documento, y un `doc` que no vino (una fila de un test viejo,
    una caché de antes) cae en `otro`, que se comporta como una factura.
    """
    for f in filas:
        try:
            return _DOCS.get(int(f.get("doc")), "otro")
        except (TypeError, ValueError):
            continue
    return "otro"


def _agrupar(filas: list[dict]) -> dict:
    """Los renglones crudos de Asinfo, agrupados por tela + color + calidad.

    El precio entra en la clave a propósito: dos rollos de la misma tela a
    precios distintos son dos hechos distintos, y promediarlos escondería el
    que se vendió mal.
    """
    doc = _tipo_doc(filas)
    # ⭐ La nota de crédito no tiene kilos ni rollos: su `cantidad` es una
    #    unidad y su precio es el importe entero (punto 7).
    sin_kilos = doc == "nota-credito"
    grupos: dict[tuple, dict] = {}
    servicios: list[dict] = []
    kg = 0.0
    rollos = 0
    bruto = 0.0
    descuento = 0.0
    pcts: list[tuple[float, float]] = []

    for f in filas:
        cant = _num(f.get("cantidad"))
        b = _num(f.get("bruto"))
        d = _num(f.get("descuento"))
        bruto += b
        descuento += d

        if (f.get("categoria") or "").strip().upper() == CATEGORIA_SERVICIOS:
            servicios.append({
                "nombre": (f.get("producto") or "").strip(),
                "cantidad": cant,
                "total": round(b - d, 2),
            })
            continue

        # ⭐ Los tramos se miran SÓLO en la mercadería. El flete entra como un
        # renglón más y no lleva el descuento del cliente: con él adentro había
        # dos pares distintos, la regla de abajo se callaba y el renglón decía
        # "Descuento" a secas. TMT 2026-08-25 (dueña): *"poner % de descuento"*.
        par = (_num(f.get("pct1")), _num(f.get("pct2")))
        if par not in pcts:
            pcts.append(par)

        tela = (f.get("tela") or "").strip() or (f.get("producto") or "").strip()
        clave = (tela, (f.get("codigo") or "").strip(),
                 (f.get("color") or "").strip(),
                 _calidad_es(f.get("calidad")), round(_num(f.get("precio")), 4))
        g = grupos.get(clave)
        if g is None:
            g = grupos[clave] = {
                "tela": clave[0], "codigo": clave[1], "color": clave[2],
                "calidad": clave[3], "precio": clave[4],
                "rollos": 0, "kg": 0.0, "total": 0.0,
            }
        g["rollos"] += 1
        g["kg"] += cant
        g["total"] += b - d
        kg += cant
        rollos += 1

    # Sin kilos que ordenar, el renglón que más pesa es el que más plata
    # acredita: es lo primero que se mira en una nota de crédito.
    lineas = sorted(grupos.values(),
                    key=lambda g: (-(g["total"] if sin_kilos else g["kg"]),
                                   g["tela"], g["color"]))
    for g in lineas:
        g["kg"] = None if sin_kilos else round(g["kg"], 2)
        g["total"] = round(g["total"], 2)
        if sin_kilos:
            g["rollos"] = None

    # ⭐ El pie se arma DESDE ARRIBA con las cifras ya redondeadas, en el mismo
    # orden en que se leen. La pantalla muestra bruto, descuento e IVA con dos
    # decimales y el que mira los suma con el ojo; si alguno de los pasos usa
    # el número largo de adentro, la columna queda un centavo despegada de lo
    # que se ve. Pasó dos veces el mismo día y para los dos lados: la 182382
    # decía 174,45 donde la vista sumaba 174,44, y al arreglar sólo el último
    # paso la 182574 pasó a decir 202,03 donde la vista sumaba 202,04 — porque
    # el neto salía de restar los largos (175,6837 → 175,68) y no los que
    # están a la vista (210,15 − 34,46 = 175,69).
    bruto = round(bruto, 2)
    descuento = round(descuento, 2)
    neto = round(bruto - descuento, 2)
    iva = round(neto * IVA, 2)
    return {
        "doc": doc,
        "titulo": TITULOS.get(doc, TITULO_DEFAULT),
        "lineas": lineas,
        "servicios": servicios,
        "totales": {
            "rollos": None if sin_kilos else rollos,
            "kg": None if sin_kilos else round(kg, 2),
            "bruto": bruto,
            "descuento": descuento,
            "neto": neto,
            "iva": iva,
            "total": round(neto + iva, 2),
            # Los dos tramos, sólo si TODOS los renglones llevan los mismos.
            # Con dos escalones distintos en la misma factura un solo par
            # mentiría — mejor no decir nada que decir el de una fila sola.
            "pct1": pcts[0][0] if len(pcts) == 1 else None,
            "pct2": pcts[0][1] if len(pcts) == 1 else None,
            # El que DIO, sobre el bruto. Es la red: con dos escalones
            # distintos en la misma factura no hay un par que nombrar, y un
            # renglón que dice "Descuento" y nada más obliga a sacar la cuenta
            # a mano. Éste siempre se puede decir.
            "pct_efectivo": round(descuento / bruto * 100, 1) if bruto else 0.0,
        },
    }


def _recordar(numero: str, res: dict) -> None:
    """Guarda en memoria, que es más rápido todavía que ir a Postgres."""
    with _CANDADO:
        if len(_CACHE) >= _TOPE_CACHE:
            _CACHE.clear()
        _CACHE[numero] = (time.monotonic(), res)


def en_cache(numero) -> dict | None:
    """Lo que ya está guardado de esta factura, SIN preguntarle a Asinfo.

    → el mismo diccionario que `que_se_llevo`, o `None` si todavía no se sabe.

    Existe para que la ficha de la factura pinte el detalle DE UNA cuando ya
    está guardado (que es casi siempre, porque el calentador lo va llenando):
    ahí no hace falta el segundo pedido que va a buscarlo, ni el cartelito de
    "buscando el detalle" que parpadea antes de que llegue. Si todavía no se
    sabe, devuelve None y la ficha lo pide aparte como siempre — nunca cruza el
    puente, porque el sentido de esto es no hacer esperar a la pantalla.
    """
    num = (numero or "").strip()
    if not _NUMERO_RE.match(num):
        return None

    ahora = time.monotonic()
    with _CANDADO:
        guardado = _CACHE.get(num)
        if guardado and (ahora - guardado[0]) < _TTL_SEGS:
            return guardado[1]

    guardado_base = _de_la_base(num)
    if guardado_base is not None:
        _recordar(num, guardado_base)
    return guardado_base


def que_se_llevo(numero) -> dict:
    """Qué salió en esta factura, según Asinfo.

    → `{"estado": ..., "lineas": [...], "servicios": [...], "totales": {...}}`

    `estado` es uno de:
      · `ok`          — Asinfo contestó y hay renglones.
      · `sin-numero`  — la factura no tiene número SRI (carga vieja o a mano).
      · `sin-puente`  — Metabase no está configurado (pasa en local).
      · `sin-datos`   — Asinfo contestó y no conoce esa factura.
      · `error`       — no se pudo preguntar. NO es lo mismo que `sin-datos`,
                        y por eso son dos estados: un "no hay nada" que en
                        realidad es "no pude preguntar" es la mentira que ya
                        costó el balance del 29/07.
    """
    vacio = {"doc": "", "titulo": TITULO_DEFAULT,
             "lineas": [], "servicios": [], "totales": {}}
    num = (numero or "").strip()
    if not _NUMERO_RE.match(num):
        return {"estado": "sin-numero", **vacio}

    guardado = en_cache(num)
    if guardado is not None:
        return guardado

    from modules._lib import metabase_client

    if not metabase_client.disponible():
        return {"estado": "sin-puente", **vacio}

    try:
        filas, ok = metabase_client.fetch_dataset_estado(
            DB_ASINFO, _sql(num), max_results=500)
    except Exception as e:  # noqa: BLE001 — fail-soft, igual que el resto del puente
        _LOG.warning("que_se_llevo(%s) falló: %s", num, e)
        return {"estado": "error", **vacio}

    if not ok:
        return {"estado": "error", **vacio}
    if not filas:
        return {"estado": "sin-datos", **vacio}

    res = {"estado": "ok", "formato": FORMATO, **_agrupar(filas)}
    _guardar(num, res)
    _recordar(num, res)
    return res
