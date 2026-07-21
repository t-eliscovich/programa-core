"""/informes/comparativa-tintoreria — cruce kg/día PC vs formulas_app.

Blueprint propio (no toca modules/informes/views.py) — montado bajo
url_prefix="/informes" para que la URL canónica sea /informes/comparativa-tintoreria.
"""
from __future__ import annotations

import calendar
import logging
from collections import defaultdict
from datetime import date, timedelta

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

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


def _build_tintoreria_mensual(anio: int, mes: int, n_meses: int | None = None) -> dict | None:
    """Devuelve la estructura {filas, promedio, total, limite_bajos} con TODOS
    los meses del año `anio` que tengan data (hasta el mes `mes` inclusive).
    None si falla.  Se usa para inyectar en el template de /informes/flujo-produccion.

    TMT 2026-07-07 — pedido dueña: el PROMEDIO tiene que ser el promedio POR
    AÑO (promedio 2026 = todos los meses de 2026 con data; en 2027 sería el
    promedio de 2027), no el del último mes. Antes la ventana era "últimos 12
    meses" y con un solo mes cargado el PROMEDIO quedaba idéntico al mes único.
    Ahora la ventana es el AÑO calendario de `anio` (enero -> mes en curso) y
    el PROMEDIO = suma meses del año / cantidad de meses del año con data.
    El parámetro `n_meses` se conserva por compatibilidad pero se ignora.
    """
    _ = n_meses  # compat: ya no se usa (ventana = año calendario)
    desde = date(anio, 1, 1)
    hasta = date(anio, mes, calendar.monthrange(anio, mes)[1])

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

    # scintela.tinto solo guarda meses del dBase (pre-corte). Los meses de
    # formulas se AGREGAN SOLO para los (yy, mm) que scintela.tinto no tiene,
    # para no doblar un mes que ya vino del dBase.
    # Dueña 2026-07-21: los meses de formulas salen del universo PRODUCCIÓN
    # TINTORERÍA — tinto_formulas_terminadas_por_mes usa tinturado_resumen
    # (órdenes con fecha_terminado en el mes, kg = tela terminada, lavados y
    # reprocesos incluidos) → el Total kg de la tabla = el de la página
    # Producción Tintorería, EXACTO por construcción (misma función, mismo
    # rango). Antes (tinto_formulas_bajos_fuertes_por_mes, por fecha de
    # creación y sin lavados) la tabla daba 165k vs 216k para el mismo julio.
    try:
        raw_f = queries.tinto_formulas_terminadas_por_mes(desde, hasta) or []
    except Exception:  # noqa: BLE001
        raw_f = []
    meses_tinto = set(meses_dict.keys())
    for r in raw_f:
        k = (int(r["yy"]), int(r["mm"]))
        if k in meses_tinto:
            continue  # ese mes ya vino del dBase -- no mezclar bases de costo
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

    # TMT 2026-07-08 (dueña): en la vista dejar SOLO el mes en curso + el
    # PROMEDIO del año. El promedio se calcula sobre TODOS los meses (arriba);
    # acá filtramos las FILAS mostradas al mes seleccionado.
    filas_mes = [x for x in filas if x["yy"] == anio and x["mm"] == mes]

    # ── PROYECTADO del mes en curso (dueña 2026-07-16) ───────────────────────
    # La tintorería costea SOLO lo terminado (con kg de tela). Las órdenes EN
    # PROCESO (ya teñidas, sin cerrar la tela) tienen el químico ya consumido y
    # el kg empezado (ordenes.kil). Proyectamos su kg terminado con la merma
    # REAL del mes (terminada/kil de lo ya cerrado) y sumamos: Proyectado =
    # actual + proceso. Solo para el mes seleccionado, todo de formulas.
    try:
        from modules._lib import formulas_db as _fdb
        _pr = _fdb.fetch_all(
            """
            WITH ord AS (
                SELECT o.id, COALESCE(o.tela_cruda_kg, 0)     AS cruda,
                       COALESCE(o.tela_terminada_kg, 0)       AS term,
                       COALESCE(o.kil, 0)                     AS kil,
                       SUM(ol.cantidad_kg
                         * COALESCE(NULLIF(ol.precio_us, 0), p.us, 0)) AS imp
                  FROM ordenes o
                  JOIN orden_lineas ol ON ol.orden_id = o.id
                  JOIN productos p    ON p.num = ol.producto_num
                  LEFT JOIN formulas f ON f.cod = o.codigo
                 WHERE UPPER(TRIM(p.familia)) IN ('POLI', 'ALG', 'AUX')
                   AND TO_DATE(o.fecha, 'DD/MM/YYYY') >= %(d1)s
                   AND TO_DATE(o.fecha, 'DD/MM/YYYY') <= %(d2)s
                   AND COALESCE(f.categoria, '') NOT ILIKE '%%lavado%%'
                   AND COALESCE(f.color, '')     NOT ILIKE 'LAV%%'
                   AND UPPER(TRIM(COALESCE(o.codigo, ''))) <> 'LAV'
                 GROUP BY o.id, o.tela_cruda_kg, o.tela_terminada_kg, o.kil
            )
            SELECT (cruda > 0) AS fin,
                   CASE WHEN imp / NULLIF(CASE WHEN cruda > 0 THEN cruda ELSE kil END, 0)
                             <= %(lim)s THEN 'Bajos' ELSE 'Fuertes' END AS tipo,
                   COALESCE(SUM(imp), 0)  AS imp,
                   COALESCE(SUM(term), 0) AS term,
                   COALESCE(SUM(kil), 0)  AS kil
              FROM ord GROUP BY 1, 2
            """,
            {"d1": date(anio, mes, 1).isoformat(),
             "d2": hasta.isoformat(), "lim": 0.4},  # 0.4 = umbral Bajos/Fuertes
        ) or []
        # Merma (terminada/kil) POR GRUPO — Bajos y Fuertes tienen merma distinta
        # (dueña 2026-07-16), así que cada uno proyecta con la suya.
        _fb_term = _fb_kil = _ff_term = _ff_kil = 0.0
        _pb_imp = _pb_kil = _pf_imp = _pf_kil = 0.0
        for _r in _pr:
            _imp, _term, _kil = (float(_r.get("imp") or 0),
                                 float(_r.get("term") or 0),
                                 float(_r.get("kil") or 0))
            _es_bajo = (_r.get("tipo") == "Bajos")
            if _r.get("fin"):
                if _es_bajo:
                    _fb_term += _term; _fb_kil += _kil
                else:
                    _ff_term += _term; _ff_kil += _kil
            elif _es_bajo:
                _pb_imp += _imp; _pb_kil += _kil
            else:
                _pf_imp += _imp; _pf_kil += _kil
        _merma_all = ((_fb_term + _ff_term) / (_fb_kil + _ff_kil)
                      if (_fb_kil + _ff_kil) else 0.0)
        _merma_b = (_fb_term / _fb_kil) if _fb_kil else _merma_all
        _merma_f = (_ff_term / _ff_kil) if _ff_kil else _merma_all
        _cur = next((x for x in filas_mes
                     if x["yy"] == anio and x["mm"] == mes), None)
        if _cur and _merma_all > 0 and (_pb_kil or _pf_kil):
            # kg proyectado = actual + proceso (empezado × merma del grupo).
            _b_kg = float(_cur["b_kg"]) + _pb_kil * _merma_b
            _f_kg = float(_cur["f_kg"]) + _pf_kil * _merma_f
            # $ proyectado = kg proyectado × $/kg ACTUAL de cada grupo (dueña
            # 2026-07-16: proyectar al mismo $/kg → el $/kg no cambia).
            _b_ukg = (float(_cur["b_imp"]) / float(_cur["b_kg"])
                      if _cur["b_kg"] else 0.0)
            _f_ukg = (float(_cur["f_imp"]) / float(_cur["f_kg"])
                      if _cur["f_kg"] else 0.0)
            _b_imp = _b_kg * _b_ukg
            _f_imp = _f_kg * _f_ukg
            _cur["proy"] = _calc(_b_kg, _b_imp, _f_kg, _f_imp,
                                 float(_cur.get("gp_imp") or 0))
            _cur["proy_kg_empez"] = round(_pb_kil + _pf_kil, 0)
    except Exception:  # noqa: BLE001 -- fail-soft, la tabla igual renderiza
        pass

    return {
        "filas": filas_mes,
        "promedio": promedio,
        "total": total,
        "anio": anio,
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
        # La vista flujo_produccion ya calcula el mensual (para usar el
        # proyectado como consumo de químico) y lo deja en g. Reusarlo evita
        # recalcular (2 round-trips a formulas). Dueña 2026-07-16.
        from flask import g
        _cached = getattr(g, "_tint_mensual", None)
        if _cached is not None:
            return {"tintoreria_mensual": _cached}
        hoy = today_ec()
        anio = int(request.args.get("anio") or hoy.year)
        mes = max(1, min(12, int(request.args.get("mes") or hoy.month)))
        data = _build_tintoreria_mensual(anio, mes)
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
    # TMT 2026-07-14 (dueña): "eliminá el tab" — Costos mensuales colorantes ya
    # vive al final de /informes/flujo-produccion (tabla COSTOS DE TINTORERÍA).
    # Esta pantalla se retira; la ruta redirige allá (no rompe links viejos).
    return redirect(url_for("informes.flujo_produccion"))
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

    # scintela.tinto (dBase) sólo guarda el mes en curso; los meses previos
    # tienen su tinturado en formulas_app. Rellenar los meses que el dBase NO
    # tiene, sin doblar los que ya vinieron del dBase (mismo merge que
    # _build_tintoreria_mensual de /informes/flujo-produccion). TMT 2026-07-09.
    try:
        raw_f = queries.tinto_formulas_bajos_fuertes_por_mes(desde, hasta) or []
    except Exception:  # noqa: BLE001
        raw_f = []
    _meses_tinto = set(meses.keys())
    for r in raw_f:
        k = (int(r["yy"]), int(r["mm"]))
        if k in _meses_tinto:
            continue
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
    """Tintorería — kg tinturados por día, SÓLO desde Fórmulas App.

    TMT 2026-07-11 (dueña): "traer la info solo de formulas app y dejar de
    hacer comparación". Antes esta pantalla cruzaba PC (scintela.tinto, dBase)
    vs formulas_app día a día. Se retiró la comparación: ahora es una vista
    simple del tinturado terminado según Fórmulas App (kg crudo/terminado,
    desperdicio y costo $/kg de colorantes+aux). El histórico anterior al
    CORTE (7/7/2026) queda congelado en el balance como dBase; esta pantalla
    es sólo de consulta y no alimenta el balance.

    Cada día se puede desplegar en su detalle por código de fórmula.
    """
    from modules.tintura import service as tintura_service

    hoy = today_ec()
    default_desde = hoy - timedelta(days=14)
    desde = _parse_date(request.args.get("desde"), default_desde)
    hasta = _parse_date(request.args.get("hasta"), hoy)

    error = None
    rows_form: list = []
    costos_form: dict[str, float] = {}
    try:
        rows_form = tintura_service.tinturado_resumen(
            limite=20000, terminado_desde=desde, terminado_hasta=hasta,
        )
    except Exception as e:  # noqa: BLE001
        error = f"formulas_app: {e}"
    try:
        costos_form = tintura_service.costo_por_orden(
            terminado_desde=desde, terminado_hasta=hasta,
        )
    except Exception:  # noqa: BLE001 -- fail-soft
        costos_form = {}

    def _costo_kg(costo, term, cruda):
        """$/kg: costo / kg terminado (preferido) o crudo. None si no aplica."""
        if term and term > 0:
            return costo / term
        if cruda and cruda > 0:
            return costo / cruda
        return None

    # Agregado por (día terminado) y por (día, código de fórmula).
    por_dia: dict[date, dict] = defaultdict(
        lambda: {"n_ots": 0, "cruda": 0.0, "term": 0.0, "costo": 0.0, "ots": []}
    )
    por_dia_cod: dict[tuple, dict] = defaultdict(
        lambda: {"n_ots": 0, "cruda": 0.0, "term": 0.0, "costo": 0.0}
    )
    for o in rows_form:
        f = o.fecha_terminado
        if not f:
            continue
        cruda = float(o.tela_cruda_kg or 0)
        term = float(o.tela_terminada_kg or 0)
        costo = float(costos_form.get(o.numero, 0.0) or 0.0)
        d = por_dia[f]
        d["n_ots"] += 1
        d["cruda"] += cruda
        d["term"] += term
        d["costo"] += costo
        d["ots"].append(o.to_dict())
        cod = (o.formula_cod or "—").upper().strip() or "—"
        c = por_dia_cod[(f, cod)]
        c["n_ots"] += 1
        c["cruda"] += cruda
        c["term"] += term
        c["costo"] += costo

    fechas = sorted([f for f in por_dia if f is not None], reverse=True)

    filas = []
    tot_cruda = tot_term = tot_costo = 0.0
    tot_ots = 0
    for f in fechas:
        d = por_dia[f]
        cruda, term, costo, n = d["cruda"], d["term"], d["costo"], d["n_ots"]
        desperd = ((cruda - term) / cruda * 100.0) if cruda > 0 else None
        tot_cruda += cruda
        tot_term += term
        tot_costo += costo
        tot_ots += n
        # Detalle por código del día.
        detalle_cod = []
        for (fc, cod), c in por_dia_cod.items():
            if fc != f:
                continue
            c_desp = ((c["cruda"] - c["term"]) / c["cruda"] * 100.0) if c["cruda"] > 0 else None
            detalle_cod.append({
                "cod": cod,
                "n_ots": c["n_ots"],
                "cruda": c["cruda"],
                "term": c["term"],
                "desperdicio_pct": round(c_desp, 1) if c_desp is not None else None,
                "costo": c["costo"],
                "costo_kg": _costo_kg(c["costo"], c["term"], c["cruda"]),
            })
        detalle_cod.sort(key=lambda x: x["term"], reverse=True)
        filas.append({
            "fecha": f,
            "n_ots": n,
            "cruda": cruda,
            "term": term,
            "desperdicio_pct": round(desperd, 1) if desperd is not None else None,
            "costo": costo,
            "costo_kg": _costo_kg(costo, term, cruda),
            "detalle_cod": detalle_cod,
        })

    if request.args.get("export") == "csv":
        csv_rows = [{
            "fecha": fila["fecha"].isoformat(),
            "n_ots": fila["n_ots"],
            "cruda": f"{fila['cruda']:.1f}",
            "term": f"{fila['term']:.1f}",
            "desperdicio_pct": (f"{fila['desperdicio_pct']:.1f}" if fila["desperdicio_pct"] is not None else ""),
            "costo": f"{fila['costo']:.2f}",
            "costo_kg": (f"{fila['costo_kg']:.4f}" if fila["costo_kg"] is not None else ""),
        } for fila in filas]
        return csv_response(
            csv_rows,
            columnas=[
                ("fecha", "Fecha terminado"),
                ("n_ots", "OTs"),
                ("cruda", "Kg crudo"),
                ("term", "Kg terminado"),
                ("desperdicio_pct", "Desperdicio %"),
                ("costo", "Costo $"),
                ("costo_kg", "$/kg"),
            ],
            filename=f"tintoreria_formulas_{desde}_{hasta}.csv",
        )

    tot_desperd = ((tot_cruda - tot_term) / tot_cruda * 100.0) if tot_cruda > 0 else None
    return render_template(
        "comparativa_tintoreria/index.html",
        filas=filas,
        desde=desde,
        hasta=hasta,
        tot_ots=tot_ots,
        tot_cruda=tot_cruda,
        tot_term=tot_term,
        tot_costo=tot_costo,
        tot_desperdicio_pct=round(tot_desperd, 1) if tot_desperd is not None else None,
        tot_costo_kg=(_costo_kg(tot_costo, tot_term, tot_cruda)),
        error=error,
    )


_KG_EDIT_MARKER = "manual-kg-edit"


def _parse_kg_es(s) -> float:
    """Parsea '20.974,5' (es) o '20974.5' (en) a float."""
    s = str(s or "").strip().replace(" ", "")
    if not s:
        raise ValueError("vacío")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


@comparativa_tintoreria_bp.route("/comparativa-tintoreria/editar-kg", methods=["POST"])
@requiere_login
@requiere_permiso("tintura.registrar")
def editar_kg_dbase():
    """Setea el total KG DBASE de un día (para cargar data de prueba).

    TMT 2026-06-09 dueña: 'Dejame editar kg dbase, asi podemos cargar
    aca para probar'.

    No pisa las filas reales del DBF: inserta UNA fila de ajuste manual
    en scintela.tinto (cod 'MAN', usuario_crea 'manual-kg-edit') con el
    delta necesario para que el SUM del día quede en el valor ingresado.
    Re-editar reemplaza el ajuste anterior; ingresar el total original
    lo elimina.

    TMT 2026-06-10 dueña: los ajustes CUENTAN en el balance ("para eso
    lo creé") — el sync los preserva el mes corriente y los absorbe
    cuando el DBF trae filas de esa misma fecha (dBase gana).

    Acepta JSON o form: {fecha: 'YYYY-MM-DD', kg: '20.974,5' | '20974.5'}.
    """
    from flask import jsonify

    import db

    data = request.get_json(silent=True) or request.form
    try:
        fecha = date.fromisoformat(str(data.get("fecha", "")).strip())
    except (ValueError, AttributeError):
        return jsonify({"ok": False, "error": "Fecha inválida."}), 400
    try:
        nuevo = _parse_kg_es(data.get("kg"))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "KG inválido."}), 400

    try:
        with db.tx() as conn:
            # Total actual del día SIN el ajuste manual previo — misma
            # fórmula GREATEST(kgn, kg) que usa la pantalla, así el
            # resultado matchea exacto lo que se ve.
            base = db.fetch_one(
                """
                SELECT COALESCE(SUM(GREATEST(COALESCE(kgn, 0), COALESCE(kg, 0))), 0) AS kg
                  FROM scintela.tinto
                 WHERE fecha = %s
                   AND COALESCE(stat, '') NOT IN ('X', 'Y')
                   AND COALESCE(usuario_crea, '') <> %s
                """,
                (fecha, _KG_EDIT_MARKER),
                conn=conn,
            )
            delta = round(nuevo - float((base or {}).get("kg") or 0), 3)
            db.execute(
                "DELETE FROM scintela.tinto WHERE fecha = %s AND usuario_crea = %s",
                (fecha, _KG_EDIT_MARKER),
                conn=conn,
            )
            if abs(delta) >= 0.001:
                # TMT 2026-06-10 dueña ("¿restaste la tela cruda?", "275k
                # me parece mucho"): el ajuste imita lo que pasa en el dBase
                # al tipear la planilla — kgn con el DESPERDICIO promedio
                # del mes (antes kgn=kg inflaba terminado) e importe con el
                # $/kg promedio (antes importe=0 no restaba los químicos
                # consumidos del Stock Quí → utilidad optimista). Son
                # ESTIMACIONES: el sync trae los valores reales y absorbe
                # esta fila (dBase gana por fecha).
                prom = db.fetch_one(
                    """
                    SELECT COALESCE(SUM(kgn), 0) AS kr,
                           COALESCE(SUM(kg), 0)  AS kt,
                           COALESCE(SUM(importe), 0) AS itin
                      FROM scintela.tinto
                     WHERE fecha >= date_trunc('month', (CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)
                       AND UPPER(TRIM(COALESCE(color,''))) NOT LIKE 'LAV%%'
                       AND COALESCE(usuario_crea, '') <> %s
                    """,
                    (_KG_EDIT_MARKER,),
                    conn=conn,
                ) or {}
                _kt = float(prom.get("kt") or 0)
                rinde = (float(prom.get("kr") or 0) / _kt) if _kt > 0 else 0.957
                ukg = (float(prom.get("itin") or 0) / _kt) if _kt > 0 else 0.0
                kgn_est = round(delta * min(max(rinde, 0.90), 1.0), 1)
                imp_est = round(delta * ukg, 2)
                db.execute(
                    """
                    INSERT INTO scintela.tinto
                           (fecha, cod, color, kg, kgn, importe, stat, usuario_crea)
                    VALUES (%s, 'MAN', 'AJUSTE MANUAL', %s, %s, %s, '', %s)
                    """,
                    (fecha, delta, kgn_est, imp_est, _KG_EDIT_MARKER),
                    conn=conn,
                )
    except Exception as e:  # noqa: BLE001
        _LOG.exception("editar_kg_dbase falló: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({
        "ok": True,
        "fecha": fecha.isoformat(),
        "kg": nuevo,
        "ajuste": delta,
    })


