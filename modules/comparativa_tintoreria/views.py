"""/informes/comparativa-tintoreria — cruce kg/día PC vs formulas_app.

Blueprint propio (no toca modules/informes/views.py) — montado bajo
url_prefix="/informes" para que la URL canónica sea /informes/comparativa-tintoreria.
"""
from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import date, timedelta

from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso
from exports import csv_response
from filters import today_ec

from . import queries

_LOG = logging.getLogger("programa_core.tintoreria")

comparativa_tintoreria_bp = Blueprint(
    "comparativa_tintoreria",
    __name__,
    template_folder="templates",
)


def _build_tintoreria_mensual(anio: int, mes: int, n_meses: int = 12) -> dict | None:
    """Devuelve la estructura {filas, promedio, total, limite_bajos} con los
    últimos `n_meses` meses hasta (incluyendo) anio/mes. None si falla.
    Se usa para inyectar en el template de /informes/flujo-produccion.
    """
    hasta = date(anio, mes, calendar.monthrange(anio, mes)[1])
    # `n_meses` meses atrás (incluye el mes actual).
    desde_mm = mes - (n_meses - 1)
    desde_yy = anio
    while desde_mm < 1:
        desde_mm += 12
        desde_yy -= 1
    desde = date(desde_yy, desde_mm, 1)

    raw = queries.tinto_bajos_fuertes_por_mes(desde, hasta) or []
    try:
        gp = queries.gs_produccion_tintoreria_por_mes(desde, hasta) or {}
    except Exception:
        gp = {}

    meses_dict: dict[tuple, dict] = {}
    for r in raw:
        k = (int(r["yy"]), int(r["mm"]))
        slot = meses_dict.setdefault(k, {"Bajos": {"kg": 0.0, "imp": 0.0},
                                          "Fuertes": {"kg": 0.0, "imp": 0.0}})
        slot[r["tipo"]]["kg"] = float(r["kg"] or 0)
        slot[r["tipo"]]["imp"] = float(r["importe"] or 0)

    def _calc(b_kg, b_imp, f_kg, f_imp, gp_us=0.0):
        tot_kg = b_kg + f_kg
        tot_imp = b_imp + f_imp
        return {
            "b_pct": (b_kg / tot_kg * 100.0) if tot_kg else 0.0,
            "b_kg": b_kg,
            "b_ukg": (b_imp / b_kg) if b_kg else None,
            "b_imp": b_imp,
            "f_pct": (f_kg / tot_kg * 100.0) if tot_kg else 0.0,
            "f_kg": f_kg,
            "f_ukg": (f_imp / f_kg) if f_kg else None,
            "f_imp": f_imp,
            "t_kg": tot_kg,
            "t_ukg": (tot_imp / tot_kg) if tot_kg else None,
            "t_imp": tot_imp,
            # Gs. Producción Tintorería — comparten kg con Total
            "gp_kg": tot_kg,
            "gp_ukg": (gp_us / tot_kg) if tot_kg else None,
            "gp_imp": gp_us,
        }

    filas = []
    tb_kg = tb_imp = tf_kg = tf_imp = tgp = 0.0
    for (yy, mm), slot in sorted(meses_dict.items()):
        b_kg, b_imp = slot["Bajos"]["kg"], slot["Bajos"]["imp"]
        f_kg, f_imp = slot["Fuertes"]["kg"], slot["Fuertes"]["imp"]
        gp_us = float(gp.get((yy, mm), 0.0))
        tb_kg += b_kg; tb_imp += b_imp
        tf_kg += f_kg; tf_imp += f_imp
        tgp += gp_us
        row = {"yy": yy, "mm": mm, "label": f"{mm:02d}/{yy}"}
        row.update(_calc(b_kg, b_imp, f_kg, f_imp, gp_us))
        filas.append(row)

    n = len(filas) or 1
    promedio = {"label": "PROMEDIO"}
    promedio.update(_calc(tb_kg / n, tb_imp / n, tf_kg / n, tf_imp / n, tgp / n))
    total = {"label": "TOTAL"}
    total.update(_calc(tb_kg, tb_imp, tf_kg, tf_imp, tgp))

    return {
        "filas": filas,
        "promedio": promedio,
        "total": total,
        "desde": desde,
        "hasta": hasta,
    }


