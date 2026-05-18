"""Provisiones (acumulaciones recurrentes)."""
from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response
from parsers import parse_monto

from . import queries

provisiones_bp = Blueprint("provisiones", __name__, template_folder="templates")


def _form_from_request() -> dict:
    return {
        "concepto": (request.form.get("concepto") or "").strip(),
        "importe": (request.form.get("importe") or "").strip(),
        "periodo_aplica": (request.form.get("periodo_aplica") or "").strip(),
    }


@provisiones_bp.route("/provisiones/nueva", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("provisiones.crear")
def nueva():
    errores: list[str] = []
    if request.method == "GET":
        return render_template(
            "provisiones/form.html", form={}, errores=errores, modo="crear"
        )

    form = _form_from_request()
    importe = parse_monto(form["importe"])
    if not form["concepto"]:
        errores.append("Concepto requerido.")
    if importe is None:
        errores.append("Importe inválido.")
    if not form["periodo_aplica"]:
        errores.append("Período requerido.")

    if errores:
        return (
            render_template(
                "provisiones/form.html", form=form, errores=errores, modo="crear"
            ),
            400,
        )

    try:
        usuario = (g.user or {}).get("username", "web")
        queries.crear(
            concepto=form["concepto"], importe=importe,
            periodo_aplica=form["periodo_aplica"], usuario=usuario,
        )
        flash(f"Provisión '{form['concepto']}' creada.", "ok")
        return redirect(url_for("provisiones.lista"))
    except ValueError as e:
        errores.append(str(e))
        return (
            render_template(
                "provisiones/form.html", form=form, errores=errores, modo="crear"
            ),
            400,
        )
    except Exception as e:
        errores.append(f"No pude crear: {e}")
        return (
            render_template(
                "provisiones/form.html", form=form, errores=errores, modo="crear"
            ),
            500,
        )


@provisiones_bp.route("/provisiones/<int:id_provisiones>/editar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("provisiones.editar")
def editar(id_provisiones: int):
    prov = queries.por_id(id_provisiones)
    if not prov:
        abort(404)
    errores: list[str] = []

    if request.method == "GET":
        form = {
            "id_provisiones": prov["id_provisiones"],
            "concepto": prov.get("concepto") or "",
            "importe": prov.get("importe") or "",
            "periodo_aplica": prov.get("periodo_aplica") or "",
        }
        return render_template(
            "provisiones/form.html", form=form, errores=errores, modo="editar"
        )

    form = _form_from_request()
    form["id_provisiones"] = id_provisiones
    importe = parse_monto(form["importe"])
    if not form["concepto"]:
        errores.append("Concepto requerido.")
    if importe is None:
        errores.append("Importe inválido.")
    if errores:
        return (
            render_template(
                "provisiones/form.html", form=form, errores=errores, modo="editar"
            ),
            400,
        )

    try:
        usuario = (g.user or {}).get("username", "web")
        queries.editar(
            id_provisiones,
            concepto=form["concepto"], importe=importe,
            periodo_aplica=form["periodo_aplica"] or None,
            usuario=usuario,
        )
        flash("Provisión actualizada.", "ok")
        return redirect(url_for("provisiones.lista"))
    except Exception as e:
        errores.append(f"No pude actualizar: {e}")
        return (
            render_template(
                "provisiones/form.html", form=form, errores=errores, modo="editar"
            ),
            500,
        )


@provisiones_bp.route("/provisiones/<int:id_provisiones>/importe", methods=["POST"])
@requiere_login
@requiere_permiso("provisiones.editar")
def actualizar_importe(id_provisiones: int):
    """TMT 2026-05-18 — quick inline edit del importe desde la lista.

    La dueña dijo que "esto cambia con cierta frecuencia" y abrir el form
    completo para cambiar un número es engorroso.
    """
    prov = queries.por_id(id_provisiones)
    if not prov:
        abort(404)
    importe = parse_monto(request.form.get("importe"))
    if importe is None:
        flash("Importe inválido.", "error")
        return redirect(url_for("provisiones.lista"))
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.editar(id_provisiones, importe=importe, usuario=usuario)
        flash(f"Importe de '{prov.get('concepto') or ''}' actualizado a $ {importe}.", "ok")
    except Exception as e:
        flash_exc("No pude actualizar", e)
    return redirect(url_for("provisiones.lista"))


@provisiones_bp.route("/provisiones/<int:id_provisiones>/confirmar-eliminacion", methods=["GET"])
@requiere_login
@requiere_permiso("provisiones.editar")
def confirmar_eliminacion(id_provisiones: int):
    """Paso 1 del 2-step: resumen + motivo antes de eliminar."""
    prov = queries.por_id(id_provisiones)
    if not prov:
        abort(404)
    detalle = {
        "Concepto": prov.get("concepto") or "—",
        "Importe": f"$ {prov.get('importe') or 0}",
        "Periodo": prov.get("periodo_aplica") or "—",
    }
    return render_template(
        "_confirmar_accion.html",
        titulo=f"Eliminar provisión #{id_provisiones}",
        mensaje=(
            f"Vas a eliminar la provisión '{prov.get('concepto') or '(sin concepto)'}' "
            f"por $ {prov.get('importe') or 0}."
        ),
        detalle_registro=detalle,
        accion_url=url_for("provisiones.eliminar", id_provisiones=id_provisiones),
        volver_url=url_for("provisiones.lista"),
        motivo_requerido=True,
        confirm_label="Confirmar eliminación",
    )


@provisiones_bp.route("/provisiones/<int:id_provisiones>/eliminar", methods=["POST"])
@requiere_login
@requiere_permiso("provisiones.editar")
def eliminar(id_provisiones: int):
    prov = queries.por_id(id_provisiones)
    if not prov:
        abort(404)
    (request.form.get("motivo") or "").strip()  # opcional. TMT 2026-05-13.
    try:
        queries.eliminar(id_provisiones)
        flash("Provisión eliminada.", "ok")
    except Exception as e:
        flash_exc("No pude eliminar", e)
    return redirect(url_for("provisiones.lista"))


@provisiones_bp.route("/provisiones")
@requiere_login
@requiere_permiso("provisiones.ver")
def lista():
    q = (request.args.get("q") or "").strip()
    try:
        filas = queries.lista(q=q)
        resumen = queries.resumen() or {}
        error = None
    except Exception as e:
        filas, resumen, error = [], {}, str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("concepto", "Concepto"),
                ("importe", "Importe"),
                ("periodo_aplica", "Período"),
                ("fecha_actualiza", "Última actualización"),
                ("usuario_actualiza", "Usuario"),
            ],
            filename="provisiones.csv",
        )

    return render_template(
        "provisiones/lista.html",
        filas=filas, q=q, resumen=resumen, error=error,
    )
