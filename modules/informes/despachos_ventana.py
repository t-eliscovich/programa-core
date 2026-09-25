"""Las guías de despacho que salieron en una ventana de la traza.

⭐ Tamara 25/09/2026: *"si ponemos los despachos cuando sale de terminado…"*.
La columna Term. de la traza muestra el NETO de la ventana: a las 10:12 del
21/09 decía "salió de term. −184" y en realidad salieron 820 kg en cuatro
guías mientras entraban 636 de producción. El despacho quedaba escondido
adentro del neto.

Las guías vienen de Asinfo (`dia_despacho._guias`, las mismas de
/facturas/dia) y se piden UNA vez por día: el día de hoy se refresca cada
dos minutos, los días pasados no cambian. Fail-soft: si Metabase no contesta,
la traza queda como estaba.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

_LOG = logging.getLogger("programa_core.despachos_ventana")
_EC = ZoneInfo("America/Guayaquil")

#: Cada cuánto se vuelve a preguntar por las guías de HOY.
TTL_HOY = 120

_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _hoy() -> str:
    from filters import today_ec
    return today_ec().isoformat()


def _guias_del_dia(dia: str) -> list[dict]:
    c = _CACHE.get(dia)
    if c and (dia != _hoy() or time.time() - c[0] < TTL_HOY):
        return c[1]
    from modules.facturas.dia_despacho import _guias
    guias = _guias(dia)                  # si falla, no se cachea el fracaso
    _CACHE[dia] = (time.time(), guias)
    return guias


def _a_ec(t) -> datetime:
    if isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo("UTC"))
    return t.astimezone(_EC)


def de_la_ventana(desde, hasta) -> list[dict]:
    """Las guías con hora en [desde, hasta), al minuto y en hora de Ecuador.

    La guía trae la hora sin segundos; la foto, con segundos. Una guía de las
    10:12 cae en la foto que se sacó DESPUÉS de las 10:12 (medido: 111 de las
    120 ventanas en que bajó terminado tienen su guía así).
    """
    if not desde or not hasta:
        return []
    try:
        d0, d1 = _a_ec(desde), _a_ec(hasta)
        m0, m1 = d0.strftime("%Y-%m-%d %H:%M"), d1.strftime("%Y-%m-%d %H:%M")
        out = []
        for dia in sorted({d0.date().isoformat(), d1.date().isoformat()}):
            for g in _guias_del_dia(dia) or []:
                t = f"{dia} {g.get('hora') or ''}"
                if m0 <= t < m1 and (g.get("kg") or 0) > 0:
                    out.append(dict(g, dia=dia))
        return out
    except Exception as e:  # noqa: BLE001 -- la traza nunca se cae por esto
        _LOG.warning("despachos_ventana: sin guías (%s)", e)
        return []
