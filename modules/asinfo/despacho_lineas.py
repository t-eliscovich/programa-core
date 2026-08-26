"""Qué salió por una guía de despacho — rollo por rollo, con su lote.

TMT 2026-08-26 (dueña): *"desde acá quiero links a facturas y a despachos"*,
parada en **Guías del día**. El número de la factura ya llevaba a algún lado;
el de la guía era texto muerto. Y la guía es justo lo que hay que abrir cuando
la fila sale en rojo —los kilos que salieron no son los que se facturaron—:
ahí se ve CUÁL rollo no coincide.

## Lo que hay que saber

1. **La guía se mide con la misma vara que la pantalla del día**: sólo la
   bodega de producto terminado (`BODEGA_PT`) y sólo guías sin anular. Si acá
   se contara otra cosa, los kilos de la ficha no darían los del renglón que
   se clickeó, que es lo primero que alguien va a comparar.
2. **La factura cuelga del RENGLÓN, no de la guía**: cada rollo despachado
   guarda su `id_detalle_despacho_cliente`, y el renglón de la factura lo
   apunta. Por eso una guía puede terminar en dos facturas, y por eso se puede
   decir, rollo por rollo, con cuántos kilos se facturó cada uno.
3. **El lote es el número del rollo** (`codigo_lote`). Es el dato con el que
   se va a buscar la tela a la bodega cuando el cliente reclama.
4. **El renglón del despacho NO tiene atributos.** La factura los trae en
   cinco pares `id_atributo_N`; el despacho no tiene ninguno, así que el
   color sale del nombre del producto —*Pique Especial TOPACIO* menos su
   subcategoría *Pique Especial*— y el código, de las tres últimas letras del
   código del producto, que es de donde ya lo saca `analisis/asinfo_parado`.
   La CALIDAD no está en ninguna parte del despacho: no se muestra, en vez de
   ponerle "Primera" a todo.

Fail-soft, como todo lo que cuelga de Asinfo: si el ERP no contesta, la
pantalla lo dice y no levanta.
"""
from __future__ import annotations

import logging
import re

_LOG = logging.getLogger("programa_core.asinfo.despacho_lineas")

DB_ASINFO = 2

#: DES-000096542. Es lo único que se interpola en el SQL, así que se valida
#: entero: tres letras, guion y nueve dígitos.
_NUMERO_RE = re.compile(r"^[A-Z]{2,4}-\d{9}$")

#: La bodega de producto terminado. La MISMA que cuenta `dia_despacho`.
BODEGA_PT = 53

#: Cuánto puede diferir lo facturado de lo despachado sin que sea una
#: diferencia. El mismo umbral que la pantalla del día.
UMBRAL_KG = 0.05


def _sql(numero: str) -> str:
    return f"""
SELECT LTRIM(RTRIM(ISNULL(dc.numero, '')))                       AS guia,
       CONVERT(varchar(10), dc.fecha, 120)                       AS fecha,
       LTRIM(RTRIM(ISNULL(em.nombre_comercial, '')))             AS cliente,
       LTRIM(RTRIM(ISNULL(em.nombre_fiscal, '')))                AS cliente_fiscal,
       LTRIM(RTRIM(ISNULL(pr.nombre_subcategoria_producto, ''))) AS tela,
       RIGHT(RTRIM(ISNULL(pr.codigo, '')), 3)                    AS codigo,
       LTRIM(RTRIM(ISNULL(pr.nombre_comercial, '')))             AS producto,
       LTRIM(RTRIM(ISNULL(ddc.codigo_lote, '')))                 AS lote,
       ddc.cantidad                                              AS kg,
       LTRIM(RTRIM(ISNULL(fc.numero, '')))                       AS factura,
       dfc.cantidad                                              AS kg_factura
  FROM despacho_cliente dc
  JOIN detalle_despacho_cliente ddc
    ON ddc.id_despacho_cliente = dc.id_despacho_cliente
  JOIN producto pr ON pr.id_producto = ddc.id_producto
  LEFT JOIN empresa em ON em.id_empresa = dc.id_empresa
  LEFT JOIN detalle_factura_cliente dfc
    ON dfc.id_detalle_despacho_cliente = ddc.id_detalle_despacho_cliente
  LEFT JOIN factura_cliente fc
    ON fc.id_factura_cliente = dfc.id_factura_cliente AND fc.estado <> 0
 WHERE dc.numero = '{numero}'
   AND dc.fecha_anulacion IS NULL
   AND ddc.id_bodega = {BODEGA_PT}
 ORDER BY pr.nombre_subcategoria_producto, pr.nombre_comercial, ddc.codigo_lote
"""


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _color(producto: str, tela: str) -> str:
    """*Pique Especial TOPACIO* menos *Pique Especial* = TOPACIO.

    El despacho no guarda el color: lo único que hay es el nombre del
    producto, que es la tela y el color pegados. Si el nombre no empieza con
    la subcategoría —pasa cuando el maestro está escrito raro— se devuelve
    vacío en vez de un pedazo cortado a ciegas.
    """
    p = (producto or "").strip()
    t = (tela or "").strip()
    if t and p.upper().startswith(t.upper()):
        return p[len(t):].strip()
    return ""


