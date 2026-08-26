"""La factura tal cual la imprime Asinfo, para que el vendedor la comparta.

TMT 2026-08-26 (dueña): *"que la factura imite exactamente como lo hace
asinfo… así no piensan que es distinta"*. El vendedor está parado frente al
cliente y lo que le piden es LA factura, no un resumen: si la hoja que manda
tiene otro dibujo, la conversación se va a discutir si es la misma o no.

Este módulo devuelve todo lo que el papel necesita. La plantilla sólo dibuja.

## Lo que hay que saber del papel de Asinfo (medido el 26/08/2026)

1. **En el papel TODO va con IVA; en la base todo va neto.** Y el orden de
   las cuentas importa hasta el centavo — se sacó comparando los 26 renglones
   de la 001-099-000182675 contra el papel, uno por uno:

       precio unitario = redondear(precio × 1,15)
       descuento       = redondear(descuento_linea × 1,15)
       total           = redondear(precio_linea × 1,15) − descuento

   El total se arma restando dos números YA REDONDEADOS, no redondeando la
   resta: con la resta primero, tres renglones de esa factura daban un centavo
   distinto y el pie quedaba en 2.620,29 contra los 2.620,28 del papel. Con
   esta regla, los 26 renglones y las dos sumas del pie dan exactamente lo
   mismo que Asinfo.
2. **El recuadro del SRI no está en `factura_cliente`**, está en
   `factura_clienteSRI`: la autorización de 49 dígitos, la clave de acceso, la
   fecha de autorización, el ambiente y el código de forma de pago. Casi la
   damos por perdida buscándola en `comprobante_electronico_sri`, que está
   vacía. Una factura de 82.112 no tiene esa fila: la hoja sale igual, sin el
   recuadro, en vez de romperse.
3. **El orden de los renglones es `producto.codigo` DESCENDENTE**, no el de
   carga. Se ve en cualquier factura: RICEL, RIBAZ, RAVIN, RACRU…
4. **La columna que el papel titula "Calidad" muestra el COLOR.** Los
   atributos del renglón son 1=Acabado (TUB), 2=Calidad (PRI) y 3=Color, y el
   papel imprime el acabado y el color. La calidad de verdad no la imprime
   nunca. Acá se traen los dos y la plantilla decide.
5. **El "Total" de kilos sale de `dfc.cantidad`** — y no todo renglón es un
   kilo: el flete entra como una unidad (categoría SERVICIOS). Se suma aparte,
   igual que en `factura_lineas`.
6. **El cuadro de descuentos del papel NO resta.** Abajo a la izquierda,
   Asinfo imprime *Valor Factura − Descuento Contado − Descuento Volumen =
   Total*, y esa resta no da: en la 001-099-000182675 dice 3.207,19 − 146,42 −
   389,47, que son 2.671,30, contra los 2.620,30 de la factura. La cuenta que
   hace es ésta, sacada de comparar DOS papeles con segundos tramos distintos
   (14% y 8%):

       Valor Factura     = Σ redondear(precio_linea × 1,15)
       Descuento Contado = redondear(bruto × pct1% × 1,05)
       Descuento Volumen = redondear((bruto − bruto × pct1%) × pct2% × 1,05)

   El 1,05 es el error: donde va el IVA, Asinfo pone 5%. Se copia igual —el
   cliente ya tiene ese papel y un número distinto lo hace dudar de si es la
   misma factura—, y el *Total* del cuadro es el total de verdad, así que la
   hoja nunca miente en el número que importa. ⚠ El 1,05 podría ser
   `1 + pct1/100` en vez de una constante: en las dos facturas medidas el
   contado era 5%, como en 299.425 de los 313.000 renglones de 2025-2026.

7. **La guía de remisión cuelga del DESPACHO, no de la factura.**
   `guia_remision` tiene las dos columnas y la de la factura viene siempre en
   blanco: se une por `id_despacho_cliente`. Con el enganche por factura el
   campo salía vacío SIEMPRE. Y no se puede multiplicar la consulta: de los
   56.412 despachos con guía, ninguno tiene dos. Que a veces salga vacío es
   correcto — la 001-099-000182675 no tiene guía y el papel de Asinfo tampoco
   la muestra.

8. **Hay facturas de 192 renglones.** El 6,5% pasa de 26, que es lo que entra
   en una carilla: la hoja PAGINA sola y repite el encabezado de la tabla.

Fail-soft, como todo lo que cuelga de Asinfo: si el ERP no contesta, la hoja
lo dice y la pantalla sigue viva. Nunca levanta.
"""
from __future__ import annotations

import logging
import re
import threading
import time

