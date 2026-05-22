"""/stock/asinfo — vista de stock por producto desde Asinfo.

Blueprint propio (aislado de modules/stock/) montado bajo url_prefix="/stock".
Lee `v_saldo_producto_vista` del ERP vía Metabase API. Asinfo NO tiene
costos cargados (confirmado 2026-05-22), así que solo se muestra cantidad
y `precio_ultima_venta` como proxy informativo para el ~50% que lo tiene.

Filtros disponibles (query string):
    q       — busca en código, nombre y tejido
    tejido  — filtra exactamente por categoría (Jersey / Fleece / Pique / etc.)
    color   — filtra por color hex (#ffffff, #000000, …)
    min     — cantidad mínima
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


@stock_asinfo_bp.route("/quimicos")
@requiere_login
@requiere_permiso("informes.ver")
def quimicos():
    """Stock de químicos desde formulas_app (modules/tintura/service).

    Replica la lógica de stock_al_dia: última lectura de inventario por
    producto, ± ajustes, + compras, − consumo (de órdenes terminadas).
    Es la fuente confiable de químicos — no Asinfo, que solo tiene 55K kg
    (vs ~396K kg reales en formulas_app/PC).
    """
    from datetime import date as _date
    error = None
    rows = []
    fecha_corte = _date.today()
    try:
        from modules.tintura import service as tintura
        rows = tintura.stock_quimicos_al_dia(fecha_corte)
    except Exception as e:  # noqa: BLE001
        error = str(e)

    # Filtros UI
    q = (request.args.get("q") or "").strip().upper()
    familia_filtro = (request.args.get("familia") or "").strip()

    familias_universo = sorted({(r.familia or "") for r in rows if r.familia})

    if familia_filtro:
        rows = [r for r in rows if r.familia == familia_filtro]
    if q:
        rows = [
            r for r in rows
            if q in (r.nombre or "").upper()
            or q in (r.familia or "").upper()
        ]

    # Quedarse solo con los que tienen algo de stock (o que se movieron)
    rows_con_stock = [r for r in rows if abs(r.stock_al_dia_kg) > 0.001]

    total_productos = len(rows_con_stock)
    total_kg = sum(r.stock_al_dia_kg for r in rows_con_stock)
    total_us = sum(r.stock_al_dia_kg * r.precio_us for r in rows_con_stock)

    # Distribución por familia
    por_familia: dict[str, dict] = {}
    for r in rows_con_stock:
        f = r.familia or "(s/familia)"
        slot = por_familia.setdefault(f, {"n": 0, "kg": 0.0, "us": 0.0})
        slot["n"] += 1
        slot["kg"] += r.stock_al_dia_kg
        slot["us"] += r.stock_al_dia_kg * r.precio_us
    distribucion = sorted(por_familia.items(), key=lambda kv: -kv[1]["us"])

    if request.args.get("export") == "csv":
        export_rows = [
            {
                "familia": r.familia,
                "num_visible": r.num_visible,
                "nombre": r.nombre,
                "unidad": r.unidad,
                "stock_kg": round(r.stock_al_dia_kg, 3),
                "precio_us": round(r.precio_us, 4),
                "valor_us": round(r.stock_al_dia_kg * r.precio_us, 2),
                "fecha_lectura": (
                    r.fecha_lectura.isoformat() if r.fecha_lectura else ""
                ),
            }
            for r in rows_con_stock
        ]
        return csv_response(
            export_rows,
            columnas=[
                ("familia", "Familia"),
                ("num_visible", "N°"),
                ("nombre", "Nombre"),
                ("unidad", "Unidad"),
                ("stock_kg", "Stock"),
                ("precio_us", "U$/unidad"),
                ("valor_us", "Valor U$"),
                ("fecha_lectura", "Última lectura"),
            ],
            filename="stock_quimicos.csv",
        )

    return render_template(
        "stock_asinfo/quimicos.html",
        rows=rows_con_stock,
        total_productos=total_productos,
        total_kg=total_kg,
        total_us=total_us,
        distribucion=distribucion,
        familias_universo=familias_universo,
        familia_filtro=familia_filtro,
        q=q,
        fecha_corte=fecha_corte,
        error=error,
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

    q = (request.args.get("q") or "").strip().upper()
    tejido_filtro = (request.args.get("tejido") or "").strip()
    color_filtro = (request.args.get("color") or "").strip().upper()

    error = None
    rows = []
    try:
        from modules.asinfo import service as asinfo_service
        rows = asinfo_service.stock_asinfo(min_saldo=min_saldo)
    except Exception as e:  # noqa: BLE001
        error = str(e)

    # Catálogos para los dropdowns — SE CALCULAN SOBRE TODO EL UNIVERSO,
    # no sobre lo filtrado, para que la dueña vea siempre el mismo set.
    tejidos_universo = sorted({(r.get("tejido") or "") for r in rows if r.get("tejido")})
    # `color` ahora es el código de color extraído del nombre (BLA/NEG/MAR/etc.)
    colores_universo = sorted({(r.get("color") or "") for r in rows if r.get("color")})

    # Aplicar filtros (post-cache, en memoria — son ~3500 filas, irrelevante)
    if tejido_filtro:
        rows = [r for r in rows if r.get("tejido") == tejido_filtro]
    if color_filtro:
        rows = [r for r in rows if (r.get("color") or "").upper() == color_filtro]
    if q:
        rows = [
            r for r in rows
            if q in (r.get("codigo") or "").upper()
            or q in (r.get("nombre") or "").upper()
            or q in (r.get("tejido") or "").upper()
            or q in (r.get("subcategoria") or "").upper()
        ]

    # Stats
    total_productos = len(rows)
    total_unidades = sum(r["cantidad_total"] for r in rows)
    valor_proxy = sum(
        r["cantidad_total"] * r["precio_ultima"]
        for r in rows if r["precio_ultima"] > 0
    )
    productos_con_precio = sum(1 for r in rows if r["precio_ultima"] > 0)

    # Distribución por tejido (siempre sobre lo filtrado, para que ayude
    # a explorar): label → {n, kg}
    por_tejido: dict[str, dict] = {}
    for r in rows:
        t = r.get("tejido") or "(s/categoría)"
        slot = por_tejido.setdefault(t, {"n": 0, "kg": 0.0})
        slot["n"] += 1
        slot["kg"] += r["cantidad_total"]
    distribucion = sorted(por_tejido.items(), key=lambda kv: -kv[1]["kg"])

    if request.args.get("export") == "csv":
        return csv_response(
            rows,
            columnas=[
                ("codigo", "Código"),
                ("nombre", "Nombre"),
                ("tejido", "Tejido"),
                ("subcategoria", "Subcategoría"),
                ("color", "Color (hex)"),
                ("cantidad_total", "Cantidad"),
                ("n_bodegas", "Bodegas"),
                ("precio_ultima", "Precio última venta (US)"),
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
        tejido_filtro=tejido_filtro,
        color_filtro=color_filtro,
        tejidos_universo=tejidos_universo,
        colores_universo=colores_universo,
        distribucion=distribucion,
        error=error,
    )
