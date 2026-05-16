"""Listado y CRUD de clientes."""
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
from parsers import parse_int

from . import queries

clientes_bp = Blueprint("clientes", __name__, template_folder="templates")


def _form_from_request() -> dict:
    """Extrae los campos del request.form para reusar en GET/POST de nuevo/editar."""
    return {
        "codigo_cli": (request.form.get("codigo_cli") or "").strip().upper(),
        "nombre": (request.form.get("nombre") or "").strip(),
        "ruc": (request.form.get("ruc") or "").strip(),
        "telefono": (request.form.get("telefono") or "").strip(),
        "correo": (request.form.get("correo") or "").strip(),
        "direccion1": (request.form.get("direccion1") or "").strip(),
        "direccion2": (request.form.get("direccion2") or "").strip(),
        "pago": (request.form.get("pago") or "").strip(),
        "cupo": request.form.get("cupo") or "",
        "vend": (request.form.get("vend") or "").strip(),
        "observacion": (request.form.get("observacion") or "").strip(),
    }


def _safe_next_url(raw: str | None) -> str | None:
    """Validar que `next` sea una ruta interna (empieza con `/` y no `//`).

    Sin esto, alguien con un link malicioso podría redirigir al usuario a
    un sitio externo después de la operación. Acepta sólo URLs relativas
    al mismo host.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith("/") or raw.startswith("//"):
        return None
    return raw


@clientes_bp.route("/clientes/nuevo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("clientes.crear")
def nuevo():
    errores: list[str] = []
    # `?codigo=XXX` pre-carga el código (típicamente desde facturas/nueva
    # cuando el usuario tipeó un cliente que no existe).
    # `?next=/url` indica adónde volver tras guardar (default: /clientes).
    pre_codigo = (request.args.get("codigo") or "").strip().upper()
    next_url = _safe_next_url(request.args.get("next"))

    if request.method == "GET":
        form = {"codigo_cli": pre_codigo} if pre_codigo else {}
        return render_template(
            "clientes/form.html", form=form, errores=errores, modo="crear",
            next_url=next_url, pre_codigo=pre_codigo,
        )

    form = _form_from_request()
    cupo = parse_int(form["cupo"])
    # POST también puede traer `next` como hidden — preferir ese sobre el
    # query string porque sobrevive al re-render con errores.
    next_url = _safe_next_url(request.form.get("next") or request.args.get("next"))

    if not form["codigo_cli"]:
        errores.append("Código requerido.")
    if not form["nombre"]:
        errores.append("Nombre requerido.")

    if errores:
        return render_template(
            "clientes/form.html", form=form, errores=errores, modo="crear",
            next_url=next_url,
        ), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        clave = (g.user or {}).get("clave") or usuario[:3].upper()
        queries.crear(
            codigo_cli=form["codigo_cli"], nombre=form["nombre"],
            ruc=form["ruc"] or None, telefono=form["telefono"] or None,
            correo=form["correo"] or None,
            direccion1=form["direccion1"] or None, direccion2=form["direccion2"] or None,
            pago=form["pago"] or None, cupo=cupo,
            vend=form["vend"] or None, observacion=form["observacion"] or None,
            clave=clave, usuario=usuario,
        )
        flash(f"Cliente {form['codigo_cli']} creado.", "ok")
        if next_url:
            return redirect(next_url)
        return redirect(url_for("clientes.lista"))
    except ValueError as e:
        errores.append(str(e))
        return render_template(
            "clientes/form.html", form=form, errores=errores, modo="crear",
            next_url=next_url,
        ), 400
    except Exception as e:
        errores.append(f"No pude crear el cliente: {e}")
        return render_template(
            "clientes/form.html", form=form, errores=errores, modo="crear",
            next_url=next_url,
        ), 500


@clientes_bp.route("/clientes/<codigo_cli>/editar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("clientes.editar")
def editar(codigo_cli: str):
    cli = queries.por_codigo(codigo_cli)
    if not cli:
        abort(404)
    errores: list[str] = []

    if request.method == "GET":
        form = {
            "codigo_cli": cli["codigo_cli"], "nombre": cli.get("nombre") or "",
            "ruc": cli.get("ruc") or "", "telefono": cli.get("telefono") or "",
            "correo": cli.get("correo") or "", "direccion1": cli.get("direccion1") or "",
            "direccion2": cli.get("direccion2") or "",
            "pago": cli.get("pago") or "", "cupo": cli.get("cupo") or "",
            "vend": cli.get("vend") or "", "observacion": cli.get("observacion") or "",
            "stop": cli.get("stop") or "N",
            "activo": cli.get("activo", True),
        }
        return render_template("clientes/form.html", form=form, errores=errores, modo="editar")

    form = _form_from_request()
    cupo = parse_int(form["cupo"])

    if not form["nombre"]:
        errores.append("Nombre requerido.")
    if errores:
        form["codigo_cli"] = cli["codigo_cli"]
        return render_template("clientes/form.html", form=form, errores=errores, modo="editar"), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        queries.editar(
            cli["codigo_cli"],
            nombre=form["nombre"], ruc=form["ruc"] or None,
            telefono=form["telefono"] or None, correo=form["correo"] or None,
            direccion1=form["direccion1"] or None, direccion2=form["direccion2"] or None,
            pago=form["pago"] or None, cupo=cupo,
            vend=form["vend"] or None, observacion=form["observacion"] or None,
            usuario=usuario,
        )
        flash(f"Cliente {cli['codigo_cli']} actualizado.", "ok")
        return redirect(url_for("clientes.lista"))
    except Exception as e:
        errores.append(f"No pude actualizar: {e}")
        return render_template("clientes/form.html", form=form, errores=errores, modo="editar"), 500


@clientes_bp.route("/clientes/<codigo_cli>/stop", methods=["POST"])
@requiere_login
@requiere_permiso("stop_cliente.editar")
def toggle_stop(codigo_cli: str):
    cli = queries.por_codigo(codigo_cli)
    if not cli:
        abort(404)
    set_stop = (request.form.get("set") or "").upper() == "S"
    motivo = (request.form.get("motivo") or "").strip()
    # Activar STOP requiere motivo. Sacar STOP no (es restitución).
    # TMT 2026-05-13.
    if set_stop and not motivo:
        flash(
            f"Motivo requerido para poner el cliente {codigo_cli} en STOP. "
            "Queda en bitácora.",
            "warn",
        )
        return redirect(url_for("clientes.lista"))
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.set_stop(codigo_cli, set_stop, usuario=usuario, motivo=motivo)
        flash(
            f"Cliente {codigo_cli} — stop {'ACTIVADO' if set_stop else 'DESACTIVADO'}.",
            "ok",
        )
    except Exception as e:
        flash_exc("No pude cambiar stop", e)
    return redirect(url_for("clientes.lista"))


@clientes_bp.route("/clientes/<codigo_cli>/activar", methods=["POST"])
@requiere_login
@requiere_permiso("clientes.editar")
def toggle_activo(codigo_cli: str):
    """Soft-delete / re-activar (legacy DIFUNTOS).

    Marcar inactivo NO borra al cliente — sus facturas históricas, saldos
    vivos y movimientos quedan intactos. Sólo lo esconde de los autocompletes
    y la lista por default.
    """
    cli = queries.por_codigo(codigo_cli)
    if not cli:
        abort(404)
    activar = (request.form.get("set") or "").lower() in ("1", "s", "si", "true", "yes")
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.set_activo(codigo_cli, activar, usuario=usuario)
        flash(
            f"Cliente {codigo_cli} — {'REACTIVADO' if activar else 'marcado como INACTIVO (difunto)'}.",
            "ok",
        )
    except Exception as e:
        flash_exc("No pude cambiar activo", e)
    return redirect(url_for("clientes.lista"))


@clientes_bp.route("/clientes/contactos")
@requiere_login
@requiere_permiso("clientes.ver")
def contactos():
    """Directorio de contactos — agenda rápida con tel/email + acciones directas."""
    q = request.args.get("q", "").strip()
    try:
        filas = queries.directorio(q=q)
        resumen = queries.directorio_resumen()
        error = None
    except Exception as e:
        filas, resumen, error = [], {}, str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("codigo_cli", "Código"),
                ("nombre", "Cliente"),
                ("telefono", "Teléfono"),
                ("correo", "Email"),
                ("stop", "Stop"),
                ("vend", "Vend"),
                ("provincia", "Provincia"),
            ],
            filename="directorio_contactos.csv",
        )

    return render_template(
        "clientes/contactos.html",
        filas=filas, q=q, resumen=resumen, error=error,
    )


@clientes_bp.route("/clientes")
@requiere_login
@requiere_permiso("clientes.ver")
def lista():
    q = request.args.get("q", "").strip()
    incluir_inactivos = request.args.get("inactivos") == "1"
    try:
        filas = queries.buscar(q, incluir_inactivos=incluir_inactivos)
        error = None
    except Exception as e:
        filas, error = [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("codigo_cli", "Código"),
                ("nombre", "Cliente"),
                ("ruc", "RUC"),
                ("telefono", "Teléfono"),
                ("pago", "Pago"),
                ("vend", "Vend"),
                ("stop", "Stop"),
                ("cupo", "Cupo"),
                ("saldo_total", "Saldo"),
                ("n_abiertas", "Fact. abiertas"),
            ],
            filename="clientes.csv",
        )

    return render_template(
        "clientes/lista.html",
        filas=filas, q=q, error=error,
        incluir_inactivos=incluir_inactivos,
    )


@clientes_bp.route("/clientes/<codigo_cli>/cuenta", methods=["GET"])
@requiere_login
@requiere_permiso("clientes.ver")
def cuenta(codigo_cli: str):
    """Cuenta corriente del cliente: timeline unificado de movimientos.

    Junta facturas, devoluciones, aplicaciones de cheque y retenciones
    en una sola lista ordenada por fecha, con saldo acumulado. Útil
    para clientes que pagan desordenado (TMT 2026-05-11: caso Bedón).
    """
    data = queries.cuenta_corriente(codigo_cli)
    if not data["cliente"]:
        abort(404)

    if request.args.get("export") == "csv":
        return csv_response(
            data["movimientos"],
            columnas=[
                ("fecha",    "Fecha"),
                ("tipo",     "Tipo"),
                ("doc",      "Documento"),
                ("concepto", "Concepto"),
                ("debe",     "Debe"),
                ("haber",    "Haber"),
                ("saldo",    "Saldo"),
            ],
            filename=f"cuenta_{codigo_cli.upper()}.csv",
        )

    return render_template("clientes/cuenta.html", data=data)
