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
    _prestar_plantillas_de_informes(app)


def _prestar_plantillas_de_informes(app: Flask) -> None:
    """Deja que el portal RESUELVA plantillas de otros módulos, sin sus rutas.

    Son dos: la hoja impresa del estado de cuenta (`informes/`) y los estilos
    del mobile (`mi_cartera/_estilos.html`).

    ⭐ El estado de cuenta impreso tiene que salir de la MISMA hoja que usan la
    oficina y los vendedores (`informes/_estado_cuenta_impreso.html`). Dos
    plantillas del mismo documento divergen a la primera corrección — pasó ya
    con el papel que el vendedor le deja al cliente, y por eso `mi_cartera`
    tampoco arma la suya.

    Esa hoja vive en la carpeta del blueprint de informes, que acá NO se
    registra. Prestarle la CARPETA a Jinja no le abre ni una ruta: sigue sin
    existir `/informes/*`. Es exactamente lo que queremos — el documento
    compartido, las pantallas no.

    Se hace así y no moviendo el archivo a `templates/` porque media docena de
    tests lo referencian por su ruta actual, y mover un archivo que costó ocho
    vueltas de ajuste para ganar comodidad no vale el riesgo.
    """
    from pathlib import Path

    from jinja2 import ChoiceLoader, FileSystemLoader

    raiz = Path(__file__).resolve().parent / "modules"
    carpetas = [raiz / "informes" / "templates",
                # Y las del portal de VENDEDORES, por sus estilos: el del
                # cliente tiene que verse como lo que ya existe, no inventar
                # una estética nueva. Ver `mi_cartera/_estilos.html`.
                raiz / "mi_cartera" / "templates"]
    vivas = [FileSystemLoader(str(c)) for c in carpetas if c.is_dir()]
    if not vivas:                     # pragma: no cover - sólo si se mueve el repo
        return
    app.jinja_loader = ChoiceLoader([app.jinja_loader, *vivas])