_LOG = logging.getLogger("programa_core.asinfo.factura_papel")

DB_ASINFO = 2

#: El número del SRI, 001-099-000182675. Es lo único que se interpola.
_NUMERO_RE = re.compile(r"^\d{3}-\d{3}-\d{9}$")

ATRIBUTO_ACABADO = 1
ATRIBUTO_CALIDAD = 2
ATRIBUTO_COLOR = 3

CATEGORIA_SERVICIOS = "SERVICIOS"
IVA = 0.15

#: Quién emite. Es Intela y no cambia; lo único que se lee de Asinfo son las
#: direcciones, que vienen en la fila del SRI. El RUC sale de la clave de
#: acceso —posiciones 11 a 23— y sólo se cae al fijo si no hay clave.
RUC_INTELA = "1791125762001"
NOMBRE_INTELA = "INTELA INDUSTRIA TEXTIL LATINOAMERICANA CIA. LTDA"
CONTRIBUYENTE_ESPECIAL = "1478"
DIRECCION_INTELA = "DUCHICELA N2-150 9 DE AGOSTO CALDERON"

#: Las formas de pago del SRI. El papel imprime el código Y el texto, y el
#: código que guarda Asinfo es el del SRI, no el suyo propio.
#: El factor con el que Asinfo arma el cuadro de descuentos de abajo a la
#: izquierda. Donde tendría que ir el IVA (1,15) pone 1,05, y por eso ese
#: cuadro no resta. Se copia tal cual: ver el punto 6.
FACTOR_CUADRO = 1.05

FORMAS_PAGO = {
    "01": "Sin utilización del sistema financiero",
    "15": "Compensación de deudas",
    "16": "Tarjeta de débito",
    "17": "Dinero electrónico",
    "18": "Tarjeta prepago",
    "19": "Tarjeta de crédito",
    "20": "Otros con utilización del sistema financiero",
    "21": "Endoso de títulos",
}

#: Sube cuando cambia la forma de lo que se guarda: las filas viejas dejan de
#: leerse y se reescriben solas. Mismo mecanismo que `factura_lineas`.
FORMATO = 1

_TTL_SEGS = 600.0
_TOPE_CACHE = 100
_CACHE: dict[str, tuple[float, dict]] = {}
_CANDADO = threading.Lock()


def reset_cache() -> None:
    with _CANDADO:
        _CACHE.clear()


def _slot(atributo: int) -> str:
    """El valor del atributo pedido, mire en el slot que mire.

    El orden de los cinco pares lo pone el producto, no Asinfo: se pregunta
    por el ID del atributo y nunca por la posición.
    """
    ramas = " ".join(
        f"WHEN dfc.id_atributo_{i} = {atributo} THEN dfc.id_valor_atributo_{i}"
        for i in range(1, 6)
    )
    return f"(CASE {ramas} END)"


