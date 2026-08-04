"""Tab 'Producción Terminado Asinfo' — kg de producto terminado por OF y día.

Copia liviana de /produccion-tejeduria-asinfo para el OTRO paso de fabricación
(bodega 53: tela cruda → producto terminado). SOLO LECTURA: el terminado lo
hace la fábrica, no hay maquilero, así que no hay tarifas, ni carga de compras,
ni pasivos. Ver el docstring de `service.py`.

Rutas:
  GET /produccion-terminado-asinfo    (stock.ver — el mismo gate que el resto
                                       de la sección "Producción y stocks",
                                       que en config/roles.py tienen TODOS los
                                       roles: es data de producción, no plata)
"""
from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso
from filters import today_ec

from . import service

terminado_asinfo_bp = Blueprint(
    "terminado_asinfo", __name__, template_folder="templates",
)


def _anio_mes():
    """(anio, mes) de la query string, con el mes en curso como default.

    Mismo helper que tejeduría — se duplica adrede: son 12 líneas y compartirlo
    ataría dos pantallas que no tienen ninguna otra razón para conocerse.
    """
    hoy = today_ec()
    try:
        anio = int(request.values.get("anio") or hoy.year)
    except (TypeError, ValueError):
        anio = hoy.year
    try:
        mes = int(request.values.get("mes") or hoy.month)
    except (TypeError, ValueError):
        mes = hoy.month
    return anio, max(1, min(mes, 12))


@terminado_asinfo_bp.route("/produccion-terminado-asinfo")
@requiere_login
@requiere_permiso("stock.ver")
def tab():
    anio, mes = _anio_mes()
    data = service.resumen_mes(anio, mes)
    return render_template(
        "terminado_asinfo/tab.html", data=data, anio=anio, mes=mes,
    )