@comparativa_tintoreria_bp.app_context_processor
def _inject_tintoreria_mensual():
    """Inyecta `tintoreria_mensual` solo cuando se renderiza el template de
    /informes/flujo-produccion. Lo demás se ignora (devuelve {}).

    Esto permite agregar la tabla mensual al final de flujo_produccion.html
    sin tocar modules/informes/views.py (que Federico edita en paralelo).
    """
    if request.endpoint != "informes.flujo_produccion":
        return {}
    try:
        hoy = today_ec()
        anio = int(request.args.get("anio") or hoy.year)
        mes = max(1, min(12, int(request.args.get("mes") or hoy.month)))
        data = _build_tintoreria_mensual(anio, mes, n_meses=12)
        return {"tintoreria_mensual": data}
    except Exception as e:  # noqa: BLE001
        _LOG.warning("inject_tintoreria_mensual falló: %s", e)
        return {}


def _parse_date(s: str | None, default: date) -> date:
    if not s:
        return default
    try:
        return date.fromisoformat(s.strip())
    except (ValueError, AttributeError):
        return default


@comparativa_tintoreria_bp.route("/tintoreria")
@requiere_login
@requiere_permiso("tintura.ver")
def tintoreria_detalle():
    """Resumen mensual de tintorería: Bajos vs Fuertes vs Total.

    Regla: $/kg <= 0.4 → Bajos ; > 0.4 → Fuertes (mismo límite que
    `_LIM_TINT` en informes/queries.py — replica TINT.BAT).

    Por cada mes muestra para Bajos / Fuertes / Total:
        % kg sobre el total del mes, kg, $/kg promedio, $
    Y al final una fila PROMEDIO mensual.
    """
    hoy = today_ec()
    # Default: últimos 12 meses completos hasta hoy.
    default_desde = (hoy.replace(day=1) - timedelta(days=365)).replace(day=1)
    desde = _parse_date(request.args.get("desde"), default_desde)
    hasta = _parse_date(request.args.get("hasta"), hoy)

    error = None
    raw: list[dict] = []
    try:
        raw = queries.tinto_bajos_fuertes_por_mes(desde, hasta) or []
    except Exception as e:  # noqa: BLE001
        error = str(e)

    # Pivotear: por (yy, mm), Bajos y Fuertes.
    meses: dict[tuple, dict] = {}
    for r in raw:
        k = (int(r["yy"]), int(r["mm"]))
        slot = meses.setdefault(k, {"Bajos": {"kg": 0.0, "imp": 0.0},
                                     "Fuertes": {"kg": 0.0, "imp": 0.0}})
        slot[r["tipo"]]["kg"] = float(r["kg"] or 0)
        slot[r["tipo"]]["imp"] = float(r["importe"] or 0)

    def _calc(b_kg, b_imp, f_kg, f_imp):
        tot_kg = b_kg + f_kg
        tot_imp = b_imp + f_imp
        bp = (b_kg / tot_kg * 100.0) if tot_kg else 0.0
        fp = (f_kg / tot_kg * 100.0) if tot_kg else 0.0
        b_ukg = (b_imp / b_kg) if b_kg else None
        f_ukg = (f_imp / f_kg) if f_kg else None
        t_ukg = (tot_imp / tot_kg) if tot_kg else None
        return {
            "b_pct": bp, "b_kg": b_kg, "b_ukg": b_ukg, "b_imp": b_imp,
            "f_pct": fp, "f_kg": f_kg, "f_ukg": f_ukg, "f_imp": f_imp,
            "t_pct": 100.0 if tot_kg else 0.0,
            "t_kg": tot_kg, "t_ukg": t_ukg, "t_imp": tot_imp,
        }

    filas = []
    tot_b_kg = tot_b_imp = tot_f_kg = tot_f_imp = 0.0
    for (yy, mm), slot in sorted(meses.items()):
        b_kg, b_imp = slot["Bajos"]["kg"], slot["Bajos"]["imp"]
        f_kg, f_imp = slot["Fuertes"]["kg"], slot["Fuertes"]["imp"]
        tot_b_kg += b_kg; tot_b_imp += b_imp
        tot_f_kg += f_kg; tot_f_imp += f_imp
        fila = {"yy": yy, "mm": mm, "label": f"{mm:02d}/{yy}"}
        fila.update(_calc(b_kg, b_imp, f_kg, f_imp))
        filas.append(fila)

    # Fila PROMEDIO mensual (no del año total, sino el promedio por mes con data)
    n_meses = len(filas) or 1
    promedio = {"label": "PROMEDIO"}
    promedio.update(_calc(tot_b_kg / n_meses, tot_b_imp / n_meses,
                           tot_f_kg / n_meses, tot_f_imp / n_meses))

    # Fila TOTAL del rango
    total = {"label": "TOTAL RANGO"}
    total.update(_calc(tot_b_kg, tot_b_imp, tot_f_kg, tot_f_imp))

    if request.args.get("export") == "csv":
        csv_rows = []
        for f in filas + [promedio, total]:
            csv_rows.append({
                "mes": f["label"],
                "b_pct": f"{f['b_pct']:.1f}", "b_kg": f"{f['b_kg']:.2f}",
                "b_ukg": (f"{f['b_ukg']:.4f}" if f["b_ukg"] is not None else ""),
                "b_imp": f"{f['b_imp']:.2f}",
                "f_pct": f"{f['f_pct']:.1f}", "f_kg": f"{f['f_kg']:.2f}",
                "f_ukg": (f"{f['f_ukg']:.4f}" if f["f_ukg"] is not None else ""),
                "f_imp": f"{f['f_imp']:.2f}",
                "t_kg": f"{f['t_kg']:.2f}",
                "t_ukg": (f"{f['t_ukg']:.4f}" if f["t_ukg"] is not None else ""),
                "t_imp": f"{f['t_imp']:.2f}",
            })
        return csv_response(
            csv_rows,
            columnas=[
                ("mes", "Mes"),
                ("b_pct", "Bajos %"), ("b_kg", "Bajos Kg"),
                ("b_ukg", "Bajos $/kg"), ("b_imp", "Bajos $"),
                ("f_pct", "Fuertes %"), ("f_kg", "Fuertes Kg"),
                ("f_ukg", "Fuertes $/kg"), ("f_imp", "Fuertes $"),
                ("t_kg", "Total Kg"), ("t_ukg", "Total $/kg"), ("t_imp", "Total $"),
            ],
            filename=f"tintoreria_{desde}_{hasta}.csv",
        )

    return render_template(
        "comparativa_tintoreria/tintoreria.html",
        filas=filas,
        promedio=promedio,
        total=total,
        desde=desde,
        hasta=hasta,
        error=error,
    )


