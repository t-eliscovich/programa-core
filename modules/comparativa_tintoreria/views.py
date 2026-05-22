"""/informes/comparativa-tintoreria — cruce kg/día PC vs formulas_app.

Blueprint propio (no toca modules/informes/views.py) — montado bajo
url_prefix="/informes" para que la URL canónica sea /informes/comparativa-tintoreria.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso
from exports import csv_response

from . import queries

comparativa_tintoreria_bp = Blueprint(
    "comparativa_tintoreria",
    __name__,
    template_folder="templates",
)


def _parse_date(s: str | None, default: date) -> date:
    if not s:
        return default
    try:
        return date.fromisoformat(s.strip())
    except (ValueError, AttributeError):
        return default


@comparativa_tintoreria_bp.route("/tintoreria")
@requiere_login
@requiere_permiso("informes.ver")
def tintoreria_detalle():
    """Tabla plana: fecha terminado, color, kg, importe, precio unitario.

    Fuente: `scintela.tinto` agrupado por (fecha, cod). Una fila por
    combinación (fecha, color). Precio unitario = importe / kg.
    """
    hoy = date.today()
    default_desde = hoy - timedelta(days=30)
    desde = _parse_date(request.args.get("desde"), default_desde)
    hasta = _parse_date(request.args.get("hasta"), hoy)
    cod_filtro = (request.args.get("cod") or "").strip().upper()

    error = None
    rows: list[dict] = []
    try:
        rows = queries.tinto_pc_por_dia_color(desde, hasta) or []
    except Exception as e:  # noqa: BLE001
        error = str(e)

    # Universo de colores para el dropdown (sobre todo lo traído)
    cods_universo = sorted({r.get("cod") or "" for r in rows if r.get("cod")})

    # Filtro por color
    if cod_filtro:
        rows = [r for r in rows if (r.get("cod") or "").upper() == cod_filtro]

    # Calcular precio unitario y armar filas finales
    filas = []
    tot_kg = 0.0
    tot_importe = 0.0
    for r in rows:
        kg = float(r.get("kg") or 0)
        imp = float(r.get("importe") or 0)
        precio = (imp / kg) if kg else None
        tot_kg += kg
        tot_importe += imp
        filas.append({
            "fecha": r["fecha"],
            "cod": r.get("cod") or "",
            "kg": kg,
            "importe": imp,
            "precio_unitario": precio,
            "n_lineas": int(r.get("n_lineas") or 0),
        })
    precio_promedio = (tot_importe / tot_kg) if tot_kg else None

    if request.args.get("export") == "csv":
        return csv_response(
            [
                {
                    "fecha": f["fecha"].isoformat(),
                    "color": f["cod"],
                    "kg": f["kg"],
                    "importe": f["importe"],
                    "precio_unitario": (
                        f"{f['precio_unitario']:.4f}"
                        if f["precio_unitario"] is not None else ""
                    ),
                    "n_lineas": f["n_lineas"],
                }
                for f in filas
            ],
            columnas=[
                ("fecha", "Fecha"),
                ("color", "Color"),
                ("kg", "Kg"),
                ("importe", "Importe (US)"),
                ("precio_unitario", "Precio unitario (US/kg)"),
                ("n_lineas", "Líneas"),
            ],
            filename=f"tintoreria_{desde}_{hasta}.csv",
        )

    return render_template(
        "comparativa_tintoreria/tintoreria.html",
        filas=filas,
        desde=desde,
        hasta=hasta,
        cod_filtro=cod_filtro,
        cods_universo=cods_universo,
        tot_kg=tot_kg,
        tot_importe=tot_importe,
        precio_promedio=precio_promedio,
        error=error,
    )


@comparativa_tintoreria_bp.route("/comparativa-tintoreria")
@requiere_login
@requiere_permiso("informes.ver")
def comparativa_tintoreria():
    """Vista comparativa: kg tinturados por día — PC (scintela.tinto) vs
    formulas_app (ordenes.tela_terminada_kg).

    Cruce solo por fecha — `scintela.tinto` NO guarda el número de OT que
    permitiría matchear a nivel orden. El detalle por color se muestra
    como filas hijas de cada día.
    """
    hoy = date.today()
    default_desde = hoy - timedelta(days=14)
    desde = _parse_date(request.args.get("desde"), default_desde)
    hasta = _parse_date(request.args.get("hasta"), hoy)

    error = None
    rows_pc: list[dict] = []
    rows_pc_color: list[dict] = []
    rows_form: list = []

    try:
        rows_pc = queries.tinto_pc_por_dia(desde, hasta)
    except Exception as e:  # noqa: BLE001
        error = f"PC: {e}"

    try:
        rows_pc_color = queries.tinto_pc_por_dia_color(desde, hasta)
    except Exception as e:  # noqa: BLE001
        error = (error + " | " if error else "") + f"PC color: {e}"

    try:
        from modules.tintura import service as tintura_service
        rows_form = tintura_service.tinturado_resumen(
            limite=5000,
            terminado_desde=desde,
            terminado_hasta=hasta,
        )
    except Exception as e:  # noqa: BLE001
        error = (error + " | " if error else "") + f"formulas_app: {e}"

    # Indexar form por fecha_terminado
    form_por_fecha: dict[date, list] = defaultdict(list)
    for o in rows_form:
        if o.fecha_terminado:
            form_por_fecha[o.fecha_terminado].append(o)

    # Indexar PC color por fecha
    pc_color_por_fecha: dict[date, list[dict]] = defaultdict(list)
    for r in rows_pc_color:
        pc_color_por_fecha[r["fecha"]].append(r)

    # Construir filas comparativas — usa la unión de fechas de ambas fuentes.
    fechas = sorted(
        set([r["fecha"] for r in rows_pc] + list(form_por_fecha.keys())),
        reverse=True,
    )

    filas = []
    tot_pc_kg = 0.0
    tot_form_kg = 0.0
    tot_pc_us = 0.0
    tot_form_costo = 0.0
    pc_index = {r["fecha"]: r for r in rows_pc}

    for f in fechas:
        pc_row = pc_index.get(f)
        form_ots = form_por_fecha.get(f, [])
        pc_kg = float(pc_row["kg"]) if pc_row else 0.0
        pc_us = float(pc_row["importe"]) if pc_row else 0.0
        form_kg = sum(
            float(o.tela_terminada_kg or 0) for o in form_ots
        )
        # Costo desde formulas_app: tomamos kilos_planeados * 0 (no hay precio
        # cargado en TinturadoOrden). Por ahora dejamos costo None; cuando se
        # agregue un cálculo de costo por receta, llenamos acá.
        # Como aproximación de "costo PC": importe / kg promedio del día.
        diff_kg = round(pc_kg - form_kg, 3)
        diff_pct = (diff_kg / pc_kg * 100.0) if pc_kg else None
        tot_pc_kg += pc_kg
        tot_form_kg += form_kg
        tot_pc_us += pc_us

        # Estado: tolerancia ±15 kg o ±2%
        if not pc_row and form_ots:
            estado = "solo_form"
        elif pc_row and not form_ots:
            estado = "solo_pc"
        elif abs(diff_kg) <= 15 or (diff_pct is not None and abs(diff_pct) <= 2):
            estado = "ok"
        else:
            estado = "discrepancia"

        # Detalle por color (solo lado PC; formulas_app tiene color por OT)
        detalle_color = pc_color_por_fecha.get(f, [])
        # Detalle por OT (lado formulas_app)
        detalle_ots = [o.to_dict() for o in form_ots]

        filas.append({
            "fecha": f,
            "pc_kg": pc_kg,
            "pc_us": pc_us,
            "form_kg": form_kg,
            "form_ots": len(form_ots),
            "diff_kg": diff_kg,
            "diff_pct": diff_pct,
            "estado": estado,
            "detalle_color": detalle_color,
            "detalle_ots": detalle_ots,
        })

    # CSV export
    if request.args.get("export") == "csv":
        csv_rows = []
        for fila in filas:
            csv_rows.append({
                "fecha": fila["fecha"].isoformat(),
                "pc_kg": fila["pc_kg"],
                "form_kg": fila["form_kg"],
                "diff_kg": fila["diff_kg"],
                "diff_pct": (
                    f"{fila['diff_pct']:.2f}" if fila["diff_pct"] is not None else ""
                ),
                "pc_importe_us": fila["pc_us"],
                "form_ots": fila["form_ots"],
                "estado": fila["estado"],
            })
        return csv_response(
            csv_rows,
            columnas=[
                ("fecha", "Fecha"),
                ("pc_kg", "PC kg"),
                ("form_kg", "Form kg"),
                ("diff_kg", "Δ kg"),
                ("diff_pct", "Δ %"),
                ("pc_importe_us", "Importe PC (US)"),
                ("form_ots", "OTs form"),
                ("estado", "Estado"),
            ],
            filename=f"comparativa_tintoreria_{desde}_{hasta}.csv",
        )

    return render_template(
        "comparativa_tintoreria/index.html",
        filas=filas,
        desde=desde,
        hasta=hasta,
        tot_pc_kg=tot_pc_kg,
        tot_form_kg=tot_form_kg,
        tot_pc_us=tot_pc_us,
        tot_diff_kg=round(tot_pc_kg - tot_form_kg, 3),
        tot_diff_pct=(
            (tot_pc_kg - tot_form_kg) / tot_pc_kg * 100.0
            if tot_pc_kg else None
        ),
        error=error,
    )
