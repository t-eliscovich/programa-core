"""Calentador de cachés de Asinfo — nadie vuelve a ver la carga fría.

Dueña 2026-07-18: medido en vivo, /informes/balance FRÍO = 21,6s y
/informes/flujo-produccion ≈ 15-18s, contra ~1,6s en caliente. Las cachés
TTL de modules/asinfo/service.py expiran (5 min) y CADA deploy reinicia el
proceso vaciándolas → el primer usuario que entra paga la carga entera.

Este hilo daemon refresca las funciones caras de Asinfo al ARRANCAR la app
(post-deploy) y después cada _INTERVALO_SECS, un rato antes de que venzan
los TTL de 300s. Así el request de un usuario siempre encuentra la caché
caliente.

Alcance:
  · Asinfo (Metabase vía requests — thread-safe).
  · Químicos del flujo (2026-07-18): las 2 consultas lentas a formulas
    (modules/informes/quimicos_flujo, medidas en 7,4s) — el pool de
    formulas ya es ThreadedConnectionPool, así que se pueden refrescar
    desde este hilo.
  · Fail-soft total: cualquier excepción se loguea y se sigue; el hilo no
    puede tirar la app.
  · Apagable con WARMUP_ASINFO=0. No corre bajo pytest (PYTEST_CURRENT_TEST).
"""
from __future__ import annotations

import logging
import os
import threading
import time

_LOG = logging.getLogger(__name__)

# TTLs de asinfo/service = 300s → refrescar a los 240s deja margen para que
# el ciclo termine antes del vencimiento (un ciclo completo tarda ~15-20s).
_INTERVALO_SECS = 240
_started = False


def _warm_once() -> None:
    from datetime import date

    from filters import today_ec
    from modules.asinfo import service as asvc

    hoy = today_ec()
    yy, mm = hoy.year, hoy.month
    corte = date(yy, mm, 1)
    pasos = [
        ("inventario_por_etapa", lambda: asvc.inventario_por_etapa()),
        ("inventario_asof", lambda: asvc.inventario_por_etapa_a_fecha(corte)),
        ("mov_bodega_51", lambda: asvc.movimiento_bodega_mes(51, corte)),
        ("mov_bodega_52", lambda: asvc.movimiento_bodega_mes(52, corte)),
        ("mov_bodega_53", lambda: asvc.movimiento_bodega_mes(53, corte)),
        ("hilado_recibido", lambda: asvc.hilado_recibido_mes(yy, mm)),
        ("fabricacion_52", lambda: asvc.fabricacion_flujo_mes(52, yy, mm)),
        ("fabricacion_53", lambda: asvc.fabricacion_flujo_mes(53, yy, mm)),
        ("despacho_fisico", lambda: asvc.despacho_fisico_mes(yy, mm)),
        ("importaciones", lambda: asvc.importaciones_asinfo()),
        ("importaciones_kg", lambda: asvc.importaciones_kg()),
        ("produccion_tejeduria", lambda: asvc.produccion_tejeduria_mes(yy, mm)),
    ]
    # Químicos del flujo (formulas) — los 7,4s medidos de mov_asinfo_quimicos.
    try:
        import calendar as _cal
        from datetime import timedelta

        from modules.informes import quimicos_flujo as _qf
        _last = date(yy, mm, _cal.monthrange(yy, mm)[1])
        _corte_fin = min(_last, hoy)
        _corte_ini = date(yy, mm, 1) - timedelta(days=1)
        pasos += [
            ("quimicos_desglose", lambda: _qf.consumo_quimico_desglose(yy, mm)),
            ("quimicos_fisico_fin", lambda: _qf.fisico_colorante_al_dia(_corte_fin)),
            ("quimicos_fisico_ini", lambda: _qf.fisico_colorante_al_dia(_corte_ini)),
        ]
    except Exception as e:  # noqa: BLE001 -- fail-soft
        _LOG.warning("warmup quimicos setup: %s", e)
    for nombre, fn in pasos:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- fail-soft por paso
            _LOG.warning("warmup asinfo %s: %s", nombre, e)


def _loop() -> None:
    time.sleep(5)  # dejar terminar el arranque de la app
    while True:
        t0 = time.time()
        try:
            _warm_once()
            _LOG.info("warmup asinfo listo en %.1fs", time.time() - t0)
        except Exception as e:  # noqa: BLE001 -- el hilo no muere nunca
            _LOG.warning("warmup asinfo ciclo: %s", e)
        time.sleep(_INTERVALO_SECS)


def start_warmup_thread() -> bool:
    """Arranca el hilo (una sola vez). Devuelve True si arrancó."""
    global _started
    if _started:
        return False
    if os.environ.get("WARMUP_ASINFO", "1").strip() == "0":
        _LOG.info("warmup asinfo APAGADO por WARMUP_ASINFO=0")
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    _started = True
    threading.Thread(target=_loop, name="warmup-asinfo", daemon=True).start()
    _LOG.info("warmup asinfo iniciado (cada %ss)", _INTERVALO_SECS)
    return True