# ---------------------------------------------------------------------------
# PLANILLA DE CARGA — réplica de la pantalla TINTO del dBase (MODIFICA.PRG)
# ---------------------------------------------------------------------------
# TMT 2026-06-09 dueña: "lo mismo que ingresan en el dbase, tienen que
# ingresar aca". En el dBase ingresan COD + KG + KGN; el COLOR y el costo
# $/kg salen del catálogo COSTOS.DBF y el programa calcula
# IMPORTE = KG × COSTO. Acá el catálogo es scintela.tinto_costos
# (migración 0083, editable en esta misma pantalla).
#
# El balance (Resultados → COL.QUI./KR, vía tinto_mes_corriente_resultado)
# suma scintela.tinto ENTERA — las filas cargadas acá entran solas, sin
# tocar informes/. El sync dBase preserva las filas pc-carga del mes en
# curso (ver import_dbf.py TINTO.DBF delete_where).

_PC_CARGA_MARKER = "pc-carga"


@comparativa_tintoreria_bp.route("/tinto-carga")
@requiere_login
@requiere_permiso("tintura.ver")
def tinto_carga():
    """Planilla del mes: filas de scintela.tinto + alta + catálogo de costos."""
    hoy = today_ec()
    try:
        anio = int(request.args.get("anio") or hoy.year)
        mes = max(1, min(12, int(request.args.get("mes") or hoy.month)))
    except (TypeError, ValueError):
        anio, mes = hoy.year, hoy.month

    error = None
    filas: list[dict] = []
    catalogo: list[dict] = []
    try:
        filas = queries.tinto_filas_mes(anio, mes) or []
    except Exception as e:  # noqa: BLE001
        error = f"tinto: {e}"
    try:
        catalogo = queries.tinto_costos_catalogo() or []
    except Exception as e:  # noqa: BLE001
        # Tabla puede no existir hasta correr la migración 0083.
        error = (error + " | " if error else "") + f"catálogo: {e} — ¿corriste /admin/migraciones?"

    tot_kg = sum(float(r.get("kg") or 0) for r in filas)
    tot_kgn = sum(float(r.get("kgn") or 0) for r in filas)
    tot_imp = sum(float(r.get("importe") or 0) for r in filas)

    return render_template(
        "comparativa_tintoreria/tinto_carga.html",
        filas=filas,
        catalogo=catalogo,
        anio=anio,
        mes=mes,
        hoy=hoy,
        tot_kg=tot_kg,
        tot_kgn=tot_kgn,
        tot_imp=tot_imp,
        error=error,
        marker=_PC_CARGA_MARKER,
    )


