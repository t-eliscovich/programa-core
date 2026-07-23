"""Auto-carga de facturas+retenciones del día — hilo de fondo (sin abrir nada).

Dueña 2026-07-23: "quiero no tener que ir a ninguna página". La auto-carga del
día (`facturas.views._auto_cargar_facturas_hoy`) ya corría al ABRIR /facturas y
al ENTRAR al programa (/operaciones), pero ambas dependen de que alguien abra una
pantalla. Este hilo daemon la corre SOLO, en el servidor, cada _INTERVALO_SECS,
así las facturas y retenciones de HOY entran solas aunque nadie tenga el programa
abierto.

Reusa la MISMA función (idempotente, fail-soft, dedup por N° SRI, lock + throttle
interno de 60s), así que:
  · nunca duplica (mismo guard que la carga por pantalla);
  · no encima corridas con las que dispara un usuario al entrar;
  · corre SIN contexto de request — verificado: el camino de creación
    (_resolver_cliente_asinfo → queries.crear → aplicar_retenciones_asinfo) no
    usa flask g/request/session (igual que el warmup lee Asinfo desde un hilo).

Fail-soft total: cualquier excepción se loguea y el hilo sigue.
Apagable con AUTOCARGA_FACTURAS=0. No corre bajo pytest. Intervalo configurable
con AUTOCARGA_FACTURAS_SECS (default 120s).
"""
from __future__ import annotations

import logging
import os
import threading
import time

_LOG = logging.getLogger(__name__)

_started = False


def _intervalo_secs() -> int:
    try:
        v = int(os.environ.get("AUTOCARGA_FACTURAS_SECS", "120"))
        return v if v >= 60 else 60  # el throttle interno es 60s: no bajar de ahí
    except (TypeError, ValueError):
        return 120


def _loop() -> None:
    time.sleep(10)  # dejar terminar el arranque de la app (y que el warmup arranque)
    intervalo = _intervalo_secs()
    while True:
        try:
            # Import perezoso: facturas.views ya está cargado (blueprint
            # registrado), evita cualquier ciclo en tiempo de import.
            from modules.facturas.views import _auto_cargar_facturas_hoy

            res = _auto_cargar_facturas_hoy()
            if res.get("cargadas") or res.get("ret"):
                _LOG.info(
                    "auto-carga facturas (fondo): %s facturas, %s retenciones",
                    res.get("cargadas", 0), res.get("ret", 0),
                )
        except Exception as e:  # noqa: BLE001 -- el hilo no muere nunca
            _LOG.warning("auto-carga facturas (fondo) ciclo: %s", e)
        time.sleep(intervalo)


def start_auto_carga_thread() -> bool:
    """Arranca el hilo (una sola vez). Devuelve True si arrancó."""
    global _started
    if _started:
        return False
    if os.environ.get("AUTOCARGA_FACTURAS", "1").strip() == "0":
        _LOG.info("auto-carga facturas (fondo) APAGADA por AUTOCARGA_FACTURAS=0")
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    _started = True
    threading.Thread(
        target=_loop, name="auto-carga-facturas", daemon=True
    ).start()
    _LOG.info("auto-carga facturas (fondo) iniciada (cada %ss)", _intervalo_secs())
    return True
