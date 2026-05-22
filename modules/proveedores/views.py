"""Listado y CRUD de proveedores."""
from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

import db
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


@proveedores_bp.route("/proveedores/<int:id_proveedor>/eliminar", methods=["POST"])
@requiere_login
@requiere_permiso("proveedores.editar")
def eliminar(id_proveedor: int):
    """Borra un proveedor con confirmación por PK.

    TMT 2026-05-20 v2 — pedido dueña: rows legacy sin codigo_prov
    también deben ser eliminables. Usamos id_proveedor (PK) para
    que NUNCA falle por URL malformada.
    """
    fila = db.fetch_one(
        "SELECT codigo_prov, nombre FROM scintela.proveedor WHERE id_proveedor = %s",
        (int(id_proveedor),),
    ) or {}
    if not fila:
        abort(404)
    label = (fila.get("codigo_prov") or "(sin código)") + " — " + (fila.get("nombre") or "(sin nombre)")
    try:
        queries.eliminar_por_id(int(id_proveedor))
        flash(f"Proveedor {label} eliminado.", "ok")
    except ValueError as e:
        flash(str(e), "warn")
    except Exception as e:
        flash_exc("No pude eliminar", e)
    return redirect(url_for("proveedores.lista"))


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


@proveedores_bp.route("/proveedores/_api/<int:id_proveedor>/tipo", methods=["POST"])
@requiere_login
@requiere_permiso("proveedores.editar")
def api_editar_tipo(id_proveedor: int):
    """Inline edit del campo `tipo` desde /proveedores.

    TMT 2026-05-20 — pedido dueña: el tipo del proveedor define qué
    workflow puede consumirlo (U=máquinas, H=hilado, Q=químicos, Y=otro).
    Acepta JSON `{tipo: 'U'|'H'|'Q'|'Y'|''}`. Vacío = limpiar.
    """
    import db as _db
    data = request.get_json(silent=True) or request.form
    tipo_nuevo = (data.get("tipo") or "").strip().upper()[:1]
    # Empty string lo dejamos pasar para "limpiar tipo".
    try:
        usuario = (g.user or {}).get("username", "web")
        n = _db.execute(
            """
            UPDATE scintela.proveedor
               SET tipo = NULLIF(%s, ''),
                   usuario_modifica = %s,
                   fecha_modifica = CURRENT_TIMESTAMP
             WHERE id_proveedor = %s
            """,
            (tipo_nuevo, usuario[:50], id_proveedor),
        )
        if not n:
            return jsonify({"ok": False, "error": "Proveedor no existe."}), 404
        return jsonify({"ok": True, "tipo": tipo_nuevo or None})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"No pude guardar: {e}"}), 500


@proveedores_bp.route("/proveedores")
@requiere_login
@requiere_permiso("proveedores.ver")
def lista():
    q = request.args.get("q", "").strip()
    # Federico 2026-05-22 — filtro por tipo de proveedor.
    #   ''    -> Todos (sin filtro)
    #   'SIN' -> proveedores sin tipo cargado
    #   letra -> ese tipo (U/H/Q/B/Y/K)
    tipo_sel = (request.args.get("tipo") or "").strip().upper()
    if tipo_sel == "":
        tipo_filtro = None
    elif tipo_sel == "SIN":
        tipo_filtro = ""
    else:
        tipo_filtro = tipo_sel
    # TMT 2026-05-20 v2 — paginación pedido dueña.
    try:
        pag = max(1, int(request.args.get("pag") or 1))
    except (TypeError, ValueError):
        pag = 1
    POR_PAG = 200
    offset = (pag - 1) * POR_PAG
    try:
        filas = queries.buscar(q, tipo=tipo_filtro, limite=POR_PAG, offset=offset)
        total = queries.contar(q, tipo=tipo_filtro)
        error = None
    except Exception as e:
        filas, total, error = [], 0, str(e)
    total_pag = max(1, (total + POR_PAG - 1) // POR_PAG)

    if request.args.get("export") == "csv":
        try:
            todos = queries.buscar(q, tipo=tipo_filtro, limite=100000, offset=0)
        except Exception:
            todos = filas
        return csv_response(
            todos,
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

    return render_template(
        "proveedores/lista.html",
        filas=filas, q=q, tipo_sel=tipo_sel, error=error,
        pag=pag, total_pag=total_pag, total=total, por_pag=POR_PAG,
    )
