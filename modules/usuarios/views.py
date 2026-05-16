"""Usuarios — administración: listar / crear / editar / desactivar."""
from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from parsers import parse_int

from . import queries

usuarios_bp = Blueprint("usuarios", __name__, template_folder="templates")


@usuarios_bp.route("/usuarios")
@requiere_login
@requiere_permiso("usuarios.admin")
def lista():
    filas = queries.listar()
    return render_template("usuarios/lista.html", filas=filas)


@usuarios_bp.route("/usuarios/nuevo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def nuevo():
    errores: list[str] = []
    roles = queries.roles_disponibles()
    if request.method == "GET":
        return render_template("usuarios/form.html", form={}, errores=errores,
                               roles=roles, modo="crear")

    form = {
        "username": (request.form.get("username") or "").strip().lower(),
        "id_rol": parse_int(request.form.get("id_rol")),
        "clave": (request.form.get("clave") or "").strip().upper(),
    }
    password = request.form.get("password") or ""
    password_confirm = request.form.get("password_confirm") or ""

    if not form["username"]:
        errores.append("Username requerido.")
    if not form["id_rol"]:
        errores.append("Rol requerido.")
    if password != password_confirm:
        errores.append("Las contraseñas no coinciden.")
    if len(password) < 6:
        errores.append("Password debe tener al menos 6 caracteres.")

    if errores:
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, modo="crear"), 400

    try:
        queries.crear(
            username=form["username"],
            password=password,
            id_rol=form["id_rol"],
            clave=form["clave"] or None,
        )
        flash(f"Usuario {form['username']} creado.", "ok")
        return redirect(url_for("usuarios.lista"))
    except ValueError as e:
        errores.append(str(e))
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, modo="crear"), 400
    except Exception as e:
        errores.append(f"No pude crear: {e}")
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, modo="crear"), 500


@usuarios_bp.route("/usuarios/<int:id_usuario>/editar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def editar(id_usuario: int):
    u = queries.por_id(id_usuario)
    if not u:
        abort(404)
    roles = queries.roles_disponibles()
    errores: list[str] = []

    if request.method == "GET":
        form = {
            "id_usuario": u["id_usuario"],
            "username": u["username"],
            "id_rol": u["id_rol"],
            "clave": u.get("clave") or "",
            "activo": u.get("activo", True),
        }
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, modo="editar")

    id_rol = parse_int(request.form.get("id_rol")) or u["id_rol"]
    clave = (request.form.get("clave") or "").strip().upper() or None
    activo = (request.form.get("activo") or "").strip() == "1"
    password = request.form.get("password") or ""
    password_confirm = request.form.get("password_confirm") or ""

    if password and password != password_confirm:
        errores.append("Las contraseñas no coinciden.")
    if password and len(password) < 6:
        errores.append("Password debe tener al menos 6 caracteres.")

    if errores:
        form = {**u, "id_rol": id_rol, "clave": clave or "", "activo": activo}
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, modo="editar"), 400

    try:
        queries.editar(
            id_usuario, id_rol=id_rol, clave=clave,
            activo=activo, password=password or None,
        )
        flash(f"Usuario {u['username']} actualizado.", "ok")
        return redirect(url_for("usuarios.lista"))
    except Exception as e:
        errores.append(f"No pude actualizar: {e}")
        form = {**u, "id_rol": id_rol, "clave": clave or "", "activo": activo}
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, modo="editar"), 500


@usuarios_bp.route("/usuarios/<int:id_usuario>/activo", methods=["POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def toggle_activo(id_usuario: int):
    u = queries.por_id(id_usuario)
    if not u:
        abort(404)
    activo = (request.form.get("set") or "").strip() == "1"
    try:
        queries.set_activo(id_usuario, activo)
        flash(
            f"Usuario {u['username']} — {'ACTIVADO' if activo else 'DESACTIVADO'}.",
            "ok",
        )
    except Exception as e:
        flash_exc("No pude cambiar estado", e)
    return redirect(url_for("usuarios.lista"))
