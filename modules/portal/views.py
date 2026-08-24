"""Las pantallas del portal del cliente.

Por ahora sólo la puerta: el esqueleto del proceso se levanta y se testea antes
que las pantallas, porque los tests de que el ERP **no existe** acá son el
corazón de la seguridad de todo esto.

Lo que viene, en orden (ver `PLAN_PORTAL_CLIENTE_2026_08_24.md`):
el ingreso con código + RUC, elegir la clave, el estado de cuenta, sus pagos y
cheques, y sus despachos.
"""
from __future__ import annotations

from flask import Blueprint, jsonify

portal_bp = Blueprint("portal", __name__, url_prefix="",
                      template_folder="templates")


@portal_bp.route("/", methods=["GET"])
def inicio():
    """La puerta del portal. Todavía sin pantalla."""
    return jsonify({"programa": "Portal Intela", "estado": "en construcción"})
