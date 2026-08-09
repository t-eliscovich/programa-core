"""Usuarios — administración: listar / crear / editar / desactivar."""
import logging

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
from parsers import parse_int

from . import accesos as _accesos
from . import queries

_LOG = logging.getLogger("programa_core.usuarios")

usuarios_bp = Blueprint("usuarios", __name__, template_folder="templates")


@usuarios_bp.route("/usuarios")
@requiere_login
@requiere_permiso("usuarios.admin")
def lista():
    try:
        filas = queries.listar()
    except Exception as e:  # noqa: BLE001
        _LOG.exception("usuarios.listar() falló: %s", e)
        flash(f"No pude cargar la lista de usuarios: {e}", "error")
        filas = []

    # TMT 2026-08-03 (dueña, viendo dos cuentas repetidas que quedaron del
    # consolidado de Google): "podés eliminar el repetido que no está activo".
    #
    # No se BORRA una cuenta: desactivar ya le saca todo el acceso, y el
    # username sigue apareciendo en la bitácora — una fila borrada deja el
    # historial firmado por alguien que "no existe". Lo que molestaba era que
    # ensuciaban la lista, así que los desactivados se esconden y quedan
    # detrás de un contador. Sin botón de borrar: no existe en la app, y
    # agregarlo por dos filas sería dejar un botón irreversible en producción
    # para siempre.
    ver_inactivos = request.args.get("inactivos") == "1"
    n_inactivos = sum(1 for f in filas if not f.get("activo"))
    if not ver_inactivos:
        filas = [f for f in filas if f.get("activo")]

    # TMT 2026-05-22 — defensive: calcular "_real" (usuario real para
    # impersonación) acá en vez del template, donde g.impersonating_from
    # puede no estar definido en algunos contextos.
    real_user = getattr(g, "impersonating_from", None) or getattr(g, "user", None)
    real_id = real_user.get("id_usuario") if real_user else None
    real_es_accionista = bool(
        real_user and (real_user.get("nombre_rol") or "").strip().lower() == "accionista"
    )

    return render_template(
        "usuarios/lista.html",
        filas=filas,
        real_id=real_id,
        real_es_accionista=real_es_accionista,
        ver_inactivos=ver_inactivos,
        n_inactivos=n_inactivos,
    )


@usuarios_bp.route("/usuarios/nuevo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def nuevo():
    errores: list[str] = []
    roles = queries.roles_disponibles()
    vendedores = queries.vendedores_disponibles()
    if request.method == "GET":
        # TMT 2026-08-03 — el alta se puede PRE-LLENAR por querystring:
        #   /usuarios/nuevo?username=roberto&rol=Vendedor&vend=RMY
        # Sirve para dar de alta una tanda (los 6 vendedores) sin tipear el
        # mismo formulario seis veces: se abre el link y sólo queda poner la
        # contraseña, que es justamente lo único que NO tiene que viajar en
        # una URL. Pre-llenar no es crear: nada se guarda hasta que la
        # persona aprieta Guardar.
        rol_pedido = (request.args.get("rol") or "").strip().lower()
        id_rol_pedido = next(
            (r["id_rol"] for r in roles
             if (r["nombre_rol"] or "").strip().lower() == rol_pedido),
            None,
        )
        form = {
            "username": (request.args.get("username") or "").strip().lower(),
            "id_rol": id_rol_pedido or parse_int(request.args.get("id_rol")),
            "vend": (request.args.get("vend") or "").strip().upper(),
            "clave": (request.args.get("clave") or "").strip().upper(),
        }
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, vendedores=vendedores, modo="crear")

    form = {
        "username": (request.form.get("username") or "").strip().lower(),
        "id_rol": parse_int(request.form.get("id_rol")),
        "clave": (request.form.get("clave") or "").strip().upper(),
        "vend": (request.form.get("vend") or "").strip().upper(),
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
                               roles=roles, vendedores=vendedores, modo="crear"), 400

    try:
        queries.crear(
            username=form["username"],
            password=password,
            id_rol=form["id_rol"],
            clave=form["clave"] or None,
            vend=form["vend"] or None,
        )
        flash(f"Usuario {form['username']} creado.", "ok")
        return redirect(url_for("usuarios.lista"))
    except ValueError as e:
        errores.append(str(e))
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, vendedores=vendedores, modo="crear"), 400
    except Exception as e:
        errores.append(f"No pude crear: {e}")
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, vendedores=vendedores, modo="crear"), 500


