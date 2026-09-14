"""El recordatorio automático "su estado de cuenta está en el portal" —
cada DOS semanas, los LUNES, salvo feriado de Ecuador.

TMT 2026-09-14 (dueña): *"el mail de estado de cuenta va a ser cada dos
semanas los lunes salvo que sea feriado en ecuador"*. Sale de la MISMA lista
y el MISMO `mandar()` que el lanzamiento (`envio.mandar`,
`contenido="recordatorio"`) pero con el texto sin "portal nuevo": ya no es
un anuncio, es un recordatorio.

Cómo se agenda: NO es aritmética de semana par/impar — es un PUNTERO en
`scintela.nota_config` (`queries.CLAVE_PROXIMA_EC`) con la fecha de la
próxima corrida. Cada corrida (mande o se salte por feriado) empuja el
puntero 14 días — como 14 es múltiplo de 7, el puntero SIEMPRE cae lunes,
para siempre, sin recalcular nada. Se fija la primera vez a mano desde la
pantalla (`/portal-aviso`).

Si el lunes que toca es feriado (Ecuador + Quito, `informes.feriados`), esa
quincena se SALTEA — no manda, no corre al martes — y el puntero igual
avanza 14 días: la dueña dijo "salvo que sea feriado", no "un día después".

Apagado por default (`queries.CLAVE_INTERRUPTOR_EC`, nace en '0') — mismo
patrón de seguridad que el interruptor del lanzamiento: prenderlo es una
decisión aparte, en la pantalla.

Corre desde el mismo ciclo de fondo que el resto de los AUTO
(`modules._lib.autocarga_facturas`), con su propio freno de una vez por hora
(no hace falta más seguido: sólo importa el DÍA) y recién después de
`HORA_MINIMA` de Ecuador, para no mandar de madrugada si el server arranca
temprano ese lunes. `PORTAL_AVISO_EC_AUTO=0` lo apaga por env, además del
interruptor de pantalla.
"""
from __future__ import annotations

import logging
import os
import time as _time
from datetime import UTC, datetime, timedelta

from filters import today_ec

from modules.informes.feriados import feriados_ec_quito

from . import envio, queries

_LOG = logging.getLogger("programa_core.portal_aviso.recordatorio")

#: Cada cuántos días se repite (2 semanas). Múltiplo de 7 → siempre cae lunes.
CADA_DIAS = 14

#: No mandar antes de esta hora de Ecuador, el día que toca.
HORA_MINIMA = 8

#: Freno interno: como mucho una corrida real por hora (sólo importa el día).
_CHECK_MIN_SECS = 3600
_auto_ultimo: float | None = None


def _ahora_ec() -> datetime:
    """Ahora en Ecuador (UTC-5, sin horario de verano) — igual que aviso_ventas."""
    return datetime.now(UTC) - timedelta(hours=5)


def correr_si_toca() -> dict:
    """Entrada del hilo de fondo. Nunca levanta."""
    res: dict = {"corrio": False, "motivo": ""}
    if os.environ.get("PORTAL_AVISO_EC_AUTO", "1") == "0":
        res["motivo"] = "apagado por env"
        return res

    global _auto_ultimo
    ahora = _time.monotonic()
    if _auto_ultimo is not None and (ahora - _auto_ultimo) < _CHECK_MIN_SECS:
        res["motivo"] = "todavía no toca chequear"
        return res
    _auto_ultimo = ahora

    try:
        if not queries.ec_auto_encendido():
            res["motivo"] = "interruptor apagado"
            return res

        proxima = queries.proxima_corrida_ec()
        if not proxima:
            res["motivo"] = "sin próxima corrida programada"
            return res

        hoy = today_ec()
        if hoy < proxima:
            res["motivo"] = f"todavía no toca (próxima {proxima.isoformat()})"
            return res
        if hoy == proxima and _ahora_ec().hour < HORA_MINIMA:
            res["motivo"] = f"todavía no son las {HORA_MINIMA} (EC)"
            return res

        siguiente = proxima + timedelta(days=CADA_DIAS)

        if proxima in feriados_ec_quito(proxima.year):
            queries.fijar_proxima_corrida_ec(siguiente)
            _LOG.info("recordatorio EC: %s es feriado, salteado. Próxima: %s",
                      proxima.isoformat(), siguiente.isoformat())
            res["motivo"] = "feriado, salteado"
            res["saltado"] = proxima.isoformat()
            return res

        filas = [f for f in queries.lista() if f.get("correo")]
        if not filas:
            # Nadie a quién mandarle: igual empuja el puntero, si no se
            # quedaría reintentando cada hora para siempre sin nadie a bordo.
            queries.fijar_proxima_corrida_ec(siguiente)
            res["motivo"] = "nadie con correo"
            return res

        r = envio.mandar(filas, "automatico", tipo="cliente", contenido="recordatorio")
        queries.fijar_proxima_corrida_ec(siguiente)
        res["corrio"] = True
        res["enviados"] = r.get("enviados", 0)
        res["fallidos"] = r.get("fallidos", 0)

        try:
            from modules.avisos.queries import avisar
            avisar(
                fuente="portal_aviso",
                titulo=f"Recordatorio de estado de cuenta · {res['enviados']} clientes",
                detalle=f"{res['fallidos']} fallido(s). Próxima corrida: "
                        f"{siguiente.isoformat()}.",
                cantidad=res["enviados"],
                url="/portal-aviso",
                clave=f"portal-aviso-recordatorio:{proxima.isoformat()}",
            )
        except Exception:  # noqa: BLE001 -- el aviso no puede frenar nada
            _LOG.exception("recordatorio EC: no pude dejar el aviso en la campanita")

        _LOG.info("recordatorio EC: mandado (%s enviados, %s fallidos), próxima %s",
                  res["enviados"], res["fallidos"], siguiente.isoformat())
    except Exception as e:  # noqa: BLE001 -- el hilo no se cae por esto
        _LOG.exception("recordatorio EC: %s", e)
        res["motivo"] = str(e)[:160]
    return res
