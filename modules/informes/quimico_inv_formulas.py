"""Físico de químicos que COINCIDE con la pantalla "TOTALES POR TIPO" de
formulas_app (Inventario), no una aproximación.

Contexto (dueña 2026-07-21): la banda QUÍM.$ del flujo y el "Stock Quí." del
balance salían de `tintura.service.stock_quimicos_al_dia`, que es una réplica
APROXIMADA del inventario de formulas: (a) no le sumaba los ajustes JSONB
intra-línea del consumo, (b) contaba los movimientos del día del conteo de
forma EXCLUSIVA, y (c) no dividía /10 el A44 (AV SOFT NI, escamas 10x). Por eso
daba 412.277 cuando formulas mostraba 417.675.

Este módulo replica EXACTO la matemática de formulas_app
(`inventario_math.compute_row` + los builders `compute_consumption_by_date_
terminado` / `get_compras_by_date` / `get_ajustes_by_date` + el último conteo),
valuando cada producto a `productos.us` c/IVA (sal exenta) y sumando por tipo
(Auxiliares / Colorantes poliéster / Colorantes algodón). Así el número del
flujo y del balance es IDÉNTICO al que ve la fábrica en formulas_app.

FINAL es independiente de "desde" (queda anclado al conteo físico), así que sólo
recibimos el corte (= "hasta"). Fail-soft: devuelve None si formulas_db no está.
"""

from __future__ import annotations

import logging
from datetime import date

from modules._lib import formulas_db

_LOG = logging.getLogger("programa_core.quimico_inv_formulas")

# IVA de químicos — mismo criterio que tintura.service (15%, sal exenta). La sal
# se identifica por nombre ('SAL'), igual que formulas_app (match por nombre, no
# por num, porque la sal se consolidó de varias variantes).
_IVA_FACTOR = 1.15


def _tipo_key(num: int, familia: str) -> str:
    """Misma regla que formulas_app _inventario_tipo_key / inventario.html."""
    f = (str(familia or "")).strip().lower()
    if f in ("aux", "poli", "alg", "otros"):
        return f
    try:
        n = int(num)
    except (TypeError, ValueError):
        n = 0
    if n < 100:
        return "aux"
    if n < 200:
        return "poli"
    if n < 300:
        return "alg"
    return "otros"


_SQL = """
WITH conteo AS (
    -- Último conteo físico (última fila de inventario) <= corte, por producto.
    SELECT DISTINCT ON (producto_num)
           producto_num,
           cantidad AS q,
           fecha    AS f
      FROM inventario
     WHERE fecha <= %(corte)s
     ORDER BY producto_num, fecha DESC, id DESC
),
cons AS (
    -- Consumo real = cantidad_kg + ajustes JSONB (dosis sucesivas), por las
    -- órdenes TERMINADAS. Ventana desde el día del conteo INCLUSIVE (el conteo
    -- se asume a primera hora) hasta el corte. Sin conteo → todo <= corte.
    SELECT ol.producto_num,
           SUM(
             ol.cantidad_kg
             + COALESCE((
                 SELECT SUM((e->>'kg')::numeric)
                   FROM jsonb_array_elements(
                          CASE
                            WHEN jsonb_typeof(ol.ajustes) = 'array' THEN ol.ajustes
                            ELSE '[]'::jsonb
                          END) e
                ), 0)
           ) AS q
      FROM orden_lineas ol
      JOIN ordenes o ON o.id = ol.orden_id
      LEFT JOIN conteo c ON c.producto_num = ol.producto_num
     WHERE o.fecha_terminado IS NOT NULL
       AND o.fecha_terminado <= %(corte)s
       AND (c.f IS NULL OR o.fecha_terminado >= c.f)
     GROUP BY ol.producto_num
),
comp AS (
    SELECT cm.producto_num, SUM(cm.cantidad) AS q
      FROM compras cm
      LEFT JOIN conteo c ON c.producto_num = cm.producto_num
     WHERE cm.fecha <= %(corte)s
       AND (c.f IS NULL OR cm.fecha >= c.f)
     GROUP BY cm.producto_num
),
aju AS (
    SELECT ia.producto_num, SUM(ia.cantidad) AS q
      FROM inventario_ajustes ia
      LEFT JOIN conteo c ON c.producto_num = ia.producto_num
     WHERE ia.fecha <= %(corte)s
       AND (c.f IS NULL OR ia.fecha >= c.f)
     GROUP BY ia.producto_num
)
SELECT p.num,
       p.num_visible,
       p.familia,
       p.nombre,
       p.unidad                   AS unidad,
       p.us                       AS us,
       ct.f                       AS conteo_f,
       COALESCE(ct.q, 0)          AS conteo_q,
       COALESCE(cons.q, 0)        AS cons_q,
       COALESCE(comp.q, 0)        AS comp_q,
       COALESCE(aju.q, 0)         AS aju_q
  FROM productos p
  LEFT JOIN conteo ct  ON ct.producto_num  = p.num
  LEFT JOIN cons       ON cons.producto_num = p.num
  LEFT JOIN comp       ON comp.producto_num = p.num
  LEFT JOIN aju        ON aju.producto_num  = p.num
"""