def _sql(numero: str) -> str:
    """Todo el papel en UNA consulta.

    Cruzar el puente de Metabase cuesta 650 ms fijos, así que hacer dos
    consultas —la cabecera y los renglones— costaría el doble por nada: la
    cabecera repetida en cada fila sale gratis y la más larga tiene 192.
    """
    return f"""
SELECT LTRIM(RTRIM(ISNULL(fc.numero, '')))                    AS numero,
       fc.fecha                                               AS fecha,
       fc.id_documento                                        AS doc,
       LTRIM(RTRIM(ISNULL(fc.descripcion, '')))               AS referencia,
       LTRIM(RTRIM(ISNULL(sri.autorizacion, '')))             AS autorizacion,
       LTRIM(RTRIM(ISNULL(sri.clave_acceso, '')))             AS clave,
       sri.ambiente                                           AS ambiente,
       sri.tipo_emision                                       AS tipo_emision,
       sri.fecha_autorizacion                                 AS fecha_autorizacion,
       LTRIM(RTRIM(ISNULL(gui.numero, '')))                   AS guia,
       LTRIM(RTRIM(ISNULL(sri.direccion_matriz, '')))         AS emi_matriz,
       LTRIM(RTRIM(ISNULL(sri.direccion_sucursal, '')))       AS emi_sucursal,
       sri.base_imponible_diferente_cero                      AS base,
       sri.monto_iva                                          AS iva_sri,
       LTRIM(RTRIM(ISNULL(sri.codigo_forma_pago_sri, '')))    AS forma_pago,
       LTRIM(RTRIM(ISNULL(em.nombre_fiscal, '')))             AS cli_razon,
       LTRIM(RTRIM(ISNULL(em.nombre_comercial, '')))          AS cli_comercial,
       LTRIM(RTRIM(ISNULL(em.identificacion, '')))            AS cli_ruc,
       LTRIM(RTRIM(ISNULL(ubi.direccion1, '')))               AS cli_direccion,
       LTRIM(RTRIM(ISNULL(ciu.nombre, '')))                   AS cli_ciudad,
       LTRIM(RTRIM(ISNULL(NULLIF(LTRIM(RTRIM(ISNULL(fc.email, ''))), ''),
                          ISNULL(em.email1, ''))))            AS cli_email,
       LTRIM(RTRIM(ISNULL(dir.telefono1, '')))                AS cli_telefono,
       LTRIM(RTRIM(ISNULL(pr.codigo, '')))                    AS codigo,
       LTRIM(RTRIM(ISNULL(pr.nombre_comercial, '')))          AS descripcion,
       LTRIM(RTRIM(ISNULL(pr.nombre_categoria_producto, '')))  AS categoria,
       LTRIM(RTRIM(ISNULL(aca.nombre, '')))                   AS acabado,
       LTRIM(RTRIM(ISNULL(col.nombre, '')))                   AS color,
       LTRIM(RTRIM(ISNULL(cal.descripcion, ISNULL(cal.nombre, '')))) AS calidad,
       dfc.cantidad                                           AS cantidad,
       dfc.precio                                             AS precio,
       dfc.precio_linea                                       AS bruto,
       dfc.descuento_linea                                    AS descuento,
       dfc.porcentaje_descuento                               AS pct1,
       dfc.porcentaje_descuento_2                             AS pct2
  FROM factura_cliente fc
  JOIN detalle_factura_cliente dfc
    ON dfc.id_factura_cliente = fc.id_factura_cliente
  JOIN producto pr ON pr.id_producto = dfc.id_producto
  LEFT JOIN factura_clienteSRI sri
    ON sri.id_factura_cliente = fc.id_factura_cliente
  LEFT JOIN guia_remision gui
    ON gui.id_despacho_cliente = fc.id_despacho_cliente
  LEFT JOIN empresa em ON em.id_empresa = fc.id_empresa
  LEFT JOIN direccion_empresa dir
    ON dir.id_direccion_empresa = fc.id_direccion_empresa
  LEFT JOIN ubicacion ubi ON ubi.id_ubicacion = dir.id_ubicacion
  LEFT JOIN ciudad ciu ON ciu.id_ciudad = dir.id_ciudad
  LEFT JOIN valor_atributo col ON col.id_valor_atributo = {_slot(ATRIBUTO_COLOR)}
  LEFT JOIN valor_atributo cal ON cal.id_valor_atributo = {_slot(ATRIBUTO_CALIDAD)}
  LEFT JOIN valor_atributo aca ON aca.id_valor_atributo = {_slot(ATRIBUTO_ACABADO)}
 WHERE fc.numero = '{numero}'
   AND fc.estado <> 0
 ORDER BY pr.codigo DESC, dfc.id_detalle_factura_cliente
"""


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fecha(v) -> str:
    """Lo que devuelve Metabase puede venir con hora y con Z."""
    t = str(v or "")[:10]
    if len(t) == 10 and t[4] == "-":
        return f"{t[8:10]}/{t[5:7]}/{t[0:4]}"
    return t


