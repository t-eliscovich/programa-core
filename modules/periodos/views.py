"""Períodos contables — UI de cierre mensual."""
from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc

from . import queries

periodos_bp = Blueprint("periodos", __name__, template_folder="templates")


@periodos_bp.route("/periodos")
@requiere_login
@requiere_permiso("periodo.cerrar")
def lista():
    filas = queries.listar()
    ultimo = queries.ultimo_cierre()
    return render_template("periodos/lista.html", filas=filas, ultimo=ultimo)


@periodos_bp.route("/periodos/<int:id_periodo>/cerrar", methods=["POST"])
@requiere_login
@requiere_permiso("periodo.cerrar")
def cerrar(id_periodo: int):
    motivo = (request.form.get("motivo") or "").strip()
    if not motivo:
        flash("Motivo requerido.", "warn")
        return redirect(url_for("periodos.lista"))
    try:
        usuario = (g.user or {}).get("username", "web")
        n = queries.cerrar(id_periodo, motivo=motivo, usuario=usuario)
        if n == 0:
            flash("El período ya estaba cerrado.", "warn")
        else:
            flash("Período cerrado.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude cerrar", e)
    return redirect(url_for("periodos.lista"))


@periodos_bp.route("/periodos/<int:id_periodo>/reabrir", methods=["POST"])
@requiere_login
@requiere_permiso("periodo.cerrar")
def reabrir(id_periodo: int):
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.reabrir(id_periodo, usuario=usuario)
        flash("Período reabierto.", "ok")
    except Exception as e:
        flash_exc("No pude reabrir", e)
    return redirect(url_for("periodos.lista"))
