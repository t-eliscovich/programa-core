"""Vistas de cobranzas — calendario + cobros recibidos."""
from flask import Blueprint, render_template, request

from auth import requiere_login, requiere_permiso
from parsers import parse_int

from . import queries

cobranzas_bp = Blueprint("cobranzas", __name__, template_folder="templates")


@cobranzas_bp.route("/cobros-efectivo")
@requiere_login
@requiere_permiso("cartera.ver")
def efectivo():
    """Cobros recibidos en los últimos N días, agrupados por día.

    Reemplaza la opción `E. COBROS EFECT.SEMANA` del menú dBase legacy.
    """
    dias = parse_int(request.args.get("dias")) or 7
    dias = max(1, min(dias, 90))
    try:
        agenda = queries.cobros_agenda(dias)
        totales = queries.cobros_totales(dias)
        error = None
    except Exception as e:
        agenda, totales, error = [], {}, str(e)
    return render_template(
        "cobranzas/efectivo.html",
        agenda=agenda, totales=totales, dias=dias, error=error,
    )


@cobranzas_bp.route("/cobranzas/calendario")
@requiere_login
@requiere_permiso("cartera.ver")
def calendario():
    """Calendario de cobranzas próximos N días.

    Ventana default: 7 días atrás (vencidos pendientes) + 30 adelante.
    Configurable via `?atras=` y `?adelante=`.
    """
    dias_atras    = parse_int(request.args.get("atras"))    or 7
    dias_adelante = parse_int(request.args.get("adelante")) or 30
    dias_atras    = max(0, min(dias_atras, 90))
    dias_adelante = max(1, min(dias_adelante, 180))

    try:
        agenda = queries.agenda_dias(dias_atras, dias_adelante)
        totales = queries.totales_periodo(dias_atras, dias_adelante)
        error = None
    except Exception as e:
        agenda, totales, error = [], {}, str(e)

    return render_template(
        "cobranzas/calendario.html",
        agenda=agenda,
        totales=totales,
        dias_atras=dias_atras,
        dias_adelante=dias_adelante,
        error=error,
    )


@cobranzas_bp.route("/cobranzas/matriz-3-semanas")
@requiere_login
@requiere_permiso("cartera.ver")
def matriz_3_semanas():
    """Matriz de cobros últimas 3 semanas — replica MENU.PRG:1493-1522 (EFECT).

    Muestra cobros por día (lun-sáb) en una matriz 3×6 + total semana
    + promedio por día hábil.

    Query param `?hasta=YYYY-MM-DD` opcional para mirar histórico.
    """
    from parsers import parse_date as _pd
    # TMT 2026-05-15 (re-audit M3): validamos `?hasta=` explícitamente.
    # Antes, un string mal formado silenciosamente caía a None (= hoy) y
    # el usuario veía la matriz actual sin saber que su filtro fue
    # ignorado.
    hasta_raw = (request.args.get("hasta") or "").strip()
    hasta = None
    error = None
    if hasta_raw:
        hasta = _pd(hasta_raw)
        if hasta is None:
            error = f"Fecha 'hasta' inválida: {hasta_raw!r}. Usá YYYY-MM-DD."
    try:
        if error:
            semanas, total_3sem, prom_3sem = [], 0.0, 0.0
        else:
            semanas = queries.cobros_matriz_3_semanas(fecha_hasta=hasta)
            # Total general de las 3 semanas para el header.
            total_3sem = sum(float(s.get("total_semana") or 0) for s in semanas)
            prom_3sem = total_3sem / 15.0 if total_3sem else 0.0  # 3 sem × 5 días hábiles
    except Exception as e:
        semanas, total_3sem, prom_3sem, error = [], 0.0, 0.0, str(e)
    return render_template(
        "cobranzas/matriz_3_semanas.html",
        semanas=semanas,
        total_3sem=total_3sem,
        prom_3sem=prom_3sem,
        hasta=hasta,
        error=error,
    )