def armar(filas: list[dict]) -> dict:
    """Los renglones crudos de Asinfo, convertidos en la hoja."""
    cab = filas[0]
    renglones: list[dict] = []
    articulos: dict[str, int] = {}
    kilos = 0.0
    bruto = 0.0            # Σ precio_linea CON IVA — el "Valor Factura"
    neto_sin_iva = 0.0     # Σ precio_linea sin IVA — la base del cuadro
    descuento = 0.0
    total = 0.0
    pcts: list[tuple[float, float]] = []

    for i, f in enumerate(filas, start=1):
        b = _num(f.get("bruto"))
        d = _num(f.get("descuento"))
        cant = _num(f.get("cantidad"))
        servicio = (f.get("categoria") or "").strip().upper() == CATEGORIA_SERVICIOS
        # ⭐ Dos redondeos y DESPUÉS la resta. Ver el punto 1 del módulo.
        fila_desc = round(d * (1 + IVA), 2)
        fila_total = round(round(b * (1 + IVA), 2) - fila_desc, 2)
        renglones.append({
            "n": i,
            "codigo": (f.get("codigo") or "").strip(),
            "descripcion": (f.get("descripcion") or "").strip().upper(),
            "acabado": (f.get("acabado") or "").strip(),
            "color": (f.get("color") or "").strip(),
            "calidad": (f.get("calidad") or "").strip(),
            "cantidad": round(cant, 2),
            "precio": round(_num(f.get("precio")) * (1 + IVA), 2),
            "descuento": fila_desc,
            "total": fila_total,
            "servicio": servicio,
        })
        bruto += round(b * (1 + IVA), 2)
        neto_sin_iva += b
        descuento += fila_desc
        total += fila_total
        if not servicio:
            # ⭐ El flete viene con cantidad = 1, que es una unidad y no un
            #    kilo. Sumarlo daría kilos de más — el mismo error que ya se
            #    pagó en `dia_despacho` y en `factura_lineas`.
            kilos += cant
            nombre = (f.get("descripcion") or "").strip()
            articulos[nombre] = articulos.get(nombre, 0) + 1
            par = (_num(f.get("pct1")), _num(f.get("pct2")))
            if par not in pcts:
                pcts.append(par)

    valor_factura = round(bruto, 2)
    descuento = round(descuento, 2)
    # ⭐ El pie del SRI, tal cual lo arma Asinfo:
    #      SUBTOTAL PRECIO        = suma de la columna Total
    #      DESCUENTO              = suma de la columna Descuento
    #      VALOR IVA / VALOR TOTAL = los del SRI (`factura_clienteSRI`), que
    #                                son los que quedaron autorizados
    #      SUBTOTAL SIN IMPUESTOS = SUBTOTAL PRECIO − VALOR IVA
    # Los dos últimos NO se recalculan: el número que el cliente tiene en su
    # comprobante es el del SRI, y dos centavos de diferencia en el total son
    # exactamente lo que hace dudar de si la hoja es la misma factura.
    subtotal_precio = round(total, 2)
    iva = round(_num(cab.get("iva_sri")), 2)
    base = round(_num(cab.get("base")), 2)
    if not iva:
        iva = round(subtotal_precio - subtotal_precio / (1 + IVA), 2)
    total_general = round(base + iva, 2) if base else subtotal_precio
    neto = round(subtotal_precio - iva, 2)
    # El cuadro de abajo a la izquierda, con la cuenta rara de Asinfo (punto 6).
    pct1 = pcts[0][0] if len(pcts) == 1 else None
    pct2 = pcts[0][1] if len(pcts) == 1 else None
    contado = volumen = 0.0
    if pct1:
        contado = round(neto_sin_iva * pct1 / 100 * FACTOR_CUADRO, 2)
        if pct2:
            resto = neto_sin_iva - neto_sin_iva * pct1 / 100
            volumen = round(resto * pct2 / 100 * FACTOR_CUADRO, 2)
    clave = ((cab.get("clave") or "").strip()
             or (cab.get("autorizacion") or "").strip())
    forma = (cab.get("forma_pago") or "").strip()
    return {
        "emisor": {
            "nombre": NOMBRE_INTELA,
            "ruc": clave[10:23] if len(clave) >= 23 and clave[10:23].isdigit()
                   else RUC_INTELA,
            "matriz": (cab.get("emi_matriz") or "").strip() or DIRECCION_INTELA,
            "sucursal": (cab.get("emi_sucursal") or "").strip() or DIRECCION_INTELA,
            "especial": CONTRIBUYENTE_ESPECIAL,
        },
        "cabecera": {
            "numero": (cab.get("numero") or "").strip(),
            "fecha": _fecha(cab.get("fecha")),
            "doc": cab.get("doc"),
            "referencia": (cab.get("referencia") or "").strip(),
            "autorizacion": (cab.get("autorizacion") or "").strip()
                            or (cab.get("clave") or "").strip(),
            "clave": clave,
            "ambiente": "PRODUCCION" if str(cab.get("ambiente")) == "2" else "PRUEBAS",
            "emision": "NORMAL" if str(cab.get("tipo_emision")) == "1" else "INDISPONIBILIDAD",
            "fecha_autorizacion": _fecha(cab.get("fecha_autorizacion")),
            "guia": (cab.get("guia") or "").strip(),
            "forma_pago": forma,
            "forma_pago_texto": FORMAS_PAGO.get(forma, ""),
        },
        "cliente": {
            "razon": (cab.get("cli_razon") or "").strip(),
            "comercial": (cab.get("cli_comercial") or "").strip(),
            "ruc": (cab.get("cli_ruc") or "").strip(),
            "direccion": (cab.get("cli_direccion") or "").strip(),
            "ciudad": (cab.get("cli_ciudad") or "").strip(),
            "email": (cab.get("cli_email") or "").strip(),
            "telefono": (cab.get("cli_telefono") or "").strip(),
        },
        "renglones": renglones,
        "articulos": sorted(articulos.items()),
        "totales": {
            "kilos": round(kilos, 2),
            "renglones": len(renglones),
            "bruto": subtotal_precio,
            "descuento": descuento,
            "neto": neto,
            "iva": iva,
            "total": total_general,
            "pct1": pct1,
            "pct2": pct2,
            "valor_factura": valor_factura,
            "desc_contado": contado,
            "desc_volumen": volumen,
        },
    }


