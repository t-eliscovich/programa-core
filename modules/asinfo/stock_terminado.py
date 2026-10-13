"""El stock de PRODUCTO TERMINADO de Asinfo, tela por tela y color por color.

Lo pidieron los vendedores (13/10/2026): *"quieren ver todo el stock desde
Programa Core"*. Hasta ahí veían sólo lo PARADO y la segunda (Competencia).

Qué es y qué no es:

⭐ **Es lo que hay en la bodega 53, no lo disponible.** Asinfo tiene la columna
`saldo_producto_lote.inventario_comprometido`, pero está en CERO en los 19.317
lotes vivos (medido el 13/10/2026): nadie reserva. Y lo pedido no se puede
restar porque `pedido_cliente.cantidad_por_despachar` está podrido (80% de los
pedidos ya despachados siguen "pendientes"). La pantalla lo tiene que decir.

⭐ **La tela es el SUBGRUPO del producto** (`nombre_subcategoria_producto`) y el
color son las 3 últimas letras del código — la MISMA llave que usa
Competencia (`modules/analisis/asinfo_parado.py`). Si las dos pantallas
agruparan distinto, la misma tela parada mostraría dos kilos diferentes.

⭐ **Calidad y acabado son atributos del LOTE**, no del producto (atributo 2 =
PRI/SEG, atributo 1 = TUB/ABI). Un mismo tela × color puede tener las dos.

⚠ Los atributos se buscan SÓLO para los lotes vivos de la bodega 53: con el
UNPIVOT sobre la tabla `lote` entera la consulta tardaba 25 s; filtrada, 1,7 s.

⚠ **Se cachea sólo el éxito, y se guarda el último bueno.** Es la regla del
29/07 (un timeout cacheado dejó el stock en cero ~10 minutos). Si Asinfo no
contesta, se devuelve la última lectura completa marcada como vieja — nunca
una lista vacía que parece "no hay stock".
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

from modules._lib import metabase_client

_LOG = logging.getLogger("programa_core.asinfo.stock_terminado")

DB_ASINFO = 2
BODEGA_TERMINADO = 53
TTL_SECS = 600

#: Grupos que están en la bodega 53 pero no son producto terminado (dueña
#: 17/08/2026, mismo criterio que Competencia: "tela cruda no debería estar").
GRUPOS_FUERA = ("TELA CRUDA",)

SQL = f"""
WITH ult AS (
    SELECT id_producto, id_lote, saldo,
           ROW_NUMBER() OVER (PARTITION BY id_producto, id_bodega, id_lote
                              ORDER BY fecha DESC, id_saldo_producto_lote DESC) rn
      FROM saldo_producto_lote
     WHERE id_bodega = {BODEGA_TERMINADO}
),
v AS (SELECT id_producto, id_lote, saldo FROM ult WHERE rn = 1 AND saldo > 0),
attr AS (
    SELECT u.id_lote,
           MAX(CASE WHEN va.id_atributo = 2 THEN va.nombre END) AS calidad,
           MAX(CASE WHEN va.id_atributo = 3 THEN va.nombre END) AS color,
           MAX(CASE WHEN va.id_atributo = 1 THEN va.nombre END) AS acabado
      FROM lote l
      UNPIVOT (idv FOR slot IN (
          id_valor_atributo_1, id_valor_atributo_2, id_valor_atributo_3,
          id_valor_atributo_4, id_valor_atributo_5, id_valor_atributo_6,
          id_valor_atributo_7, id_valor_atributo_8, id_valor_atributo_9,
          id_valor_atributo_10)) u
      JOIN valor_atributo va ON va.id_valor_atributo = u.idv
     WHERE u.id_lote IN (SELECT id_lote FROM v)
     GROUP BY u.id_lote
)
SELECT p.nombre_categoria_producto      AS grupo,
       p.nombre_subcategoria_producto   AS tela,
       RIGHT(RTRIM(p.codigo), 3)        AS cod_color,
       a.color, a.calidad, a.acabado,
       COUNT(*)                         AS rollos,
       SUM(v.saldo)                     AS kg
  FROM v
  JOIN producto p ON p.id_producto = v.id_producto
  LEFT JOIN attr a ON a.id_lote = v.id_lote
 GROUP BY p.nombre_categoria_producto, p.nombre_subcategoria_producto,
          RIGHT(RTRIM(p.codigo), 3), a.color, a.calidad, a.acabado
