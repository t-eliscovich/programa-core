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

# CADA 60s — no cada "casi el TTL". Las funciones refrescan SOLO si su caché
# venció (si está viva, la llamada es un cache-HIT gratis, sin I/O). Con un
# intervalo cercano al TTL quedaban VENTANAS FRÍAS de minutos entre el
# vencimiento (300s) y el próximo ciclo (medido 18/07: flujo 17s de nuevo).
# Con 60s, cada caché se refresca como mucho 60s después de vencer, y un
# usuario que caiga justo en la ventana paga 1-2 funciones (1-4s), no la
# carga fría entera.
_INTERVALO_SECS = 60
_started = False


def _warm_once() -> None:
    import calendar as _cal_mod
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
        # ventas_facturado_kg → facturas_periodo(mes) — sin este paso quedaban
        # ~3s vivos en el flujo (medido 18/07: mov_asinfo_quimicos 3,1s que en
        # realidad eran la card de facturas fría).
        ("ventas_facturado", lambda: asvc.ventas_facturado_kg(yy, mm)),
        # TMT 2026-08-24 (dueña: *"qué más podemos hacer más rápido?"*).
        # Medidas las 33 pantallas del menú en producción, las tres peores NO
        # eran lentas: eran FRÍAS. Primera visita contra visita siguiente:
        #   · /produccion-terminado-asinfo   9,8 s  →  14 ms
        #   · /inventario-rotativo           5,1 s  →  33 ms
        #   · /stock/fabricacion-tc          1,7 s (las dos veces: le faltaba
        #     cache a `despachos_sin_of`, ver hilo_sin_of._CACHE)
        # O sea que el que entraba justo después de que vencía el cache pagaba
        # la carga entera. Se calientan por la función de la PANTALLA y no por
        # cada consulta suelta: si mañana la pantalla pide una más, se calienta
        # sola.
        # El rango ANCHO que usa /facturas cuando se filtra por cliente:
        # 2025-01-01 → fin del mes en curso (ver facturas/views.py, el
        # redondeo a meses enteros). Sin esto, el primero que filtra un
        # cliente después de que vence el cache espera 5 segundos.
        ("facturas_rango_ancho",
         lambda: asvc.facturas_periodo(
             date(2025, 1, 1),
             date(yy, mm, _cal_mod.monthrange(yy, mm)[1]))),
        ("stock_lote_totales", lambda: asvc.stock_asinfo_lote_totales()),
        ("fabricacion_proceso_52", lambda: asvc.fabricacion_proceso(52)),
        ("fabricacion_proceso_53", lambda: asvc.fabricacion_proceso(53)),
    ]
    try:
        from modules.asinfo import hilo_sin_of as _hso
        from modules.inventario_rotativo import service as _rot
        from modules.pedidos import service as _ped
        from modules.terminado_asinfo import service as _term
        pasos += [
            ("hilo_esperando_orden", lambda: _hso.despachos_sin_of(dias=0)),
            ("inventario_rotativo", lambda: _rot.rotativo()),
            ("produccion_terminado", lambda: _term.resumen(yy, mm)),
            # /pedidos tardaba 3,6 s SIEMPRE: sus tres consultas iban a Asinfo
            # sin cache. Ahora cachean 5 min y acá se refrescan antes de que
            # venzan (TMT 2026-08-24).
            ("pedidos_pendientes", lambda: _ped.pendientes()),
            ("pedidos_por_color", lambda: _ped.pedidos_por_color()),
            ("pedidos_acabados", lambda: _ped.acabados_por_producto()),
        ]
    except Exception as e:  # noqa: BLE001 -- fail-soft
        _LOG.warning("warmup pantallas de stock: %s", e)
    # Químicos del flujo (formulas) — los 7,4s medidos de mov_asinfo_quimicos.
    try:
        import calendar as _cal
        from datetime import timedelta

        from modules.informes import quimicos_flujo as _qf
        _last = date(yy, mm, _cal.monthrange(yy, mm)[1])
        _corte_fin = min(_last, hoy)
        _corte_ini = date(yy, mm, 1) - timedelta(days=1)
        # Banda formulas (dueña 2026-07-21): la banda QUÍM usa el físico TOTAL
        # (POLI+ALG+AUX) hoy y al cierre del mes anterior + entradas/ajustes/
        # consumo del mes. Los pasos viejos (desglose + físico colorante
        # ini/fin) quedaron solo para el FALLBACK de la vista — no se calientan.
        # (El balance usa tintura_service.stock_colorante_fisico directo, no
        # fisico_colorante_al_dia — verificado: nadie más lo consume caliente.)
        pasos += [
            ("quimicos_fisico_total_fin", lambda: _qf.fisico_total_al_dia(_corte_fin)),
            ("quimicos_fisico_total_ini", lambda: _qf.fisico_total_al_dia(_corte_ini)),
            ("quimicos_entradas", lambda: _qf.entradas_bodega_mes(yy, mm)),
            ("quimicos_ajustes", lambda: _qf.ajustes_inventario_mes(yy, mm)),
            ("quimicos_consumo_term", lambda: _qf.consumo_terminadas_mes(yy, mm)),
            ("quimicos_familias", lambda: _qf.color_familias_valuadas()),
            ("quimicos_color_mov", lambda: _qf.color_movimiento_mes(yy, mm)),
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
