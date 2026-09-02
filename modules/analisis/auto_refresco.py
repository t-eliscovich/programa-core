"""Refresco SOLO de la foto de saldos — se cuelga del hilo de fondo.

Dueña 2026-08-20, mirando la competencia cinco días antes de la largada: la
pantalla decía *"Datos de Asinfo al 18/08 19:42"*. Todo —los kilos vendidos, el
ranking, el premio del mes y hasta el congelamiento de la meta del día de la
largada (`queries._fijar_base`)— dependía de que alguien entrara a Saldos y
apretara «Actualizar desde Asinfo». Si nadie lo aprieta, los siete vendedores
miran un tablero en cero y no hay ningún síntoma de que esté viejo salvo esa
fecha en letra chica.

Mismo criterio que la auto-carga de facturas y la de tejeduría: se cuelga del
ciclo que ya existe (`modules/_lib/autocarga_facturas._loop`), NO se agrega un
hilo más, y trae su propio freno.

Dos frenos, a propósito distintos:

  · el de VERDAD es `parado_refresh.actualizado`, que vive en la base: sobrevive
    a un restart del servidor y cuenta también los refrescos que dispara alguien
    a mano con el botón. Con un freno de proceso, cada deploy reiniciaba la
    cuenta y volvía a pegarle a Asinfo.
  · el de ERROR es de proceso: si Asinfo no contesta, `actualizado` no se toca
    (la escritura es una sola transacción al final), así que sin este segundo
    freno el ciclo de 120 s reintentaría cada dos minutos para siempre.

Fail-soft total: cualquier excepción se loguea y devuelve. Apagable con
ANALISIS_AUTO=0. No corre bajo pytest. Cada cuánto, con
ANALISIS_REFRESCO_HORAS (default 0.25 = 15 minutos).

⭐ Dueña 2026-09-02: *"hacé que se recargue lo de competencia cada 15 mins.
tiene mucho delay, quizás un vendedor lo imprime y ya se vendió"*. Antes eran
3 horas: un vendedor imprimía su hoja y salía a ofrecer tela que otro ya había
vendido a la mañana. Traer la foto tarda ~10 s, así que cada 15 min es barato.
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time

import db

_LOG = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ultimo_intento: float | None = None

#: Cuánto esperar después de un intento FALLIDO antes de volver a probar.
_ESPERA_TRAS_ERROR_SEGS = 15 * 60


def _horas() -> float:
    """Cada cuántas horas se trae la foto de nuevo."""
    try:
        v = float(os.environ.get("ANALISIS_REFRESCO_HORAS", "0.25"))
    except (TypeError, ValueError):
        return 0.25
    # Traer la foto tarda ~10 s y borra y reescribe `parado_foto` entera: por
    # abajo de 15 minutos no tiene sentido y sólo castiga a Asinfo. El ciclo del
    # hilo de fondo es de 120 s, así que 0.25 h se cumple con margen.
    return v if v >= 0.25 else 0.25


def _toca() -> bool:
    """¿Pasaron las horas desde el último refresco (lo haya hecho quien lo haya
    hecho)? La cuenta la hace Postgres para no pelearse con la zona horaria del
    contenedor, que está en UTC y Ecuador no."""
    r = db.fetch_one(
        """SELECT actualizado IS NULL
                  OR actualizado < NOW() - (%s || ' hours')::interval AS toca
             FROM scintela.parado_refresh WHERE id = 1""", (str(_horas()),))
    return bool((r or {}).get("toca", True))


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo. Nunca levanta."""
    global _ultimo_intento
    res = {"corrio": False, "items": 0, "llamados": 0}
    if os.environ.get("ANALISIS_AUTO", "1").strip() == "0":
        return res
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return res
    if not _LOCK.acquire(blocking=False):
        return res                      # ya hay uno corriendo: no encimar
    try:
        ahora = _time.monotonic()
        if _ultimo_intento and (ahora - _ultimo_intento) < _ESPERA_TRAS_ERROR_SEGS:
            return res
        if not _toca():
            return res
        _ultimo_intento = ahora
        from . import queries

        r = queries.actualizar()
        res.update(corrio=True, items=r.get("items", 0),
                   llamados=r.get("llamados", 0))
        _LOG.info("saldos (fondo): %s telas en la lista, %s candidatos",
                  res["items"], res["llamados"])
    except Exception as e:              # noqa: BLE001 -- nunca frena el ciclo
        # Fail-soft: la pantalla sigue mostrando la foto vieja CON su fecha,
        # que es información. Vaciarla no lo es.
        _LOG.warning("saldos (fondo): no se pudo actualizar: %s", e)
    finally:
        _LOCK.release()
    return res