def _fecha_es(v) -> str:
    t = str(v or "")[:10]
    return f"{t[8:10]}/{t[5:7]}/{t[0:4]}" if len(t) == 10 and t[4] == "-" else t


def armar(filas: list[dict]) -> dict:
    """Los renglones crudos, ordenados y con los kilos comparados."""
    cab = filas[0]
    lineas = []
    kg = 0.0
    kg_fact = 0.0
    facturas: list[str] = []
    for f in filas:
        salieron = round(_num(f.get("kg")), 2)
        factura = (f.get("factura") or "").strip()
        facturados = round(_num(f.get("kg_factura")), 2) if factura else None
        lineas.append({
            "tela": (f.get("tela") or "").strip() or (f.get("producto") or "").strip(),
            "codigo": (f.get("codigo") or "").strip(),
            "color": _color(f.get("producto"), f.get("tela")),
            "lote": (f.get("lote") or "").strip(),
            "kg": salieron,
            "factura": factura,
            "kg_factura": facturados,
            # ⭐ El renglón en rojo de la pantalla del día, abierto: acá se ve
            #    CUÁL rollo salió con un peso y se facturó con otro.
            "difiere": bool(factura and abs(facturados - salieron) > UMBRAL_KG),
        })
        kg += salieron
        if factura:
            kg_fact += facturados
            if factura not in facturas:
                facturas.append(factura)
    return {
        "cabecera": {
            "guia": (cab.get("guia") or "").strip(),
            "fecha": _fecha_es(cab.get("fecha")),
            "cliente": (cab.get("cliente") or "").strip(),
            "cliente_fiscal": (cab.get("cliente_fiscal") or "").strip(),
            "facturas": facturas,
        },
        "lineas": lineas,
        "totales": {
            "rollos": len(lineas),
            "kg": round(kg, 2),
            "kg_factura": round(kg_fact, 2) if facturas else None,
            "sin_factura": sum(1 for ln in lineas if not ln["factura"]),
        },
    }


def que_salio(numero) -> dict:
    """Qué salió por esta guía, según Asinfo.

    `estado`: `ok`, `sin-numero`, `sin-datos`, `sin-puente` o `error`. Los dos
    últimos son distintos a propósito: un "no hay nada" que en realidad es "no
    pude preguntar" es la mentira que ya costó el balance del 29/07.
    """
    vacio = {"cabecera": {}, "lineas": [], "totales": {}}
    num = (numero or "").strip().upper()
    if not _NUMERO_RE.match(num):
        return {"estado": "sin-numero", **vacio}

    from modules._lib import metabase_client

    if not metabase_client.disponible():
        return {"estado": "sin-puente", **vacio}
    try:
        filas, ok = metabase_client.fetch_dataset_estado(
            DB_ASINFO, _sql(num), max_results=1000)
    except Exception as e:  # noqa: BLE001 — fail-soft, como todo el puente
        _LOG.warning("que_salio(%s) falló: %s", num, e)
        return {"estado": "error", **vacio}
    if not ok:
        return {"estado": "error", **vacio}
    if not filas:
        return {"estado": "sin-datos", **vacio}
    return {"estado": "ok", **armar(filas)}
