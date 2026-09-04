"""El aviso a los clientes: "su estado de cuenta está en el portal".

Fase 4 del plan del portal, POR MAIL (TMT 04/09/2026: *"fase 4 hagámosla por
mail por el momento"*; Meta/WhatsApp sigue sin alta). Una sola pantalla,
`/portal-aviso`:

* la lista de a quién le va: todos los clientes con saldo, con el correo que
  se le mandaría y de dónde sale, si ya entró al portal, y cuándo fue el
  último aviso;
* **Mandarme una prueba**: el aviso de UN cliente a la casilla que se
  escriba, para ver cómo sale. Anda siempre;
* **Mandar a los clientes**: sólo con el interruptor prendido. Nace apagado
  (*"hasta no testear no mandamos nada a los clientes"*), y prenderlo es un
  botón en la misma pantalla, para que sea UNA decisión y no dos programas.

Permiso: `portal.avisar`. Lo tienen los roles con todo (Accionista,
Administrador): es mandarle un mail a cientos de clientes.
"""
from __future__ import annotations

import logging

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from auth import requiere_login, requiere_permiso

from . import envio, queries

_LOG = logging.getLogger("programa_core.portal_aviso")

portal_aviso_bp = Blueprint("portal_aviso", __name__, template_folder="templates")

PERMISO = "portal.avisar"


def _quien() -> str:
    return (g.get("user") or {}).get("username") or "anon"


@portal_aviso_bp.route("/portal-aviso")
@requiere_login
@requiere_permiso(PERMISO)
def pantalla():
    error = None
    try:
        filas = queries.lista()
        historial = queries.historial()
        encendido = queries.a_clientes_encendido()
    except Exception as e:  # noqa: BLE001 -- la tabla puede no existir todavía
        _LOG.exception("portal_aviso: no pude armar la lista (%s)", e)
        filas, historial, encendido, error = [], [], False, str(e)
    con_correo = [f for f in filas if f["correo"]]
    sin_correo = [f for f in filas if not f["correo"]]
    return render_template(
        "portal_aviso/pantalla.html",
        filas=filas, con_correo=con_correo, sin_correo=sin_correo,
        historial=historial, encendido=encendido, error=error,
        ejemplo=envio.texto_del_aviso(con_correo[0]["nombre"] if con_correo else "cliente"),
        mail_prueba=request.args.get("a", ""),
    )


@portal_aviso_bp.route("/portal-aviso/prueba", methods=["POST"])
@requiere_login
@requiere_permiso(PERMISO)
def prueba():
    """El aviso de un cliente, a una casilla nuestra. Nunca al cliente."""
    a = (request.form.get("a") or "").strip()
    cod = (request.form.get("codigo_cli") or "").strip().upper()
    if not a or "@" not in a:
        flash("Escribí a qué correo va la prueba.", "error")
        return redirect(url_for("portal_aviso.pantalla"))
    fila = next((f for f in queries.lista() if f["codigo_cli"] == cod), None)
    if not fila:
        flash(f"No encuentro al cliente {cod or '(vacío)'} entre los que tienen saldo.", "error")
        return redirect(url_for("portal_aviso.pantalla"))
    r = envio.mandar([fila], _quien(), tipo="prueba", a=a)
    if r["enviados"]:
        flash(f"Salió la prueba con el aviso de {cod} a {a}.", "ok")
    else:
        flash(f"No salió la prueba a {a}. Mirá el historial de abajo para ver el motivo.", "error")
    return redirect(url_for("portal_aviso.pantalla"))


@portal_aviso_bp.route("/portal-aviso/mandar", methods=["POST"])
@requiere_login
@requiere_permiso(PERMISO)
def mandar():
    """A los clientes marcados. Sólo con el interruptor prendido."""
    if not queries.a_clientes_encendido():
        flash("El envío a clientes está apagado. Primero prendelo, acá mismo.", "error")
        return redirect(url_for("portal_aviso.pantalla"))
    marcados = {c.strip().upper() for c in request.form.getlist("codigos") if c.strip()}
    filas = [f for f in queries.lista() if f["codigo_cli"] in marcados and f["correo"]]
    if not filas:
        flash("No marcaste a nadie con correo.", "error")
        return redirect(url_for("portal_aviso.pantalla"))
    envio.mandar_en_fondo(filas, _quien())
    flash(f"Saliendo el aviso a {len(filas)} clientes. Van apareciendo abajo a medida que salen.", "ok")
    return redirect(url_for("portal_aviso.pantalla"))


@portal_aviso_bp.route("/portal-aviso/interruptor", methods=["POST"])
@requiere_login
@requiere_permiso(PERMISO)
def interruptor():
    prender = (request.form.get("prender") or "") == "1"
    queries.encender_a_clientes(prender)
    flash("El envío a clientes quedó PRENDIDO." if prender
          else "El envío a clientes quedó apagado.", "ok")
    return redirect(url_for("portal_aviso.pantalla"))