def quimico_final_por_tipo(corte: date | None = None, detalle: bool = False) -> dict | None:
    """{aux, poli, alg, otros, total, colorante} en US$ c/IVA al `corte`.

    Replica FINAL de formulas_app. `total` = aux+poli+alg+otros (lo que muestra
    formulas como "Totales · FINAL"). `colorante` = poli+alg (lo que el balance
    venía usando como Stock Quí.). None si formulas_db no está (fail-soft)."""
    from filters import today_ec

    _corte = (corte or today_ec())
    try:
        rows = formulas_db.fetch_all(_SQL, {"corte": _corte.isoformat()})
    except Exception as e:  # noqa: BLE001 -- fail-soft, sin cachear
        _LOG.warning("quimico_final_por_tipo %s: %s", _corte, e)
        return None
    if not rows:
        return None

    buckets = {"aux": 0.0, "poli": 0.0, "alg": 0.0, "otros": 0.0}
    filas: list = []
    for r in rows:
        num = int(r.get("num") or 0)
        num_visible = int(r.get("num_visible") or 0)
        familia = str(r.get("familia") or "")
        nombre = (str(r.get("nombre") or "")).strip().upper()
        us = float(r.get("us") or 0)
        conteo_f = r.get("conteo_f")
        conteo_q = float(r.get("conteo_q") or 0)
        cons_q = float(r.get("cons_q") or 0)
        comp_q = float(r.get("comp_q") or 0)
        aju_q = float(r.get("aju_q") or 0)

        tipo = _tipo_key(num, familia)
        # A44 = AV SOFT NI: el consumo viene en unidad normal pero el stock va
        # en escamas 10x — se divide /10 (igual que formulas es_a44).
        es_a44 = (tipo == "aux" and num_visible == 44)
        if es_a44:
            cons_q = cons_q / 10.0

        if conteo_f is not None:
            final = round(conteo_q + comp_q + aju_q - cons_q, 3)
        else:
            neto = comp_q + aju_q - cons_q
            if neto == 0:
                continue  # sin conteo y sin movimientos → no aporta
            final = round(neto, 3)

        if us <= 0:
            continue
        iva_mult = 1.0 if nombre == "SAL" else _IVA_FACTOR
        monto = round(final * us, 2)
        monto_iva = round(monto * iva_mult, 2)
        buckets[tipo] = buckets.get(tipo, 0.0) + monto_iva
        if detalle:
            filas.append({
                "num": num, "num_visible": num_visible, "tipo": tipo,
                "familia": familia, "unidad": (str(r.get("unidad") or "")),
                "nombre": (str(r.get("nombre") or "")), "us": us,
                "conteo_f": str(conteo_f) if conteo_f is not None else None,
                "conteo_q": round(conteo_q, 3),
                "cons_q": round(cons_q, 3), "comp_q": round(comp_q, 3),
                "aju_q": round(aju_q, 3), "final": final,
                "conteo_us": round(conteo_q * us * iva_mult, 2),
                "cons_us": round(cons_q * us * iva_mult, 2),
                "comp_us": round(comp_q * us * iva_mult, 2),
                "aju_us": round(aju_q * us * iva_mult, 2),
                "monto_iva": monto_iva, "a44": es_a44,
            })

    total = buckets["aux"] + buckets["poli"] + buckets["alg"] + buckets["otros"]
    colorante = buckets["poli"] + buckets["alg"]
    out = {
        "aux": round(buckets["aux"], 2),
        "poli": round(buckets["poli"], 2),
        "alg": round(buckets["alg"], 2),
        "otros": round(buckets["otros"], 2),
        "total": round(total, 2),
        "colorante": round(colorante, 2),
    }
    if detalle:
        filas.sort(key=lambda x: (x["tipo"], -x["monto_iva"]))
        out["filas"] = filas
    return out


