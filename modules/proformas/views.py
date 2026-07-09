"""Proformas (cotizaciones)."""
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

from auth import requiere_login, requiere_permiso
from error_messages import flash_exc
from exports import csv_response
from filters import today_ec
from parsers import parse_monto

from . import queries

proformas_bp = Blueprint("proformas", __name__, template_folder="templates")


def _parse_fecha(s: str | None):
    """DD/MM/AAAA (lo que tipea la contadora) → date. None si no parsea."""
    from datetime import datetime
    s = (s or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


@proformas_bp.route("/proformas")
@requiere_login
@requiere_permiso("proformas.ver")
def lista():
    q = request.args.get("q", "").strip()
    desde = request.args.get("desde") or None
    hasta = request.args.get("hasta") or None
    try:
        filas = queries.buscar(q, desde, hasta)
        error = None
    except Exception as e:
        filas, error = [], str(e)

    if request.args.get("export") == "csv":
        return csv_response(
            filas,
            columnas=[
                ("fecha_emision", "Fecha"),
                ("id_proforma", "N° proforma"),
                ("codigo_cli", "Cliente"),
                ("cliente", "Nombre"),
                ("subtotal", "Subtotal"),
                ("monto_descuento_volumen", "Desc. vol."),
                ("subtotal_con_descuento", "Subtotal c/desc"),
                ("monto_descuento_contado", "Desc. contado"),
                ("total_final", "Total"),
            ],
            filename="proformas.csv",
        )

    total = sum(float(r["total_final"] or 0) for r in filas)
    return render_template(
        "proformas/lista.html",
        filas=filas, q=q, desde=desde, hasta=hasta, total=total, error=error,
    )


@proformas_bp.route("/proformas/nueva", methods=["GET", "POST"])
@requiere_login
@requiere_permiso("proformas.crear")
def nueva():
    """Cotización nueva — flujo campo-a-campo del dBase (FACTURAR.PRG), pero
    solo para cotizar (no factura, no stock, no contabilidad).

    Cada línea: Tipo (tela) + Clase de color → precio de lista sugerido
    (editable) → Kg → Importe = Kg×Precio. Al final descuento por volumen y
    por contado en cascada, igual que PROCEDURE FACTURO.
    """
    # Datalist de clientes (mismo dropdown que Nueva factura/cobranza).
    try:
        from modules.autocomplete.queries import clientes_para_datalist
        clientes_datalist = clientes_para_datalist()
    except Exception:
        clientes_datalist = []

    try:
        matriz = queries.matriz_precios()
    except Exception:
        matriz = {"clases": [], "telas": [], "precios": {}}

    if request.method == "GET":
        form = {"fecha": today_ec().strftime("%d/%m/%Y")}
        for k in ("codigo_cli", "fecha"):
            if request.args.get(k):
                form[k] = request.args.get(k)
        return render_template(
            "proformas/nueva.html",
            form=form, errores=[], clientes_datalist=clientes_datalist,
            matriz=matriz,
        )

    # POST — armar líneas desde los arrays paralelos del form.
    errores: list[str] = []
    codigo_cli = (request.form.get("codigo_cli") or "").strip().upper()
    fecha = _parse_fecha(request.form.get("fecha")) or today_ec()

    telas = request.form.getlist("linea_tela")
    nombres = request.form.getlist("linea_nombre")
    colores = request.form.getlist("linea_color")
    clases = request.form.getlist("linea_clase")
    kgs = request.form.getlist("linea_kg")
    precios = request.form.getlist("linea_precio")

    lineas: list[dict] = []
    for i in range(len(telas)):
        kg = parse_monto(kgs[i] if i < len(kgs) else "")
        pu = parse_monto(precios[i] if i < len(precios) else "")
        if (kg or 0) == 0 and (pu or 0) == 0:
            continue
        lineas.append({
            "tela": telas[i],
            "nombre_producto": nombres[i] if i < len(nombres) else "",
            "color": colores[i] if i < len(colores) else "",
            "clase": clases[i] if i < len(clases) else None,
            "cantidad_kilos": kg or 0,
            "precio_unitario": pu or 0,
        })

    pct_vol = parse_monto(request.form.get("descuento_volumen")) or 0
    aplica_contado = bool(request.form.get("aplica_contado"))
    pct_contado = parse_monto(request.form.get("descuento_contado"))
    if pct_contado is None:
        pct_contado = 5.0
    observaciones = (request.form.get("observaciones") or "").strip()

    if not codigo_cli:
        errores.append("Elegí un cliente.")
    elif not queries.cliente_defaults(codigo_cli):
        errores.append(f"El cliente {codigo_cli} no existe.")
    if not lineas:
        errores.append("Agregá al menos una línea con Kg y precio.")

    if errores:
        form = {
            "codigo_cli": codigo_cli,
            "fecha": request.form.get("fecha") or today_ec().strftime("%d/%m/%Y"),
            "observaciones": observaciones,
        }
        return render_template(
            "proformas/nueva.html",
            form=form, errores=errores, clientes_datalist=clientes_datalist,
            matriz=matriz,
        ), 400

    try:
        usuario = (g.user or {}).get("username", "web")
        creada = queries.crear(
            codigo_cli=codigo_cli, fecha=fecha, lineas=lineas,
            pct_volumen=float(pct_vol), aplica_contado=aplica_contado,
            pct_contado=float(pct_contado), observaciones=observaciones,
            usuario=usuario,
        )
        flash(f"Cotización N° {creada['id_proforma']} creada.", "ok")
        return redirect(url_for("proformas.detalle",
                                id_proforma=creada["id_proforma"]))
    except Exception as e:  # noqa: BLE001
        flash_exc("No pude guardar la cotización", e)
        form = {
            "codigo_cli": codigo_cli,
            "fecha": request.form.get("fecha"),
            "observaciones": observaciones,
        }
        return render_template(
            "proformas/nueva.html",
            form=form, errores=[str(e)], clientes_datalist=clientes_datalist,
            matriz=matriz,
        ), 500


@proformas_bp.route("/proformas/cliente-defaults")
@requiere_login
@requiere_permiso("proformas.crear")
def cliente_defaults_api():
    """Prefill de descuentos al elegir cliente (JSON)."""
    data = queries.cliente_defaults(request.args.get("codigo_cli", ""))
    if not data:
        return jsonify({"ok": False}), 404
    return jsonify({"ok": True, **data})


@proformas_bp.route("/proformas/<int:id_proforma>")
@requiere_login
@requiere_permiso("proformas.ver")
def detalle(id_proforma: int):
    data = queries.detalle(id_proforma)
    if not data:
        abort(404)
    return render_template("proformas/detalle.html", **data)
