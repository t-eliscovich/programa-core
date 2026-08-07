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
    # TMT 2026-08-07 (dueña, sobre los links del historial): "esos links
    # deberían venir filtrados por lo que quiero ver" / "si al clickear hay que
    # buscar la fila a ojo, el link no está terminado". ?id=<id_retiro> deja UNA
    # fila. Un id que no es número se ignora (no 500, no se cuela en el SQL).
    try:
        id_retiro = int(request.args.get("id") or 0) or None
    except (TypeError, ValueError):
        id_retiro = None

    try:
        filas = queries.buscar(q, desde, hasta, de=de, id_retiro=id_retiro)
        # El id va a TODOS los agregados de la pantalla, no sólo a la grilla:
        # `resumen` es el hero (Total retirado / N retiros / Ticket / Periodo) y
        # `totales_por_persona` son los chips «Por código». Si no se les pasa,
        # la pantalla muestra los totales de todo el período arriba de una sola
        # fila — el contador incongruente que ya mordió en /posdat.
        resumen = queries.resumen(desde, hasta, id_retiro=id_retiro)
        por_persona = queries.totales_por_persona(desde, hasta, id_retiro=id_retiro)
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