# ---------------------------------------------------------------------------
# La caché, igual que la del detalle: una factura emitida no cambia nunca
# ---------------------------------------------------------------------------
def _de_la_base(numero: str) -> dict | None:
    try:
        import db

        fila = db.fetch_one(
            "SELECT datos FROM scintela.factura_papel WHERE numero = %s",
            (numero,))
    except Exception as e:  # noqa: BLE001 — es una caché, no una fuente
        _LOG.warning("leyendo la caché del papel de %s: %s", numero, e)
        return None
    datos = (fila or {}).get("datos")
    if not isinstance(datos, dict) or datos.get("estado") != "ok":
        return None
    return datos if datos.get("formato") == FORMATO else None


def _guardar(numero: str, res: dict) -> None:
    """Guarda SÓLO el éxito. Un 'no pude preguntar' guardado es una mentira
    que dura para siempre."""
    if res.get("estado") != "ok":
        return
    try:
        import json

        import db

        db.execute(
            "INSERT INTO scintela.factura_papel (numero, datos) "
            "VALUES (%s, %s::jsonb) "
            "ON CONFLICT (numero) DO UPDATE "
            "   SET datos = EXCLUDED.datos, fecha_crea = now()",
            (numero, json.dumps(res)))
    except Exception as e:  # noqa: BLE001 — no poder guardar no rompe la hoja
        _LOG.warning("guardando la caché del papel de %s: %s", numero, e)


def papel(numero) -> dict:
    """La hoja de esta factura, según Asinfo.

    `estado` es uno de `ok`, `sin-numero`, `sin-datos`, `sin-puente` o
    `error`. `sin-datos` y `error` son dos cosas distintas a propósito: un
    "no hay nada" que en realidad es "no pude preguntar" es la mentira que ya
    costó el balance del 29/07.
    """
    vacio = {"emisor": {}, "cabecera": {}, "cliente": {}, "renglones": [],
             "articulos": [], "totales": {}}
    num = (numero or "").strip()
    if not _NUMERO_RE.match(num):
        return {"estado": "sin-numero", **vacio}

    ahora = time.monotonic()
    with _CANDADO:
        guardado = _CACHE.get(num)
        if guardado and (ahora - guardado[0]) < _TTL_SEGS:
            return guardado[1]

    de_la_base = _de_la_base(num)
    if de_la_base is not None:
        with _CANDADO:
            _CACHE[num] = (ahora, de_la_base)
        return de_la_base

    from modules._lib import metabase_client

    if not metabase_client.disponible():
        return {"estado": "sin-puente", **vacio}
    try:
        filas, ok = metabase_client.fetch_dataset_estado(
            DB_ASINFO, _sql(num), max_results=500)
    except Exception as e:  # noqa: BLE001 — fail-soft, como todo el puente
        _LOG.warning("papel(%s) falló: %s", num, e)
        return {"estado": "error", **vacio}
    if not ok:
        return {"estado": "error", **vacio}
    if not filas:
        return {"estado": "sin-datos", **vacio}

    res = {"estado": "ok", "formato": FORMATO, **armar(filas)}
    _guardar(num, res)
    with _CANDADO:
        if len(_CACHE) >= _TOPE_CACHE:
            _CACHE.clear()
        _CACHE[num] = (ahora, res)
    return res


def hoja(numero) -> dict:
    """Lo que la plantilla necesita: la factura y su código de barras.

    Vive acá y no en cada vista porque son TRES pantallas —oficina, vendedor y
    cliente— más el PDF y la foto que salen por WhatsApp, y todas tienen que
    mostrar exactamente la misma hoja. Se usa así:

        render_template("informes/factura_papel.html", **hoja(numf_completo))
    """
    from modules._lib import code128

    p = papel(numero)
    clave = (p.get("cabecera") or {}).get("clave") or ""
    try:
        barras = code128.barras(clave)
        ancho = code128.ancho_total(clave)
    except ValueError:
        # Una clave con algo que no es un dígito no puede dibujar un código de
        # barras honesto: mejor sin código que con uno que lee cualquier cosa.
        barras, ancho = [], 0
    return {"p": p, "barras": barras, "barras_ancho": ancho}
