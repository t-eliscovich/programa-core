"""Las 2 consultas LENTAS de químicos del flujo — con caché TTL y warmup.

Medido en vivo 2026-07-18 (PERF del flujo): `mov_asinfo_quimicos` = 7,4s de
los 8,3s del recompute de /informes/flujo-produccion, y casi todo es:
  (a) el DESGLOSE costeado/proceso/lavado sobre orden_lineas de formulas
      (TO_DATE sobre columna de texto → scan completo del mes), y
  (b) el FÍSICO de colorante al día (tintura_service.stock_colorante_fisico).

Acá viven las dos con caché TTL de 240s + reset, compartidas entre la vista
y el calentador (modules/_lib/warmup.py) — el pool de formulas ya es
ThreadedConnectionPool, así que el warmup puede refrescarlas desde su hilo
y nadie paga los 7,4s en un request.

Bajo pytest la caché se BYPASSEA (PYTEST_CURRENT_TEST) para que los tests
no se contaminen entre sí. Fail-soft: los fallos devuelven None y NO se
cachean (un hipo de formulas no deja 4 min de datos vacíos pegados).
"""
from __future__ import annotations

import calendar
import logging
import os
import time
from datetime import date

_LOG = logging.getLogger(__name__)

_TTL_SECS = 240  # < TTL 300 de Asinfo; el warmup refresca cada 240s
_CACHE: dict = {}


def reset_quimicos_flujo_cache() -> None:
    """Vaciar el caché (tests / tras deploy)."""
    _CACHE.clear()


def _cache_get(key):
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _TTL_SECS:
        return hit[1]
    return None


def _cache_put(key, valor) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    _CACHE[key] = (time.time(), valor)


def consumo_quimico_desglose(anio: int, mes: int) -> dict | None:
    """Egreso de químico del mes de formulas, desglosado.

    {"costeado": $, "proceso": $, "lavado": $} — POLI+ALG+AUX por fecha de
    tinturado (ordenes.fecha 'DD/MM/YYYY'), c/IVA salvo sal (num=12):
      costeado = teñido con kg de tela cargado (= Total COSTOS DE TINTORERÍA)
      proceso  = teñido sin cerrar la tela (En máquinas de la banda QUÍM.$)
      lavado   = órdenes de lavado
    None si formulas no está (fail-soft, sin cachear).
    """
    key = ("desglose", int(anio), int(mes))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        from modules._lib import formulas_db as _fdb
        d1 = date(int(anio), int(mes), 1).isoformat()
        d2 = date(int(anio), int(mes),
                  calendar.monthrange(int(anio), int(mes))[1]).isoformat()
        rows = _fdb.fetch_all(
            """
            SELECT CASE
                     WHEN (COALESCE(f.categoria, '') ILIKE '%%lavado%%'
                           OR COALESCE(f.color, '') ILIKE 'LAV%%'
                           OR UPPER(TRIM(COALESCE(o.codigo, ''))) = 'LAV')
                          THEN 'lavado'
                     WHEN COALESCE(o.tela_cruda_kg, 0) > 0 THEN 'costeado'
                     ELSE 'proceso'
                   END AS clase,
                   COALESCE(SUM(ol.cantidad_kg
                     * COALESCE(NULLIF(ol.precio_us, 0), p.us, 0)
                     * (CASE WHEN ol.producto_num IN (12) THEN 1.0 ELSE 1.15 END)), 0) AS us
              FROM orden_lineas ol
              JOIN ordenes   o ON o.id  = ol.orden_id
              JOIN productos p ON p.num = ol.producto_num
              LEFT JOIN formulas f ON f.cod = o.codigo
             WHERE UPPER(TRIM(p.familia)) IN ('POLI', 'ALG', 'AUX')
               AND TO_DATE(o.fecha, 'DD/MM/YYYY') >= %(d1)s
               AND TO_DATE(o.fecha, 'DD/MM/YYYY') <= %(d2)s
             GROUP BY 1
            """,
            {"d1": d1, "d2": d2},
        )
        if rows is None:
            return None
        out = {"costeado": 0.0, "proceso": 0.0, "lavado": 0.0}
        for r in rows or []:
            cl = r.get("clase")
            if cl in out:
                out[cl] = float(r.get("us") or 0)
        _cache_put(key, out)
        return out
    except Exception as e:  # noqa: BLE001 -- fail-soft, sin cachear
        _LOG.warning("consumo_quimico_desglose %s/%s: %s", mes, anio, e)
        return None


