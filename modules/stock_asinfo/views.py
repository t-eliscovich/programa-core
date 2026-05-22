"""/stock/asinfo — vista de stock por producto desde Asinfo.

Blueprint propio (aislado de modules/stock/) montado bajo url_prefix="/stock".
Lee la tabla `saldo_producto` del ERP vía Metabase API. Asinfo NO tiene
costos cargados (confirmado 2026-05-22), así que solo se muestra cantidad
y `precio_ultima_venta` como proxy informativo para el 24% que lo tiene.
"""
from __future__ import annotations

from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso
from exports import csv_response

stock_asinfo_bp = Blueprint(
    "stock_asinfo",
    __name__,
    template_folder="templates",
)


@stock_asinfo_bp.route("/asinfo")
@requiere_login
@requiere_permiso("informes.ver")
def lista():
    """Stock por producto en Asinfo. Read-only, cantidad + precio referencial."""
    try:
        min_saldo = float(request.args.get("min") or 0)
    except (TypeError, ValueError):
        min_saldo = 0.0

    error = None
    rows = []
    try:
        from modules.asinfo import service as asinfo_service
        rows = asinfo_service.stock_asinfo(min_saldo=min_saldo)
    except Exception as e:  # noqa: BLE001
        error = str(e)

    # Búsqueda por código/descripcion (client-side simple)
    q = (request.args.get("q") or "").strip().upper()
    if q:
        rows = [
            r for r in rows
            if q in (r.get("codigo") or "").upper()
            or q in (r.get("descripcion") or "").upper()
        ]

    # Stats
    total_productos = len(rows)
    total_unidades = sum(r["cantidad_total"] for r in rows)
    valor_proxy = sum(
        r["cantidad_total"] * r["precio_ultima"]
        for r in rows if r["precio_ultima"] > 0
    )
    productos_con_precio = sum(1 for r in rows if r["precio_ultima"] > 0)

    if request.args.get("export") == "csv":
        return csv_response(
            rows,
            columnas=[
                ("codigo", "Código"),
                ("descripcion", "Descripción"),
                ("cantidad_total", "Cantidad"),
                ("n_bodegas", "Bodegas"),
                ("precio_ultima", "Precio última venta (US)"),
                ("bodegas_detalle", "Detalle bodegas"),
            ],
            filename="stock_asinfo.csv",
        )

    return render_template(
        "stock_asinfo/lista.html",
        rows=rows,
        total_productos=total_productos,
        total_unidades=total_unidades,
        valor_proxy=valor_proxy,
        productos_con_precio=productos_con_precio,
        q=q,
        min_saldo=min_saldo,
        error=error,
    )