@comparativa_tintoreria_bp.route("/tinto-carga/agregar", methods=["POST"])
@requiere_login
@requiere_permiso("tintura.registrar")
def tinto_carga_agregar():
    """Alta de filas — replica el ADD (999) del dBase: COD + KG + KGN.

    TMT 2026-06-09 dueña: "dejame agregar mas lineas antes de cargar" —
    acepta N líneas en un solo POST (inputs repetidos fecha/cod/kg/kgn).
    Filas totalmente vacías se ignoran. Si CUALQUIER fila con data tiene
    error, no se carga NADA (all-or-nothing, así no quedan cargas a
    medias).

    Por fila: COLOR e IMPORTE salen del catálogo: importe = kg × costo
    (kg BRUTO, igual que `REPLA ... IMPORTE WITH KG * B->COSTO` del PRG).
    STAT 'Z' ('S' si COD='SER'). COD fuera del catálogo se rechaza (el
    dBase hace lo mismo: borra la fila).
    """
    import db

    fechas = request.form.getlist("fecha")
    cods = request.form.getlist("cod")
    kgs = request.form.getlist("kg")
    kgns = request.form.getlist("kgn")
    n = max(len(fechas), len(cods), len(kgs), len(kgns))

    def _at(lst, i):
        return lst[i] if i < len(lst) else ""

    filas: list[dict] = []
    errores: list[str] = []
    for i in range(n):
        f_raw = str(_at(fechas, i) or "").strip()
        cod = (_at(cods, i) or "").upper().strip()[:5]
        kg_raw = str(_at(kgs, i) or "").strip()
        kgn_raw = str(_at(kgns, i) or "").strip()
        if not cod and not kg_raw:
            continue  # línea vacía — ignorar
        rotulo = f"línea {len(filas) + len(errores) + 1}"
        try:
            fecha = date.fromisoformat(f_raw)
        except (ValueError, AttributeError):
            errores.append(f"{rotulo}: fecha inválida")
            continue
        try:
            kg = _parse_kg_es(kg_raw)
            kgn = _parse_kg_es(kgn_raw) if kgn_raw else kg
        except (ValueError, TypeError):
            errores.append(f"{rotulo} ({cod or '?'}): KG inválido")
            continue
        if not cod or kg <= 0:
            errores.append(f"{rotulo}: falta código o KG")
            continue
        filas.append({"fecha": fecha, "cod": cod, "kg": kg, "kgn": kgn})

    if not filas and not errores:
        flash("No hay líneas para cargar.", "error")
        return redirect(url_for("comparativa_tintoreria.tinto_carga"))

    # Lookup de catálogo para todos los códigos de una.
    cat_by_cod: dict[str, dict] = {}
    if filas:
        rows = db.fetch_all(
            "SELECT cod, color, costo FROM scintela.tinto_costos WHERE cod = ANY(%s)",
            ([f["cod"] for f in filas],),
        )
        cat_by_cod = {r["cod"]: r for r in rows}
        sin_cat = sorted({f["cod"] for f in filas} - set(cat_by_cod))
        if sin_cat:
            errores.append(
                "código(s) fuera del catálogo: " + ", ".join(sin_cat)
                + " — agregalos al catálogo primero"
            )

    if errores:
        flash("No se cargó nada. " + " | ".join(errores), "error")
        return redirect(url_for("comparativa_tintoreria.tinto_carga"))

    usuario = (getattr(g, "user", None) or {}).get("username", "web")
    tot_kg = tot_imp = 0.0
    try:
        with db.tx() as conn:
            for f in filas:
                cat = cat_by_cod[f["cod"]]
                importe = round(f["kg"] * float(cat.get("costo") or 0), 2)
                stat = "S" if f["cod"] == "SER" else "Z"
                db.execute(
                    """
                    INSERT INTO scintela.tinto
                           (fecha, cod, color, kg, kgn, importe, stat,
                            usuario_crea, usuario_modifica)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (f["fecha"], f["cod"], (cat.get("color") or "")[:30],
                     f["kg"], f["kgn"], importe, stat,
                     _PC_CARGA_MARKER, usuario),
                    conn=conn,
                )
                tot_kg += f["kg"]
                tot_imp += importe
    except Exception as e:  # noqa: BLE001
        _LOG.exception("tinto_carga_agregar falló: %s", e)
        flash(f"No se pudo guardar: {e}", "error")
        return redirect(url_for("comparativa_tintoreria.tinto_carga"))

    flash(
        f"Cargada{'s' if len(filas) != 1 else ''} {len(filas)} línea"
        f"{'s' if len(filas) != 1 else ''}: {tot_kg:,.1f} kg, $ {tot_imp:,.2f}",
        "success",
    )
    ult = filas[-1]["fecha"]
    return redirect(url_for(
        "comparativa_tintoreria.tinto_carga", anio=ult.year, mes=ult.month,
    ))


@comparativa_tintoreria_bp.route("/tinto-carga/<int:id_tinto>/borrar", methods=["POST"])
@requiere_login
@requiere_permiso("tintura.registrar")
def tinto_carga_borrar(id_tinto: int):
    """Borra una fila cargada en PC. Las del DBF no se tocan."""
    import db

    n = db.execute(
        "DELETE FROM scintela.tinto WHERE id_tinto = %s AND usuario_crea = %s",
        (id_tinto, _PC_CARGA_MARKER),
    )
    if n:
        flash("Fila borrada.", "success")
    else:
        flash("Solo se pueden borrar filas cargadas en PC (no las del dBase).", "error")
    return redirect(request.referrer or url_for("comparativa_tintoreria.tinto_carga"))


@comparativa_tintoreria_bp.route("/tinto-carga/costo", methods=["POST"])
@requiere_login
@requiere_permiso("tintura.registrar")
def tinto_carga_costo():
    """Upsert de una entrada del catálogo de costos (cod, color, costo)."""
    import db

    cod = (request.form.get("cod") or "").upper().strip()[:5]
    color = (request.form.get("color") or "").upper().strip()[:30]
    try:
        costo = _parse_kg_es(request.form.get("costo"))
    except (ValueError, TypeError):
        flash("Costo inválido.", "error")
        return redirect(url_for("comparativa_tintoreria.tinto_carga"))
    if not cod or costo < 0:
        flash("Falta código o costo.", "error")
        return redirect(url_for("comparativa_tintoreria.tinto_carga"))

    usuario = (getattr(g, "user", None) or {}).get("username", "web")
    try:
        db.execute(
            """
            INSERT INTO scintela.tinto_costos (cod, color, costo, usuario_crea)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (cod) DO UPDATE
               SET color = CASE WHEN EXCLUDED.color <> '' THEN EXCLUDED.color
                                ELSE scintela.tinto_costos.color END,
                   costo = EXCLUDED.costo,
                   fecha_modifica = CURRENT_TIMESTAMP,
                   usuario_modifica = %s
            """,
            (cod, color, costo, usuario, usuario),
        )
    except Exception as e:  # noqa: BLE001
        _LOG.exception("tinto_carga_costo falló: %s", e)
        flash(f"No se pudo guardar el costo: {e}", "error")
        return redirect(url_for("comparativa_tintoreria.tinto_carga"))

    flash(f"Catálogo: {cod} → $ {costo:,.4f}/kg", "success")
    return redirect(url_for("comparativa_tintoreria.tinto_carga"))


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