def quimico_total_fisico(corte: date | None = None) -> float | None:
    """Total químico c/IVA (aux+poli+alg) al `corte` — el número de formulas."""
    r = quimico_final_por_tipo(corte)
    return None if r is None else float(r["total"])


_SQL_CONSUMO = """
SELECT COALESCE(SUM(
    (ol.cantidad_kg
     + COALESCE((SELECT SUM((e->>'kg')::numeric)
                   FROM jsonb_array_elements(
                          CASE WHEN jsonb_typeof(ol.ajustes) = 'array'
                               THEN ol.ajustes ELSE '[]'::jsonb END) e), 0))
    * COALESCE(NULLIF(ol.precio_us, 0), p.us, 0)
    * (CASE WHEN p.num = 12 THEN 1.0 ELSE 1.15 END)
    -- A44 (AV SOFT NI, aux num_visible=44): consumo en escamas 10x → /10
    * (CASE WHEN (p.familia ILIKE 'aux' OR p.num < 100) AND p.num_visible = 44
            THEN 0.1 ELSE 1.0 END)
), 0) AS us
  FROM orden_lineas ol
  JOIN ordenes o   ON o.id = ol.orden_id
  JOIN productos p ON p.num = ol.producto_num
 WHERE o.fecha_terminado IS NOT NULL
   AND o.fecha_terminado <> ''
   AND o.fecha_terminado >= %(desde)s
   AND o.fecha_terminado <= %(hasta)s
   AND (UPPER(TRIM(p.familia)) IN ('POLI', 'ALG', 'AUX') OR p.num < 300)
"""


def quimico_consumido_us(desde: date, hasta: date) -> float | None:
    """Químico CONSUMIDO c/IVA en el período [desde, hasta] por fecha_terminado —
    el MISMO número que la columna CONSUMIDO de formulas_app (~157.882). Físico:
    Σ orden_lineas (cantidad_kg + ajustes JSONB) × precio × IVA, con A44 /10.

    Esto es el consumo REAL de bodega — coherente con el stock físico (que ya
    usa el balance/flujo). Reemplaza al ITIN (costeo por orden) en la fila
    'Colorantes/Quím.' para que el costo = lo que realmente salió del stock.
    None si formulas_db no está (fail-soft)."""
    try:
        row = formulas_db.fetch_one(
            _SQL_CONSUMO, {"desde": desde.isoformat(), "hasta": hasta.isoformat()})
    except Exception as e:  # noqa: BLE001 -- fail-soft
        _LOG.warning("quimico_consumido_us %s-%s: %s", desde, hasta, e)
        return None
    if not row:
        return None
    return float(row.get("us") or 0)
