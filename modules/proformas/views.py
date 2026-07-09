"""Proformas (cotizaciones)."""
from flask import (
    Blueprint,
    abort,
    jsonify,
    render_template,
    request,
)

from auth import requiere_login, requiere_permiso
from exports import csv_response
from filters import today_ec

from . import queries

proformas_bp = Blueprint("proformas", __name__, template_folder="templates")


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


def _render_form(es_pedido: bool, titulo: str):
    """Render compartido de la pantalla campo-a-campo (nueva.html), usada por
    Cotización y por Pedido. Solo para COTIZAR/PEDIR e IMPRIMIR — NO se guarda
    (GET-only, cálculo e impresión 100% client-side).

    Diferencia: en PEDIDO se ingresan los KILOS EXACTOS (sin multiplicar
    piezas), porque ya sale el pedido; en COTIZACIÓN la cantidad se convierte a
    kg por tipo (piezas×22, cuellos÷33, puños÷45, rib directo).
    """
    try:
        from modules.autocomplete.queries import clientes_para_datalist
        clientes_datalist = clientes_para_datalist()
    except Exception:
        clientes_datalist = []

    try:
        matriz = queries.matriz_precios()
    except Exception:
        matriz = {"clases": [], "telas": [], "precios": {}}

    try:
        colores = queries.colores_catalogo()
    except Exception:
        colores = []

    form = {"fecha": today_ec().strftime("%d/%m/%Y")}
    for k in ("codigo_cli", "fecha"):
        if request.args.get(k):
            form[k] = request.args.get(k)
    return render_template(
        "proformas/nueva.html",
        form=form, errores=[], clientes_datalist=clientes_datalist,
        matriz=matriz, colores=colores,
        es_pedido=es_pedido, titulo=titulo,
    )


@proformas_bp.route("/proformas/nueva", methods=["GET"])
@requiere_login
@requiere_permiso("proformas.crear")
def nueva():
    """Cotización nueva (kilos por conversión de cantidad)."""
    return _render_form(es_pedido=False, titulo="Nueva cotización")


@proformas_bp.route("/pedidos/nuevo", methods=["GET"])
@requiere_login
@requiere_permiso("proformas.crear")
def pedido_nuevo():
    """Pedido nuevo — igual que la cotización pero se ingresan los KILOS
    EXACTOS (sin multiplicar), porque ya sale el pedido."""
    return _render_form(es_pedido=True, titulo="Factura Proforma")


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