@comparativa_tintoreria_bp.route("/comparativa-tintoreria")
@requiere_login
@requiere_permiso("tintura.ver")
def comparativa_tintoreria():
    """Vista comparativa: kg tinturados por día — PC (scintela.tinto) vs
    formulas_app (ordenes.tela_terminada_kg).

    Cruce solo por fecha — `scintela.tinto` NO guarda el número de OT que
    permitiría matchear a nivel orden. El detalle por color se muestra
    como filas hijas de cada día.
    """
    hoy = today_ec()
    # TMT 2026-05-27 dueña: "DEJA DE FILTRAR POR FECHA DE CREACION!
    # ME SALE TODO MAL" — volver al default ultimos 14 dias.
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
        # TMT 2026-05-27 dueña: REVERTIDO a terminado_desde/hasta.
        # El filtro por creacion (intentado para matchear Excel) hizo que
        # ordenes terminadas pero creadas antes del rango quedaran fuera,
        # mostrando kg vacios en muchos dias. La dueña prefiere ver TODO
        # lo que termino en el rango, aunque haya sido creado antes.
        from modules.tintura import service as tintura_service
        rows_form = tintura_service.tinturado_resumen(
            limite=20000,
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

    # TMT 2026-05-26 dueña: 'lo de formulas tiene que ir aca ... matchea
    # en codigo de tres letras el programa'.
    # Indexar form por (fecha_terminado, formula_cod) para enriquecer el
    # detalle_color del lado PC con cruda/terminada/desperdicio/fecha_term.
    form_por_fecha_cod: dict[tuple, dict] = defaultdict(
        lambda: {"cruda": 0.0, "terminada": 0.0, "n_ots": 0, "ots": []}
    )
    for o in rows_form:
        if o.fecha_terminado and o.formula_cod:
            cod_norm = (o.formula_cod or "").upper().strip()
            if cod_norm:
                key = (o.fecha_terminado, cod_norm)
                slot = form_por_fecha_cod[key]
                slot["cruda"] += float(o.tela_cruda_kg or 0)
                slot["terminada"] += float(o.tela_terminada_kg or 0)
                slot["n_ots"] += 1
                slot["ots"].append(o.to_dict())

    # Indexar PC color por fecha. Enriquece cada línea con los datos
    # de formulas_app del mismo código (suma kg crudo + terminado).
    # IMPORTANTE: forzar TODOS los numéricos a float — el SQL devuelve Decimal
    # y mezclarlos con float (de formulas_app) da TypeError en el template
    # (reportado: 'unsupported operand type(s) for -: Decimal and float').
    pc_color_por_fecha: dict[date, list[dict]] = defaultdict(list)
    for r in rows_pc_color:
        cod_norm = (r.get("cod") or "").upper().strip()
        key = (r["fecha"], cod_norm)
        form_match = form_por_fecha_cod.get(key)
        enriched = dict(r)
        # Force-cast los campos numéricos del SQL (Decimal) a float.
        if enriched.get("kg") is not None:
            enriched["kg"] = float(enriched["kg"])
        if enriched.get("importe") is not None:
            enriched["importe"] = float(enriched["importe"])
        if enriched.get("n_lineas") is not None:
            try:
                enriched["n_lineas"] = int(enriched["n_lineas"])
            except (TypeError, ValueError):
                pass
        if form_match:
            cruda = form_match["cruda"]
            terminada = form_match["terminada"]
            enriched["form_cruda_kg"] = cruda
            enriched["form_terminada_kg"] = terminada
            enriched["form_n_ots"] = form_match["n_ots"]
            enriched["form_fecha_terminado"] = r["fecha"]  # mismo día
            # Desperdicio % = (cruda - terminada) / cruda * 100. None si no aplica.
            if cruda > 0 and terminada >= 0:
                desperd_pct = (cruda - terminada) / cruda * 100.0
                enriched["form_desperdicio_pct"] = round(desperd_pct, 1)
            else:
                enriched["form_desperdicio_pct"] = None
        else:
            enriched["form_cruda_kg"] = None
            enriched["form_terminada_kg"] = None
            enriched["form_n_ots"] = 0
            enriched["form_fecha_terminado"] = None
            enriched["form_desperdicio_pct"] = None
        pc_color_por_fecha[r["fecha"]].append(enriched)

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

    # TMT 2026-05-26 dueña: tabla final con 7 columnas — una fila por
    # (fecha terminado, código color). Wrap todo en try/except — 2 veces
    # dio 500 (3af9b500, 81b1557c) por edge case del data. Fail-soft.
    filas_codigo: list[dict] = []
    filas_dia: list[dict] = []
    try:
        for f in sorted([x for x in fechas if x is not None], reverse=True):
            # 1) Códigos PC (con o sin match formulas).
            pc_cods_dia = set()
            for c in pc_color_por_fecha.get(f, []):
                try:
                    cod_pc = str(c.get("cod") or "—").upper().strip()
                    pc_cods_dia.add(cod_pc)
                    kg_pc = float(c.get("kg") or 0)
                    importe_pc = float(c.get("importe") or 0)
                    raw_term = c.get("form_terminada_kg")
                    raw_cruda = c.get("form_cruda_kg")
                    kg_form_term = float(raw_term) if raw_term is not None else None
                    kg_form_cruda = float(raw_cruda) if raw_cruda is not None else None
                    cambio = (kg_pc - kg_form_cruda) if kg_form_cruda is not None else None
                    raw_desp = c.get("form_desperdicio_pct")
                    desperd_pct = float(raw_desp) if raw_desp is not None else None
                    # TMT 2026-05-26 dueña: 'el costo calculalo con formulas!'
                    # Costo $/kg usando kg de formulas (terminado preferido,
                    # fallback a crudo, fallback a dbase si formulas vacío).
                    if kg_form_term and kg_form_term > 0:
                        costo_kg = importe_pc / kg_form_term
                    elif kg_form_cruda and kg_form_cruda > 0:
                        costo_kg = importe_pc / kg_form_cruda
                    elif kg_pc > 0:
                        costo_kg = importe_pc / kg_pc
                    else:
                        costo_kg = None
                    filas_codigo.append({
                        "fecha": f,
                        "cod": cod_pc,
                        "kg_dbase": kg_pc,
                        "kg_form_term": kg_form_term,
                        "cambio": cambio,
                        "kg_form_cruda": kg_form_cruda,
                        "desperdicio_pct": desperd_pct,
                        "form_n_ots": int(c.get("form_n_ots") or 0),
                        "costo_kg": costo_kg,
                    })
                except Exception:
                    continue
            # 2) TMT 2026-05-26 dueña: 'como no matchean codigo y deberian son
            # los mismos'. Agregamos también las OTs de formulas_app cuyo
            # formula_cod NO está en los códigos PC del día — así la dueña ve
            # qué códigos formulas existen sin contraparte y puede investigar
            # el desfase (case, espacios, código distinto, etc).
            for (form_fecha, form_cod), form_data in form_por_fecha_cod.items():
                if form_fecha != f:
                    continue
                if form_cod in pc_cods_dia:
                    continue  # ya está en la lista PC
                try:
                    cruda = float(form_data.get("cruda") or 0)
                    term = float(form_data.get("terminada") or 0)
                    desperd = ((cruda - term) / cruda * 100.0) if cruda > 0 else None
                    filas_codigo.append({
                        "fecha": f,
                        "cod": f"⚠ {form_cod} (solo form)",
                        "kg_dbase": None,
                        "kg_form_term": term if term > 0 else None,
                        "cambio": None,
                        "kg_form_cruda": cruda if cruda > 0 else None,
                        "desperdicio_pct": round(desperd, 1) if desperd is not None else None,
                        "form_n_ots": int(form_data.get("n_ots") or 0),
                        # TMT 2026-05-27 dueña: 'dict object has no attribute
                        # costo_kg'. Sin importe PC (es OT solo-formulas) no
                        # podemos calcular costo $/kg. Explicit None evita el
                        # error de Jinja al hacer r.costo_kg.
                        "costo_kg": None,
                    })
                except Exception:
                    continue

        for f in sorted([x for x in fechas if x is not None], reverse=True):
            try:
                cods = pc_color_por_fecha.get(f, [])
                kg_dbase = sum(float(c.get("kg") or 0) for c in cods)
                # TMT 2026-05-26 dueña: 'lo de formulas esta mal'. Antes
                # sumaba solo las OTs que matcheaban con código PC → daba
                # ~3x menos. Ahora suma TODAS las OTs terminadas del día,
                # sin importar si matcheó con código PC.
                ots_dia = form_por_fecha.get(f, [])
                kg_form_term = sum(float(o.tela_terminada_kg or 0) for o in ots_dia)
                kg_form_cruda = sum(float(o.tela_cruda_kg or 0) for o in ots_dia)
                n_ots = len(ots_dia)
                cambio = (kg_dbase - kg_form_cruda) if kg_form_cruda > 0 else None
                desperd_pct = ((kg_form_cruda - kg_form_term) / kg_form_cruda * 100.0) if kg_form_cruda > 0 else None
                # TMT 2026-05-26 dueña: costo con formulas (kg terminado o crudo).
                importe_dia = sum(float(c.get("importe") or 0) for c in cods)
                if kg_form_term and kg_form_term > 0:
                    costo_promedio = importe_dia / kg_form_term
                elif kg_form_cruda and kg_form_cruda > 0:
                    costo_promedio = importe_dia / kg_form_cruda
                elif kg_dbase > 0:
                    costo_promedio = importe_dia / kg_dbase
                else:
                    costo_promedio = None
                filas_dia.append({
                    "fecha": f,
                    "cod": "",
                    "kg_dbase": float(kg_dbase),
                    "kg_form_term": float(kg_form_term) if kg_form_term > 0 else None,
                    "cambio": float(cambio) if cambio is not None else None,
                    "kg_form_cruda": float(kg_form_cruda) if kg_form_cruda > 0 else None,
                    "desperdicio_pct": round(desperd_pct, 1) if desperd_pct is not None else None,
                    "form_n_ots": n_ots,
                    "costo_kg": costo_promedio,
                })
            except Exception:
                continue
    except Exception as _e:
        _LOG.exception("Tabla 7 cols falló: %s", _e)
        filas_codigo = []
        filas_dia = []

    # TMT 2026-05-26 dueña: default 'dia' (más limpio). Toggle a 'codigo' opcional.
    vista = (request.args.get("vista") or "dia").lower()
    if vista not in ("codigo", "dia"):
        vista = "dia"

    try:
        return render_template(
            "comparativa_tintoreria/index.html",
            filas=filas,
            filas_codigo=filas_codigo,
            filas_dia=filas_dia,
            vista=vista,
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
    except Exception as _e_render:
        _LOG.exception("render comparativa-tintoreria falló: %s", _e_render)
        # Render minimal con solo lo crítico — sin filas_codigo/dia.
        return render_template(
            "comparativa_tintoreria/index.html",
            filas=[],
            filas_codigo=[],
            filas_dia=[],
            vista="codigo",
            desde=desde,
            hasta=hasta,
            tot_pc_kg=0.0,
            tot_form_kg=0.0,
            tot_pc_us=0.0,
            tot_diff_kg=0.0,
            tot_diff_pct=None,
            error=f"Error renderizando tabla: {_e_render}",
        )


# ---------------------------------------------------------------------------
# DEBUG endpoint — comparar tinturado_resumen vs get_telas_report
# ---------------------------------------------------------------------------
# TMT 2026-05-26: la dueña reportó "TINTORERIA ESTA MAL MATCHEADO" vs el
# Excel oficial /telas/export. Adivinar el bug no funcionó (rompí más cosas).
# Este endpoint corre AMBAS queries en el web server (que ya tiene las env
# vars cargadas) y devuelve diff por día + órdenes específicas que difieren.
# JSON → fácil de leer desde Chrome / curl, sin más SSM.
@comparativa_tintoreria_bp.route("/debug-tintoreria-diff")
@requiere_login
@requiere_permiso("tintura.ver")
def debug_tintoreria_diff():
    """JSON: compara get_telas_report (fuente del Excel) vs tinturado_resumen
    (lo que usa la pantalla). Identifica órdenes específicas que difieren.

    Params: desde, hasta (YYYY-MM-DD). Default últimos 26 días.
    """
    from collections import defaultdict

    from flask import jsonify

    from modules._lib import formulas_db
    from modules.tintura import service as tintura_service

    hoy = today_ec()
    desde = _parse_date(request.args.get("desde"), hoy - timedelta(days=26))
    hasta = _parse_date(request.args.get("hasta"), hoy)

    # A) get_telas_report (lo que usa el Excel oficial)
    a_rows = formulas_db.fetch_all(
        """
        SELECT o.id, o.numero, o.fecha, o.codigo, o.jet,
               o.tela_cruda_kg, o.tela_terminada_kg, o.fecha_terminado,
               o.es_reproceso,
               SUBSTRING(o.fecha, 7, 4)||'-'||SUBSTRING(o.fecha, 4, 2)||'-'||SUBSTRING(o.fecha, 1, 2) AS fecha_iso
          FROM ordenes o
         WHERE SUBSTRING(o.fecha, 7, 4)||'-'||SUBSTRING(o.fecha, 4, 2)||'-'||SUBSTRING(o.fecha, 1, 2)
               BETWEEN %s AND %s
         ORDER BY fecha_iso ASC, o.id ASC
        """,
        (desde.isoformat(), hasta.isoformat()),
    )
    a_by_ft = defaultdict(lambda: {"n": 0, "cruda": 0.0, "term": 0.0, "ordenes": []})
    a_by_id = {}
    sin_ft_a = 0
    for r in a_rows:
        a_by_id[r['id']] = r
        ft = str(r.get("fecha_terminado") or "")[:10]
        if not ft:
            sin_ft_a += 1
            continue
        s = a_by_ft[ft]
        s["n"] += 1
        s["cruda"] += float(r.get("tela_cruda_kg") or 0)
        s["term"] += float(r.get("tela_terminada_kg") or 0)
        s["ordenes"].append({
            "id": r['id'], "numero": r['numero'], "fecha_creacion": r['fecha'],
            "cruda": float(r.get("tela_cruda_kg") or 0),
            "term": float(r.get("tela_terminada_kg") or 0),
            "es_reproceso": bool(r.get("es_reproceso")),
        })

    # B) tinturado_resumen (lo que usa la pantalla actual — terminado_*)
    b_rows = tintura_service.tinturado_resumen(
        limite=20000, terminado_desde=desde, terminado_hasta=hasta,
    )
    # C) tinturado_resumen con creacion_* — para confirmar si matchea A (Excel)
    c_rows = tintura_service.tinturado_resumen(
        limite=20000, creacion_desde=desde, creacion_hasta=hasta,
    )
    c_by_ft = defaultdict(lambda: {"n": 0, "cruda": 0.0, "term": 0.0})
    c_sin_ft = 0
    for o in c_rows:
        ft = o.fecha_terminado.isoformat() if o.fecha_terminado else None
        if not ft:
            c_sin_ft += 1
            continue
        s = c_by_ft[ft]
        s["n"] += 1
        s["cruda"] += float(o.tela_cruda_kg or 0)
        s["term"] += float(o.tela_terminada_kg or 0)
    tot_c_c = sum(s["cruda"] for s in c_by_ft.values())
    tot_c_t = sum(s["term"] for s in c_by_ft.values())
    tot_c_n = sum(s["n"] for s in c_by_ft.values())
    b_by_ft = defaultdict(lambda: {"n": 0, "cruda": 0.0, "term": 0.0, "ordenes": []})
    b_by_numero = {}
    for o in b_rows:
        b_by_numero[o.numero] = o
        ft = o.fecha_terminado.isoformat() if o.fecha_terminado else None
        if not ft:
            continue
        s = b_by_ft[ft]
        s["n"] += 1
        s["cruda"] += float(o.tela_cruda_kg or 0)
        s["term"] += float(o.tela_terminada_kg or 0)
        s["ordenes"].append({
            "numero": o.numero,
            "fecha_creacion": o.fecha.isoformat() if o.fecha else None,
            "cruda": float(o.tela_cruda_kg or 0),
            "term": float(o.tela_terminada_kg or 0),
            "es_reproceso": o.es_reproceso,
        })

    # Diff por día
    all_dias = sorted(set(a_by_ft) | set(b_by_ft))
    diff = []
    tot_a_c = tot_a_t = tot_b_c = tot_b_t = 0.0
    tot_a_n = tot_b_n = 0
    for d in all_dias:
        a = a_by_ft.get(d, {"n": 0, "cruda": 0.0, "term": 0.0})
        b = b_by_ft.get(d, {"n": 0, "cruda": 0.0, "term": 0.0})
        tot_a_c += a["cruda"]; tot_a_t += a["term"]; tot_a_n += a["n"]
        tot_b_c += b["cruda"]; tot_b_t += b["term"]; tot_b_n += b["n"]
        diff.append({
            "fecha": d,
            "a_n": a["n"], "a_cruda": round(a["cruda"], 1), "a_term": round(a["term"], 1),
            "b_n": b["n"], "b_cruda": round(b["cruda"], 1), "b_term": round(b["term"], 1),
            "delta_n": b["n"] - a["n"],
            "delta_cruda": round(b["cruda"] - a["cruda"], 1),
        })

    # Órdenes específicas que difieren — solo en uno o solo en otro
    a_numeros_terminados = {r['numero'] for r in a_rows if r.get('fecha_terminado')}
    b_numeros = set(b_by_numero.keys())
    solo_en_a = sorted(a_numeros_terminados - b_numeros)
    solo_en_b = sorted(b_numeros - a_numeros_terminados)

    detalle_solo_a = []
    for n in solo_en_a[:50]:
        rr = next((x for x in a_rows if x['numero'] == n), None)
        if rr:
            detalle_solo_a.append({
                "numero": n, "id": rr['id'], "fecha_creacion": rr['fecha'],
                "fecha_terminado": str(rr.get('fecha_terminado') or ''),
                "cruda": float(rr.get('tela_cruda_kg') or 0),
                "term": float(rr.get('tela_terminada_kg') or 0),
            })

    detalle_solo_b = []
    for n in solo_en_b[:50]:
        o = b_by_numero[n]
        detalle_solo_b.append({
            "numero": n,
            "fecha_creacion": o.fecha.isoformat() if o.fecha else None,
            "fecha_terminado": o.fecha_terminado.isoformat() if o.fecha_terminado else None,
            "cruda": float(o.tela_cruda_kg or 0),
            "term": float(o.tela_terminada_kg or 0),
            "es_reproceso": o.es_reproceso,
        })

    # Diff por dia para C vs A
    c_diff = []
    for d in sorted(set(a_by_ft) | set(c_by_ft)):
        a = a_by_ft.get(d, {"n": 0, "cruda": 0.0, "term": 0.0})
        c = c_by_ft.get(d, {"n": 0, "cruda": 0.0, "term": 0.0})
        c_diff.append({
            "fecha": d,
            "a_n": a["n"], "a_cruda": round(a["cruda"], 1),
            "c_n": c["n"], "c_cruda": round(c["cruda"], 1),
            "delta_n": c["n"] - a["n"],
            "delta_cruda": round(c["cruda"] - a["cruda"], 1),
        })

    return jsonify({
        "rango": {"desde": desde.isoformat(), "hasta": hasta.isoformat()},
        "a": {
            "fuente": "get_telas_report (Excel oficial)",
            "filtro": "fecha (creacion) BETWEEN desde y hasta",
            "total_ordenes": len(a_rows),
            "sin_fecha_terminado": sin_ft_a,
            "total_cruda": round(tot_a_c, 1),
            "total_term": round(tot_a_t, 1),
        },
        "b": {
            "fuente": "tinturado_resumen (pantalla actual)",
            "filtro": "fecha_terminado BETWEEN desde y hasta",
            "total_ordenes": len(b_rows),
            "total_cruda": round(tot_b_c, 1),
            "total_term": round(tot_b_t, 1),
        },
        "c": {
            "fuente": "tinturado_resumen con creacion_* (candidato a fix)",
            "filtro": "fecha (creacion) BETWEEN desde y hasta",
            "total_ordenes": len(c_rows),
            "sin_fecha_terminado": c_sin_ft,
            "total_cruda": round(tot_c_c, 1),
            "total_term": round(tot_c_t, 1),
            "matchea_A": (abs(tot_c_c - tot_a_c) < 1 and abs(tot_c_t - tot_a_t) < 1),
        },
        "c_vs_a_diff_por_dia": c_diff,
        "diff_por_dia": diff,
        "ordenes_solo_en_A_excel": {
            "count": len(solo_en_a),
            "sample": detalle_solo_a,
        },
        "ordenes_solo_en_B_pantalla": {
            "count": len(solo_en_b),
            "sample": detalle_solo_b,
        },
    })