@usuarios_bp.route("/usuarios/<int:id_usuario>/editar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("usuarios.admin")
def editar(id_usuario: int):
    u = queries.por_id(id_usuario)
    if not u:
        abort(404)
    roles = queries.roles_disponibles()
    vendedores = queries.vendedores_disponibles()
    errores: list[str] = []

    if request.method == "GET":
        form = {
            "id_usuario": u["id_usuario"],
            "username": u["username"],
            "email": u.get("email") or "",
            "id_rol": u["id_rol"],
            "clave": u.get("clave") or "",
            "vend": u.get("vend") or "",
            "activo": u.get("activo", True),
        }
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, vendedores=vendedores, modo="editar")

    id_rol = parse_int(request.form.get("id_rol")) or u["id_rol"]
    clave = (request.form.get("clave") or "").strip().upper() or None
    email = (request.form.get("email") or "").strip().lower() or None
    # Siempre string (nunca None) para que "sacarle el vendedor" a un usuario
    # llegue a queries.editar como '' → NULL, y no como "no lo toques".
    vend = (request.form.get("vend") or "").strip().upper()
    # ⚠ El form manda DOS inputs con name="activo": un hidden value="0" y el
    # checkbox value="1" (patrón clásico para que "destildado" viaje). El
    # navegador postea AMBOS —"activo=0&activo=1"— cuando está tildado, y
    # `request.form.get()` devuelve el PRIMERO, o sea "0" SIEMPRE. Resultado:
    # cualquier guardada de esta pantalla DESACTIVABA al usuario, aunque el
    # checkbox se viera tildado. Se le cambió la contraseña a maribel el
    # 2026-08-05 y desapareció del listado. Hay que mirar TODOS los valores.
    activo = "1" in request.form.getlist("activo")
    password = request.form.get("password") or ""
    password_confirm = request.form.get("password_confirm") or ""

    if password and password != password_confirm:
        errores.append("Las contraseñas no coinciden.")
    if password and len(password) < 6:
        errores.append("Password debe tener al menos 6 caracteres.")

    if errores:
        form = {**u, "id_rol": id_rol, "clave": clave or "",
                "vend": vend, "activo": activo}
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, vendedores=vendedores, modo="editar"), 400

    try:
        queries.editar(
            id_usuario, id_rol=id_rol, clave=clave,
            activo=activo, password=password or None,
            email=email, vend=vend,
        )
        flash(f"Usuario {u['username']} actualizado.", "ok")
        return redirect(url_for("usuarios.lista"))
    except Exception as e:
        errores.append(f"No pude actualizar: {e}")
        form = {**u, "id_rol": id_rol, "clave": clave or "",
                "vend": vend, "activo": activo}
        return render_template("usuarios/form.html", form=form, errores=errores,
                               roles=roles, vendedores=vendedores, modo="editar"), 500


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


@usuarios_bp.route("/usuarios/accesos")
@requiere_login
@requiere_permiso("usuarios.admin")
def mapa_accesos():
    """Qué pantalla abre cada rol — ver el docstring de `accesos.py`.

    Va detrás de `usuarios.admin` (Accionista / Administrador): es el mismo
    permiso que da de alta la gente, y esta pantalla dice exactamente qué le
    estás dando cuando elegís un rol.
    """
    from flask import current_app

    m = _accesos.mapa(current_app)
    roles = _accesos.roles_con_permisos()
    # Los roles sin un solo usuario activo van aparte: existen en el catálogo
    # pero hoy no los usa nadie, y mezclarlos infla la tabla a diez columnas.
    con_gente = [r for r in roles if r["usuarios"] > 0]
    vacios = [r for r in roles if r["usuarios"] == 0]
    # ?tecnicas=1 muestra también los JSON, los debug y las descargas. Por
    # defecto no: la pregunta de esta pantalla es "qué VE cada rol" y esas
    # rutas no las abre nadie. TMT 2026-08-09 ("¿podemos simplificar esta
    # lista?"), con el contador siempre a la vista para que se sepa qué falta.
    ver_tecnicas = request.args.get("tecnicas") == "1"
    return render_template(
        "usuarios/accesos.html",
        grupos=m["grupos"],
        sin_permiso=m["sin_permiso"],
        aceptadas=m["aceptadas"],
        control_propio=m["control_propio"],
        roles=con_gente,
        roles_vacios=vacios,
        puede=_accesos.puede,
        ver_tecnicas=ver_tecnicas,
        n_tecnicas=sum(len(p["tecnicas"]) for g in m["grupos"] for p in g["permisos"]),
    )
