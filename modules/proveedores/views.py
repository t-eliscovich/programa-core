"""Listado y CRUD de proveedores."""
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
from parsers import parse_int, parse_monto

from . import queries

proveedores_bp = Blueprint("proveedores", __name__, template_folder="templates")


def _form_from_request() -> dict:
    return {
        "codigo_prov": (request.form.get("codigo_prov") or "").strip().upper(),
        "nombre": (request.form.get("nombre") or "").strip(),
        "ruc": (request.form.get("ruc") or "").strip(),
        "telefono": (request.form.get("telefono") or "").strip(),
        "representante": (request.form.get("representante") or "").strip(),
        "tipo": (request.form.get("tipo") or "").strip().upper(),
        "plazo": request.form.get("plazo") or "",
        "retbase": request.form.get("retbase") or "",
        "retiva": request.form.get("retiva") or "",
        "direccion": (request.form.get("direccion") or "").strip(),
        "correo": (request.form.get("correo") or "").strip(),
        "activo": (request.form.get("activo") or "").strip(),
    }


@proveedores_bp.route("/proveedores/nuevo", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("proveedores.crear")
def nuevo():
    """Alta de proveedor.

    Acepta `?codigo=XYZ&next=/compras/nueva?…` para el flujo "venía de compras
    y el proveedor no existía" — pre-llena el código, y después de crear
    redirige al `next` URL para retomar la operación que disparó la creación.
    TMT 2026-05-13.
    """
    errores: list[str] = []
    next_url = request.values.get("next") or ""
    codigo_prefill = (request.args.get("codigo") or "").strip().upper()

    if request.method == "GET":
        form = {"codigo_prov": codigo_prefill} if codigo_prefill else {}
        return render_template(
            "proveedores/form.html",
            form=form, errores=errores, modo="crear",
            next_url=next_url,
        )

    form = _form_from_request()
    plazo = parse_int(form["plazo"])
    retbase = parse_monto(form["retbase"])
    retiva = parse_monto(form["retiva"])

    if not form["codigo_prov"]:
        errores.append("Código requerido.")
    if not form["nombre"]:
        errores.append("Nombre requerido.")

    if errores:
        return (
            render_template(
                "proveedores/form.html", form=form, errores=errores,
                modo="crear", next_url=next_url,
            ),
            400,
        )

    try:
        usuario = (g.user or {}).get("username", "web")
        queries.crear(
            codigo_prov=form["codigo_prov"], nombre=form["nombre"],
            ruc=form["ruc"] or None, telefono=form["telefono"] or None,
            representante=form["representante"] or None,
            tipo=form["tipo"] or None, plazo=plazo,
            retbase=retbase, retiva=retiva,
            direccion=form["direccion"] or None, correo=form["correo"] or None,
            usuario=usuario,
        )
        flash(f"Proveedor {form['codigo_prov']} creado.", "ok")
        # Si venía de otra pantalla (?next=/compras/nueva…), volver allá.
        if next_url and next_url.startswith("/"):
            return redirect(next_url)
        return redirect(url_for("proveedores.lista"))
    except ValueError as e:
        errores.append(str(e))
        return (
            render_template(
                "proveedores/form.html", form=form, errores=errores,
                modo="crear", next_url=next_url,
            ),
            400,
        )
    except Exception as e:
        errores.append(f"No pude crear: {e}")
        return (
            render_template(
                "proveedores/form.html", form=form, errores=errores,
                modo="crear", next_url=next_url,
            ),
            500,
        )


@proveedores_bp.route("/proveedores/<codigo_prov>/editar", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("proveedores.editar")
def editar(codigo_prov: str):
    prov = queries.por_codigo(codigo_prov)
    if not prov:
        abort(404)
    errores: list[str] = []

    if request.method == "GET":
        try:
            from modules.recientes import queries as rec
            rec.registrar(
                "proveedor", prov["codigo_prov"],
                etiqueta=f"{prov['codigo_prov']} — {prov.get('nombre') or ''}",
            )
        except Exception:
            pass
        form = {
            "codigo_prov": prov["codigo_prov"],
            "nombre": prov.get("nombre") or "",
            "ruc": prov.get("ruc") or "",
            "telefono": prov.get("telefono") or "",
            "representante": prov.get("representante") or "",
            "tipo": prov.get("tipo") or "",
            "plazo": prov.get("plazo") or "",
            "retbase": prov.get("retbase") or "",
            "retiva": prov.get("retiva") or "",
            "direccion": prov.get("direccion") or "",
            "correo": prov.get("correo") or "",
            "activo": prov.get("activo") or "1",
        }
        return render_template(
            "proveedores/form.html", form=form, errores=errores, modo="editar"
        )

    form = _form_from_request()
    plazo = parse_int(form["plazo"])
    retbase = parse_monto(form["retbase"])
    retiva = parse_monto(form["retiva"])

    if not form["nombre"]:
        errores.append("Nombre requerido.")
    if errores:
        form["codigo_prov"] = prov["codigo_prov"]
        return (
            render_template(
                "proveedores/form.html", form=form, errores=errores, modo="editar"
            ),
            400,
        )

    try:
        usuario = (g.user or {}).get("username", "web")
        queries.editar(
            prov["codigo_prov"],
            nombre=form["nombre"], ruc=form["ruc"] or None,
            telefono=form["telefono"] or None,
            representante=form["representante"] or None,
            tipo=form["tipo"] or None, plazo=plazo,
            retbase=retbase, retiva=retiva,
            direccion=form["direccion"] or None,
            correo=form["correo"] or None,
            activo=form["activo"] or None,
            usuario=usuario,
        )
        flash(f"Proveedor {prov['codigo_prov']} actualizado.", "ok")
        return redirect(url_for("proveedores.lista"))
    except Exception as e:
        errores.append(f"No pude actualizar: {e}")
        return (
            render_template(
                "proveedores/form.html", form=form, errores=errores, modo="editar"
            ),
            500,
        )


@proveedores_bp.route("/proveedores/<codigo_prov>/activo", methods=["POST"])
@requiere_login
@requiere_permiso("proveedores.editar")
def toggle_activo(codigo_prov: str):
    prov = queries.por_codigo(codigo_prov)
    if not prov:
        abort(404)
    set_activo = (request.form.get("set") or "").strip() == "1"
    try:
        usuario = (g.user or {}).get("username", "web")
        queries.set_activo(codigo_prov, set_activo, usuario=usuario)
        flash(
            f"Proveedor {codigo_prov} — {'ACTIVADO' if set_activo else 'DESACTIVADO'}.",
            "ok",
        )
    except Exception as e:
        flash_exc("No pude cambiar estado", e)
    return redirect(url_for("proveedores.lista"))


@proveedores_bp.route("/proveedores")
@requiere_login
@requiere_permiso("proveedores.ver")
def lista():
    q = request.args.get("q", "").strip()
    try:
        filas = queries.buscar(q)
        error = None
    except Exception as e:
        filas, error = [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("codigo_prov", "Código"),
                ("nombre", "Proveedor"),
                ("ruc", "RUC"),
                ("telefono", "Teléfono"),
                ("representante", "Representante"),
                ("plazo", "Plazo"),
                ("retbase", "Ret. base"),
                ("retiva", "Ret. IVA"),
                ("saldo_total", "Deuda"),
            ],
            filename="proveedores.csv",
        )

    return render_template("proveedores/lista.html", filas=filas, q=q, error=error)
