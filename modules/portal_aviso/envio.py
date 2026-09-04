"""Armar y mandar el aviso "su estado de cuenta está en el portal".

El texto es el del plan (sin el monto: quien lo reciba por error se entera de
que esa persona es cliente de Intela, y nada más), y el botón lleva a la
puerta del portal. El mail sale del remitente de siempre y la RESPUESTA le
llega al vendedor del cliente (Reply-To), que es a quien la dueña quiere que
le caigan las respuestas.

Dos maneras de mandarlo, y las dos pasan por `mandar()`:

* **La prueba**: el aviso de UN cliente a la casilla de alguien de la casa,
  para ver cómo sale. Anda siempre.
* **A los clientes**: sólo si el interruptor está prendido
  (`queries.a_clientes_encendido`). Son cientos de mails y SES manda ~14 por
  segundo, así que va en un hilo de fondo y la pantalla lee la bitácora.
"""
from __future__ import annotations

import logging
import threading
from datetime import date

from filters import today_ec

from . import queries

_LOG = logging.getLogger("programa_core.portal_aviso")

PORTAL_URL = "https://portal.intela.com.ec/"

ASUNTO = "Intela · su estado de cuenta"


def nombre_lindo(nombre: str) -> str:
    """'TOTOY BUITRON ANDRES JULIO' → 'Totoy Buitron Andres Julio'."""
    return " ".join(p.capitalize() for p in (nombre or "").split()) or "cliente"


def texto_del_aviso(nombre: str, fecha: date | None = None) -> str:
    fecha = fecha or today_ec()
    return (
        f"Hola {nombre_lindo(nombre)}: le dejamos su estado de cuenta de Intela "
        f"al {fecha.strftime('%d/%m/%Y')}.\n\n"
        f"Para verlo entre acá: {PORTAL_URL}\n"
        f"Le va a pedir su RUC.\n\n"
        f"Intela · Industria Textil Latinoamericana C. Ltda."
    )


def html_del_aviso(nombre: str, fecha: date | None = None) -> str:
    fecha = fecha or today_ec()
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'color:#1e293b;max-width:520px;margin:0 auto;padding:24px 16px">'
        f'<p>Hola {nombre_lindo(nombre)}: le dejamos su estado de cuenta de '
        f'Intela al <strong>{fecha.strftime("%d/%m/%Y")}</strong>.</p>'
        '<p>Para verlo, toque el botón. Le va a pedir su RUC.</p>'
        f'<p style="margin:28px 0"><a href="{PORTAL_URL}" '
        'style="background:#b91c1c;color:#fff;text-decoration:none;'
        'padding:12px 22px;border-radius:6px;font-weight:bold;display:inline-block">'
        'Ver mi estado de cuenta</a></p>'
        '<p style="font-size:12px;color:#64748b">Intela · Industria Textil '
        'Latinoamericana C. Ltda.</p>'
        '</div>'
    )


def mandar(filas: list[dict], quien: str, tipo: str = "cliente",
           a: str = "") -> dict:
    """Manda el aviso a cada fila (`codigo_cli`, `nombre`, `vend`, `correo`).

    `a`: si viene, TODOS van a esa casilla en vez de a la del cliente (la
    prueba). Cada envío queda anotado en `scintela.portal_aviso`, salga o no.
    Devuelve ``{"enviados": int, "fallidos": int, "sin_correo": int}``.
    """
    from modules._lib import mailer

    res = {"enviados": 0, "fallidos": 0, "sin_correo": 0}
    hoy = today_ec()
    for f in filas:
        correo = (a or f.get("correo") or "").strip()
        if not correo:
            res["sin_correo"] += 1
            continue
        nombre = f.get("nombre") or f.get("codigo_cli") or ""
        env = mailer.enviar(
            ASUNTO, texto_del_aviso(nombre, hoy), [correo],
            html=html_del_aviso(nombre, hoy),
            responder_a=queries.correo_del_vendedor(f.get("vend") or ""))
        ok = bool(env.get("ok"))
        res["enviados" if ok else "fallidos"] += 1
        try:
            queries.anotar(f.get("codigo_cli") or "", correo, tipo, ok,
                           env.get("motivo") or "", env.get("id") or "", quien)
        except Exception:  # noqa: BLE001 -- anotar no puede frenar el envío
            _LOG.exception("portal_aviso: no pude anotar el aviso de %s",
                           f.get("codigo_cli"))
    return res


def mandar_en_fondo(filas: list[dict], quien: str) -> threading.Thread:
    """Los avisos a clientes, en un hilo: cientos de mails no entran en un
    request. La pantalla muestra el avance leyendo la bitácora."""
    from flask import current_app

    app = current_app._get_current_object()

    def _correr():
        with app.app_context():
            try:
                r = mandar(filas, quien, tipo="cliente")
                _LOG.info("portal_aviso: %s", r)
            except Exception:  # noqa: BLE001
                _LOG.exception("portal_aviso: el envío de fondo falló")

    t = threading.Thread(target=_correr, name="portal-aviso", daemon=True)
    t.start()
    return t
