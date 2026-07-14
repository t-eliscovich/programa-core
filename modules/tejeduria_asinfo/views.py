"""Tab 'Producción Tejeduría Asinfo' — resumen diario/mensual de kg producidos
por tejedor (Asinfo) y match contra las compras tipo K de Programa Core.

Reemplaza la carga manual del dBase: muestra lo que Asinfo dice que se produjo
(INTELA + tercerizados), lo cruza contra lo cargado en compras, y filtra las
OFs tercerizadas SIN compra para que se puedan cargar (el humano pone el $).
"""
from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso
from filters import today_ec

from . import service

tejeduria_asinfo_bp = Blueprint(
    "tejeduria_asinfo", __name__, template_folder="templates",
)


@tejeduria_asinfo_bp.route("/produccion-tejeduria-asinfo")
@requiere_login
@requiere_permiso("compras.ver")
def tab():
    hoy = today_ec()
    try:
        anio = int(request.args.get("anio") or hoy.year)
    except (TypeError, ValueError):
        anio = hoy.year
    try:
        mes = int(request.args.get("mes") or hoy.month)
    except (TypeError, ValueError):
        mes = hoy.month
    mes = max(1, min(mes, 12))
    data = service.resumen_mes(anio, mes)
    return render_template(
        "tejeduria_asinfo/tab.html", data=data, anio=anio, mes=mes,
    )
