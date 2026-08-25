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

#: IVA vigente. El mismo 15% que usa la lista de precios.
IVA = 0.15

#: Cuánto vale la foto. Una factura vieja no cambia nunca; una de hoy puede
#: recibir un renglón más en los minutos siguientes a emitirse.
_TTL_SEGS = 600.0
_TOPE_CACHE = 200

_CACHE: dict[str, tuple[float, dict]] = {}
_CANDADO = threading.Lock()


def reset_cache() -> None:
    """Olvida lo cacheado (tests, y cualquiera que quiera el dato fresco)."""
    with _CANDADO:
        _CACHE.clear()


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


def _sql(numero: str) -> str:
    return f"""
SELECT LTRIM(RTRIM(ISNULL(pr.nombre_subcategoria_producto, ''))) AS tela,
       ISNULL(NULLIF(LTRIM(RTRIM(ISNULL(col.codigo, ''))), ''),
              RIGHT(RTRIM(ISNULL(pr.codigo, '')), 3))            AS codigo,
       LTRIM(RTRIM(ISNULL(pr.nombre_comercial, '')))             AS producto,
       LTRIM(RTRIM(ISNULL(pr.nombre_categoria_producto, '')))    AS categoria,
       LTRIM(RTRIM(ISNULL(col.nombre, '')))                      AS color,
       LTRIM(RTRIM(ISNULL(cal.descripcion, ISNULL(cal.nombre, '')))) AS calidad,
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
 WHERE fc.numero = '{numero}'
   AND fc.estado <> 0
 ORDER BY pr.nombre_subcategoria_producto, col.nombre
"""


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


def _agrupar(filas: list[dict]) -> dict:
    """Los renglones crudos de Asinfo, agrupados por tela + color + calidad.

    El precio entra en la clave a propósito: dos rollos de la misma tela a
    precios distintos son dos hechos distintos, y promediarlos escondería el
    que se vendió mal.
    """
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
        par = (_num(f.get("pct1")), _num(f.get("pct2")))
        if par not in pcts:
            pcts.append(par)

        if (f.get("categoria") or "").strip().upper() == CATEGORIA_SERVICIOS:
            servicios.append({
                "nombre": (f.get("producto") or "").strip(),
                "cantidad": cant,
                "total": round(b - d, 2),
            })
            continue

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

    lineas = sorted(grupos.values(), key=lambda g: (-g["kg"], g["tela"], g["color"]))
    for g in lineas:
        g["kg"] = round(g["kg"], 2)
        g["total"] = round(g["total"], 2)

    neto = bruto - descuento
    return {
        "lineas": lineas,
        "servicios": servicios,
        "totales": {
            "rollos": rollos,
            "kg": round(kg, 2),
            "bruto": round(bruto, 2),
            "descuento": round(descuento, 2),
            "neto": round(neto, 2),
            "iva": round(neto * IVA, 2),
            "total": round(neto * (1 + IVA), 2),
            # Los dos tramos, sólo si TODOS los renglones llevan los mismos.
            # Con dos escalones distintos en la misma factura un solo par
            # mentiría — mejor no decir nada que decir el de una fila sola.
            "pct1": pcts[0][0] if len(pcts) == 1 else None,
            "pct2": pcts[0][1] if len(pcts) == 1 else None,
        },
    }


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
    vacio = {"lineas": [], "servicios": [], "totales": {}}
    num = (numero or "").strip()
    if not _NUMERO_RE.match(num):
        return {"estado": "sin-numero", **vacio}

    ahora = time.monotonic()
    with _CANDADO:
        guardado = _CACHE.get(num)
        if guardado and (ahora - guardado[0]) < _TTL_SEGS:
            return guardado[1]

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

    res = {"estado": "ok", **_agrupar(filas)}
    with _CANDADO:
        if len(_CACHE) >= _TOPE_CACHE:
            _CACHE.clear()
        _CACHE[num] = (ahora, res)
    return res
