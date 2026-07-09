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


@proformas_bp.route("/proformas/nueva", methods=["GET"])
@requiere_login
@requiere_permiso("proformas.crear")
def nueva():
    """Cotización nueva — flujo campo-a-campo del dBase (FACTURAR.PRG), pero
    solo para COTIZAR e IMPRIMIR. NO se guarda (dueña 2026-07-09: "no las
    guardes, solo botón para imprimir"). Por eso es GET-only y no hay POST:
    el cálculo y la impresión son 100% client-side (nueva.html).

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
    )


@proformas_bp.route("/proformas/_debug-tinto")
@requiere_login
@requiere_permiso("proformas.crear")
def _debug_tinto():
    """TEMPORAL: estado de scintela.tinto_costos.clase."""
    from flask import jsonify
    import db as _db
    out = {}
    try:
        out["col_clase_existe"] = bool(_db.fetch_one(
            "SELECT 1 x FROM information_schema.columns "
            "WHERE table_schema='scintela' AND table_name='tinto_costos' "
            "AND column_name='clase'"))
    except Exception as e:  # noqa: BLE001
        out["col_err"] = str(e)
    try:
        out["total"] = _db.fetch_one("SELECT COUNT(*) n FROM scintela.tinto_costos")
        out["con_clase"] = _db.fetch_one(
            "SELECT COUNT(*) n FROM scintela.tinto_costos WHERE clase BETWEEN 1 AND 5")
        out["por_clase"] = _db.fetch_all(
            "SELECT clase, COUNT(*) n FROM scintela.tinto_costos GROUP BY clase ORDER BY clase")
        out["muestra"] = _db.fetch_all(
            "SELECT cod, color, clase FROM scintela.tinto_costos "
            "WHERE cod IN ('BLA','NEG','JAS','AGU','AVE')")
    except Exception as e:  # noqa: BLE001
        out["err"] = str(e)
    try:
        out["catalogo_len"] = len(queries.colores_catalogo())
    except Exception as e:  # noqa: BLE001
        out["cat_err"] = str(e)
    return jsonify(out)


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