def color_familias_valuadas() -> dict | None:
    """{FAMILIA: US$} del stock FOTO de químicos de formulas, valuado c/IVA
    por producto (sal num=12 exenta) — el loop que antes corría inline en
    _build_mov_asinfo (stock_quimicos() + factor_iva_producto por ítem).
    None si formulas no está (fail-soft, sin cachear)."""
    key = ("familias",)
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        from modules.tintura import service as _tsvc
        fams: dict = {}
        for x in (_tsvc.stock_quimicos() or []):
            fam = (getattr(x, "familia", "") or "?").strip().upper()
            val = (float(getattr(x, "stock_kg", 0) or 0)
                   * float(getattr(x, "precio_us", 0) or 0)
                   * _tsvc.factor_iva_producto(getattr(x, "num", None)))
            fams[fam] = fams.get(fam, 0.0) + val
        if not fams:
            return None
        _cache_put(key, fams)
        return fams
    except Exception as e:  # noqa: BLE001 -- fail-soft, sin cachear
        _LOG.warning("color_familias_valuadas: %s", e)
        return None


def color_movimiento_mes(anio: int, mes: int) -> dict | None:
    """CONSUMO y COMPRAS de colorante (POLI+ALG) del mes desde formulas —
    las 2 queries de la banda COLORANTES de _build_mov_asinfo, con caché.
    {"consumo_us", "n_ordenes", "compras_us"}. None si formulas no está."""
    key = ("color_mov", int(anio), int(mes))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        from modules._lib import formulas_db as _fdb
        d1 = date(int(anio), int(mes), 1).isoformat()
        d2 = date(int(anio), int(mes),
                  calendar.monthrange(int(anio), int(mes))[1]).isoformat()
        cons = _fdb.fetch_one(
            """
            SELECT COALESCE(SUM(ol.cantidad_kg
                     * COALESCE(NULLIF(ol.precio_us, 0), p.us, 0)
                     * (CASE WHEN ol.producto_num IN (12) THEN 1.0 ELSE 1.15 END)), 0) AS us,
                   COUNT(DISTINCT o.id) AS n_ordenes
              FROM orden_lineas ol
              JOIN ordenes o   ON o.id  = ol.orden_id
              JOIN productos p ON p.num = ol.producto_num
             WHERE UPPER(TRIM(p.familia)) IN ('POLI', 'ALG')
               AND o.fecha_terminado IS NOT NULL
               AND o.fecha_terminado >= %(d1)s
               AND o.fecha_terminado <= %(d2)s
            """,
            {"d1": d1, "d2": d2},
        )
        if cons is None:
            return None
        comp = _fdb.fetch_one(
            """
            SELECT COALESCE(SUM(c.cantidad
                     * COALESCE(NULLIF(c.precio_us, 0), p.us, 0)
                     * (CASE WHEN c.producto_num IN (12) THEN 1.0 ELSE 1.15 END)), 0) AS us
              FROM compras c
              JOIN productos p ON p.num = c.producto_num
             WHERE UPPER(TRIM(p.familia)) IN ('POLI', 'ALG')
               AND c.fecha >= %(d1)s AND c.fecha <= %(d2)s
            """,
            {"d1": d1, "d2": d2},
        ) or {}
        out = {
            "consumo_us": float((cons or {}).get("us") or 0),
            "n_ordenes": int((cons or {}).get("n_ordenes") or 0),
            "compras_us": float(comp.get("us") or 0),
        }
        _cache_put(key, out)
        return out
    except Exception as e:  # noqa: BLE001 -- fail-soft, sin cachear
        _LOG.warning("color_movimiento_mes %s/%s: %s", mes, anio, e)
        return None


# ── Banda QUÍM del flujo — modelo TODO-formulas (dueña 2026-07-21) ──────────
# La tintorería/químicos de julio sale ENTERA de formulas_app y la banda cierra
# a cero con filas nombradas:
#   Stock inic. (físico fin mes anterior) + Entradas bodega ± Ajustes inventario
#   − Consumo (órdenes TERMINADAS del mes) = Stock act. (físico hoy)
# Criterio contable acordado: el químico se descuenta AL TERMINAR LA ORDEN
# (fecha_terminado), igual que los kg y el costo. Todo valuado A PRECIO DE
# CATÁLOGO (productos.us) × factor IVA (sal num=12 exenta) para que la
# identidad cierre en $. Familias POLI+ALG+AUX en TODOS los términos (mismo
# universo de productos, si no la banda no puede cerrar).
# Fail-soft: None (sin cachear) si formulas no está.