"""

_lock = threading.Lock()
_ultimo: dict = {"t": 0.0, "leido": None, "telas": None}


def _armar(filas: list[dict]) -> list[dict]:
    """Filas planas de Asinfo → telas con sus colores, de más kilos a menos."""
    telas: dict[tuple, dict] = {}
    for r in filas:
        grupo = str(r.get("grupo") or "").strip()
        if grupo.upper() in GRUPOS_FUERA:
            continue
        try:
            kg = float(r.get("kg") or 0)
            rollos = int(r.get("rollos") or 0)
        except (TypeError, ValueError):
            continue
        if kg <= 0:
            continue
        nombre = str(r.get("tela") or "").strip() or "(sin tela)"
        t = telas.setdefault((grupo, nombre), {
            "grupo": grupo, "tela": nombre, "kg": 0.0, "rollos": 0,
            "seg_kg": 0.0, "seg_rollos": 0, "colores": {}})
        cod = str(r.get("cod_color") or "").strip().upper()
        nombre_color = str(r.get("color") or "").strip()
        c = t["colores"].setdefault(cod, {
            "cod": cod, "color": nombre_color or cod, "nombres": [],
            "kg": 0.0, "rollos": 0, "seg_kg": 0.0, "seg_rollos": 0,
            "tub_kg": 0.0, "abi_kg": 0.0})
        # Algunos rollos del mismo código traen el color escrito distinto
        # ("NEGRO" / "NEGRO OSC"): se muestra el primero, pero el buscador
        # mira todos — si no, buscar "negro" pierde kilos.
        if nombre_color and nombre_color not in c["nombres"]:
            c["nombres"].append(nombre_color)
        seg = str(r.get("calidad") or "").strip().upper() == "SEG"
        acab = str(r.get("acabado") or "").strip().upper()
        for x in (t, c):
            x["kg"] += kg
            x["rollos"] += rollos
            if seg:
                x["seg_kg"] += kg
                x["seg_rollos"] += rollos
        if acab == "ABI":
            c["abi_kg"] += kg
        elif acab == "TUB":
            c["tub_kg"] += kg
    out = []
    for t in telas.values():
        t["colores"] = sorted(t["colores"].values(), key=lambda c: -c["kg"])
        out.append(t)
    out.sort(key=lambda t: -t["kg"])
    return out


def por_tela(*, ahora=None) -> dict:
    """El stock armado por tela → color, con la hora de la lectura.

    Devuelve `{"telas": [...], "leido": datetime|None, "viejo": bool}`.
    `telas` es `[]` sólo si Asinfo NUNCA contestó desde que arrancó el proceso;
    `viejo=True` quiere decir "es la última lectura buena, Asinfo no contestó
    ahora".
    """
    now = time.time()
    with _lock:
        if _ultimo["telas"] is not None and now - _ultimo["t"] < TTL_SECS:
            return {"telas": _ultimo["telas"], "leido": _ultimo["leido"], "viejo": False}
    filas, ok = metabase_client.fetch_dataset_estado(DB_ASINFO, SQL, max_results=20000)
    if ok:
        telas = _armar(filas)
        leido = ahora or datetime.now(UTC)
        with _lock:
            _ultimo.update(t=now, leido=leido, telas=telas)
        return {"telas": telas, "leido": leido, "viejo": False}
    _LOG.warning("stock_terminado: Asinfo no contestó; uso la última lectura buena")
    with _lock:
        return {"telas": _ultimo["telas"] or [], "leido": _ultimo["leido"],
                "viejo": _ultimo["telas"] is not None}
