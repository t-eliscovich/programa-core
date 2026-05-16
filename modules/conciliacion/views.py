"""Rutas del módulo conciliación."""
from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from modules.conciliacion import queries
from modules.conciliacion.matcher import matchear
from modules.conciliacion.parser import parse_csv

conciliacion_bp = Blueprint(
    "conciliacion",
    __name__,
    url_prefix="/conciliacion",
    template_folder="templates",
)

_SESSION_KEY = "_conciliacion_sospechosos"


@conciliacion_bp.route("/", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("bancos.conciliar")
def index():
    """GET = formulario de upload; POST = procesa el CSV.

    Resultado: se guarda la lista de sospechosos en `session` (sólo ids +
    flags, no objetos pesados) y se redirige al visor.
    """
    if request.method == "POST":
        f = request.files.get("estado_cuenta")
        if not f or not f.filename:
            flash("Subí un archivo CSV del banco.", "warn")
            return redirect(url_for("conciliacion.index"))
        raw = f.read()
        lineas = parse_csv(raw)
        if not lineas:
            flash("El CSV no tenía líneas válidas. ¿Lo exportaste con fechas DD/MM/YYYY?", "warn")
            return redirect(url_for("conciliacion.index"))

        # Rango: desde la fecha más antigua del CSV, hasta la más nueva.
        fechas = [ln.fecha for ln in lineas if ln.fecha]
        desde = min(fechas) - timedelta(days=3) if fechas else date.today() - timedelta(days=45)
        hasta = max(fechas) if fechas else date.today()

        cheques = queries.cheques_depositados_rango(desde, hasta)
        result = matchear(lineas, cheques, dias_sospecha=3)

        # Guardar sólo lo minimal en la session para el paso siguiente.
        session[_SESSION_KEY] = [
            {
                "id_cheque": s["id_cheque"],
                "no_cheque": s["no_cheque"],
                "importe":   float(s["importe"]),
                "codigo_cli": s["codigo_cli"],
                "fechad":     s["fechad"].isoformat() if s["fechad"] else None,
                "dias":       s["dias_sin_match"],
                "razon":      s["razon"],
            }
            for s in result.sospechosos
        ]

        return render_template(
            "conciliacion/resultado.html",
            matches=result.matches,
            sospechosos=result.sospechosos,
            n_lineas=len(lineas),
            desde=desde,
            hasta=hasta,
        )

    return render_template("conciliacion/upload.html")


@conciliacion_bp.route("/confirmar-rebote", methods=["POST"])
@requiere_login
@requiere_permiso("cheques.anular")
def confirmar_rebote():
    """Confirma UN cheque como rebotado — dispara `cheques.reversar()`.

    La contadora revisa la bandeja de sospechosos y confirma uno por uno. NO
    hacemos bulk-confirm a propósito: queremos que alguien mire y decida cada
    caso (a veces el banco está atrasado, no es un rebote).
    """
    from modules.cheques import queries as chq

    try:
        id_cheque = int(request.form["id_cheque"])
    except (KeyError, ValueError, TypeError):
        flash("Falta el id_cheque.", "error")
        return redirect(url_for("conciliacion.index"))

    motivo = (request.form.get("motivo") or "Rechazado por banco (conciliación)").strip()
    try:
        res = chq.reversar(
            id_cheque=id_cheque,
            motivo=motivo,
            usuario=request.remote_user or "conciliacion",
        )
    except ValueError as e:
        flash_exc("No se pudo reversar el cheque", e)
        return redirect(url_for("conciliacion.index"))

    if res.get("stop_aplicado"):
        flash(f"Cheque #{id_cheque} reversado. Cliente a STOP por rebote real.", "ok")
    else:
        flash(f"Cheque #{id_cheque} reversado (sin side-effect de stop).", "ok")

    # Remover el cheque confirmado de la bandeja en session
    sospechosos = session.get(_SESSION_KEY, [])
    session[_SESSION_KEY] = [s for s in sospechosos if s.get("id_cheque") != id_cheque]

    return redirect(url_for("conciliacion.bandeja"))


@conciliacion_bp.route("/bandeja")
@requiere_login
@requiere_permiso("bancos.conciliar")
def bandeja():
    """Muestra la bandeja de sospechosos guardada en session."""
    sospechosos = session.get(_SESSION_KEY, [])
    return render_template("conciliacion/bandeja.html", sospechosos=sospechosos)