_FAMILIAS_QUIMICO = ("POLI", "ALG", "AUX")
_SQL_FAM_QUIMICO = "UPPER(TRIM(p.familia)) IN ('POLI', 'ALG', 'AUX')"
# Factor IVA a catálogo — mismo CASE que el resto del módulo (sal num=12 al 0%).
_SQL_IVA = "(CASE WHEN p.num IN (12) THEN 1.0 ELSE 1.15 END)"


def fisico_total_al_dia(corte: date) -> float | None:
    """Físico de TODO el químico (POLI+ALG+AUX) al `corte`, valuado a precio
    de catálogo (productos.us) c/IVA por producto (sal exenta).

    Usa tintura_service.stock_quimicos_al_dia (lectura + ajustes + compras −
    consumo de órdenes terminadas, por producto) y valúa cada
    StockProductoAlDia: stock_al_dia_kg × precio_us × factor_iva_producto.
    None si formulas no está (fail-soft, sin cachear)."""
    key = ("fisico_total", str(corte))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        from modules.tintura import service as _tsvc
        items = _tsvc.stock_quimicos_al_dia(corte)
        if not items:  # [] = bridge apagado o falló → no distinguible, None
            return None
        tot = 0.0
        for x in items:
            fam = (getattr(x, "familia", "") or "").strip().upper()
            if fam not in _FAMILIAS_QUIMICO:
                continue
            tot += (float(getattr(x, "stock_al_dia_kg", 0) or 0)
                    * float(getattr(x, "precio_us", 0) or 0)
                    * _tsvc.factor_iva_producto(getattr(x, "num", None)))
        _cache_put(key, tot)
        return tot
    except Exception as e:  # noqa: BLE001 -- fail-soft, sin cachear
        _LOG.warning("fisico_total_al_dia %s: %s", corte, e)
        return None


def entradas_bodega_mes(anio: int, mes: int) -> dict | None:
    """Entradas REALES a bodega del mes = formulas.compras (POLI+ALG+AUX).

    {"us": Σ cantidad × COALESCE(NULLIF(precio_us,0), p.us, 0) × factorIVA,
     "n": count}. compras.fecha es ISO text → rango lexicográfico.
    None si formulas no está (fail-soft, sin cachear)."""
    key = ("entradas_bodega", int(anio), int(mes))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        from modules._lib import formulas_db as _fdb
        if not _fdb.disponible():
            return None
        d1 = date(int(anio), int(mes), 1).isoformat()
        d2 = date(int(anio), int(mes),
                  calendar.monthrange(int(anio), int(mes))[1]).isoformat()
        row = _fdb.fetch_one(
            f"""
            SELECT COALESCE(SUM(c.cantidad
                     * COALESCE(NULLIF(c.precio_us, 0), p.us, 0)
                     * (CASE WHEN c.producto_num IN (12) THEN 1.0 ELSE 1.15 END)), 0) AS us,
                   COUNT(*) AS n
              FROM compras c
              JOIN productos p ON p.num = c.producto_num
             WHERE {_SQL_FAM_QUIMICO}
               AND c.fecha >= %(d1)s AND c.fecha <= %(d2)s
            """,
            {"d1": d1, "d2": d2},
        )
        if row is None:
            return None
        out = {"us": float(row.get("us") or 0), "n": int(row.get("n") or 0)}
        _cache_put(key, out)
        return out
    except Exception as e:  # noqa: BLE001 -- fail-soft, sin cachear
        _LOG.warning("entradas_bodega_mes %s/%s: %s", mes, anio, e)
        return None


