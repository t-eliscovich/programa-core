"""Listado de retiros del dueño."""
from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso
from exports import csv_response

from . import queries

retiros_bp = Blueprint("retiros", __name__, template_folder="templates")


@retiros_bp.route("/retiros")
@requiere_login
@requiere_permiso("retiros.ver")
def lista():
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    de = (request.args.get("de") or "").strip().upper() or None

    try:
        filas = queries.buscar(q, desde, hasta, de=de)
        resumen = queries.resumen(desde, hasta)
        por_persona = queries.totales_por_persona(desde, hasta)
        error = None
    except Exception as e:
        filas, resumen, por_persona, error = [], {}, [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha",    "Fecha"),
                ("de",       "De"),
                ("nb",       "N° banco"),
                ("banco",    "Banco"),
                ("ret",      "Importe"),
                ("concepto", "Concepto"),
                ("clave",    "Clave"),
            ],
            filename="retiros.csv",
        )

    total = sum(float(r["ret"] or 0) for r in filas)
    return render_template(
        "retiros/lista.html",
        filas=filas, q=q, desde=desde, hasta=hasta, de=de,
        total=total, resumen=resumen, por_persona=por_persona,
        error=error,
    )
