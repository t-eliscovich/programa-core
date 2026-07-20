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