def ajustes_inventario_mes(anio: int, mes: int) -> dict | None:
    """Ajustes de inventario del mes (formulas.inventario_ajustes), valuados
    a precio de catálogo (productos.us × factorIVA). Pueden ser ±.

    {"us": Σ a.cantidad × p.us × factorIVA, "n": count,
     "detalle": [{"motivo", "n", "us"}]} — el detalle por motivo va al
    tooltip de la fila. inventario_ajustes.fecha es ISO text.
    None si formulas no está (fail-soft, sin cachear)."""
    key = ("ajustes_inv", int(anio), int(mes))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        from modules._lib import formulas_db as _fdb
        if not _fdb.disponible():
            return None
        d1 = date(int(anio), int(mes), 1).isoformat()
        d2 = date(int(anio), int(mes),
                  calendar.monthrange(int(anio), int(mes))[1]).isoformat()
        rows = _fdb.fetch_all(
            f"""
            SELECT COALESCE(NULLIF(TRIM(a.motivo), ''), '(sin motivo)') AS motivo,
                   COUNT(*) AS n,
                   COALESCE(SUM(a.cantidad * COALESCE(p.us, 0) * {_SQL_IVA}), 0) AS us
              FROM inventario_ajustes a
              JOIN productos p ON p.num = a.producto_num
             WHERE {_SQL_FAM_QUIMICO}
               AND a.fecha >= %(d1)s AND a.fecha <= %(d2)s
             GROUP BY 1
             ORDER BY 3 DESC
            """,
            {"d1": d1, "d2": d2},
        )
        if rows is None:
            return None
        detalle = [
            {"motivo": str(r.get("motivo") or ""),
             "n": int(r.get("n") or 0),
             "us": float(r.get("us") or 0)}
            for r in rows
        ]
        out = {
            "us": sum(d["us"] for d in detalle),
            "n": sum(d["n"] for d in detalle),
            "detalle": detalle,
        }
        _cache_put(key, out)
        return out
    except Exception as e:  # noqa: BLE001 -- fail-soft, sin cachear
        _LOG.warning("ajustes_inventario_mes %s/%s: %s", mes, anio, e)
        return None


def consumo_terminadas_mes(anio: int, mes: int) -> dict | None:
    """Consumo de químico (POLI+ALG+AUX) de las órdenes TERMINADAS en el mes
    (criterio contable dueña 2026-07-21: el químico se descuenta al terminar
    la orden — fecha_terminado — igual que los kg y el costo).

    {"us": Σ ol.cantidad_kg × COALESCE(NULLIF(ol.precio_us,0), p.us, 0)
           × factorIVA}. ordenes.fecha_terminado es ISO text ('YYYY-MM-DD')
    → rango lexicográfico, mismo parseo que color_movimiento_mes.
    None si formulas no está (fail-soft, sin cachear)."""
    key = ("consumo_term", int(anio), int(mes))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        from modules._lib import formulas_db as _fdb
        if not _fdb.disponible():
            return None
        d1 = date(int(anio), int(mes), 1).isoformat()
        d2 = date(int(anio), int(mes),
                  calendar.monthrange(int(anio), int(mes))[1]).isoformat()
        row = _fdb.fetch_one(
            f"""
            SELECT COALESCE(SUM(ol.cantidad_kg
                     * COALESCE(NULLIF(ol.precio_us, 0), p.us, 0)
                     * (CASE WHEN ol.producto_num IN (12) THEN 1.0 ELSE 1.15 END)), 0) AS us
              FROM orden_lineas ol
              JOIN ordenes   o ON o.id  = ol.orden_id
              JOIN productos p ON p.num = ol.producto_num
             WHERE {_SQL_FAM_QUIMICO}
               AND o.fecha_terminado IS NOT NULL
               AND o.fecha_terminado >= %(d1)s
               AND o.fecha_terminado <= %(d2)s
            """,
            {"d1": d1, "d2": d2},
        )
        if row is None:
            return None
        out = {"us": float(row.get("us") or 0)}
        _cache_put(key, out)
        return out
    except Exception as e:  # noqa: BLE001 -- fail-soft, sin cachear
        _LOG.warning("consumo_terminadas_mes %s/%s: %s", mes, anio, e)
        return None


def fisico_colorante_al_dia(corte: date) -> float | None:
    """Físico de colorante (POLI+ALG) al `corte` — LA variable compartida con
    el balance (tintura_service.stock_colorante_fisico), acá con caché.
    None si formulas no está (fail-soft, sin cachear)."""
    key = ("fisico", str(corte))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        from modules.tintura import service as _tsvc
        v = _tsvc.stock_colorante_fisico(corte)
        if v is None:
            return None
        out = float(v or 0)
        _cache_put(key, out)
        return out
    except Exception as e:  # noqa: BLE001 -- fail-soft, sin cachear
        _LOG.warning("fisico_colorante_al_dia %s: %s", corte, e)
        return None
