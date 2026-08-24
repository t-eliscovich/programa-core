"""Las pantallas del PORTAL DEL CLIENTE. Lo único que se registra en modo portal.

⭐ La lista de acá es la superficie que queda expuesta a internet. Cada
blueprint que se agregue la agranda, así que se agrega de a uno y con su test.

Lo que NO va acá, aunque tiente: nada del ERP. Si el portal necesita un dato
que hoy sólo calcula una pantalla de la oficina, se saca la CONSULTA a un
módulo compartido y se la llama desde las dos — no se registra la pantalla de
la oficina en el portal.

Ver `modo.py` y `registro_erp.py`.
"""
from __future__ import annotations

from flask import Flask


def registrar(app: Flask) -> None:
    """Registra las pantallas del portal del cliente en `app`."""
    from modules.portal.views import portal_bp

    app.register_blueprint(portal_bp)
