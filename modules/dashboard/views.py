"""Dashboard route. Role-aware view of the most important numbers.

Nota TMT 2026-05-12: la home (`/`) ahora redirige a `/operaciones`. La vista
clásica del dashboard sigue disponible en `/tablero` por si la quiere consultar
puntualmente, pero ya no es la pantalla de bienvenida.
"""
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, g, redirect, render_template, request, session, url_for

from auth import requiere_login

from . import queries

_log = logging.getLogger("programa_core.dashboard")
_tim = logging.getLogger("programa_core.dashboard.timings")


def _dashboard_mode() -> str:
    """'parallel' (default) o 'sequential'. Controlado por env DASHBOARD_MODE.

    El modo secuencial existe para medir overhead del ThreadPoolExecutor en
    prod — si todas las queries son <100ms index-driven, el pool puede que
    no valga la pena. Ver scripts/dashboard_profile.py para el benchmark.
    """
    val = (os.environ.get("DASHBOARD_MODE") or "parallel").strip().lower()
    return "sequential" if val in ("sequential", "seq", "serial") else "parallel"

dashboard_bp = Blueprint(
    "dashboard",
    __name__,
    url_prefix="/tablero",
    template_folder="templates",
)


def _run_with_fallback(fn_default, name: str = "") -> tuple:
    """Corre fn y captura ValueError/Exception con fallback. Devuelve (res, ms, err)."""
    fn, default = fn_default
    t0 = time.perf_counter()
    try:
        res = fn()
        return res, (time.perf_counter() - t0) * 1000, None
    except Exception as e:  # nunca leak a la UI
        return default, (time.perf_counter() - t0) * 1000, type(e).__name__


def _parallel(tasks: dict) -> dict:
    """Run {name: callable} concurrently; return {name: result-or-default}.

    Each value in `tasks` is either a zero-arg callable, or a tuple
    `(callable, default)` where `default` is the fallback if it raises.

    Modos (env `DASHBOARD_MODE`):
        'parallel'   (default) — ThreadPoolExecutor con max_workers=min(8,N).
        'sequential'           — corre uno tras otro, para medir overhead.

    Ambos modos loggean timings por tarea a nivel DEBUG en
    `programa_core.dashboard.timings`. Subí el level del logger a DEBUG para
    ver el reporte por render. Usado por `scripts/dashboard_profile.py`.
    """
    normalized = {k: (v if isinstance(v, tuple) else (v, None)) for k, v in tasks.items()}
    mode = _dashboard_mode()

    t0 = time.perf_counter()
    if mode == "sequential":
        pairs = {name: _run_with_fallback(fd, name) for name, fd in normalized.items()}
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(normalized) or 1)) as pool:
            futures = {name: pool.submit(_run_with_fallback, fd, name) for name, fd in normalized.items()}
            pairs = {name: f.result() for name, f in futures.items()}
    wall_ms = (time.perf_counter() - t0) * 1000

    # Log estructurado: total + desglose por tarea. Si >1 task falló, WARNING;
    # si no, DEBUG.
    summary = [(name, ms, err) for name, (_res, ms, err) in pairs.items()]
    sum_ms = sum(ms for _n, ms, _e in summary)
    errs = [n for n, _, e in summary if e]
    lvl = logging.WARNING if errs else logging.DEBUG
    _tim.log(
        lvl,
        "dashboard mode=%s tasks=%d wall=%.0fms sum=%.0fms overhead=%.0fms%s details=%s",
        mode, len(normalized), wall_ms, sum_ms, max(0.0, wall_ms - sum_ms),
        (f" errs={errs}" if errs else ""),
        [(n, round(ms, 1), e) for n, ms, e in summary],
    )

    return {name: res for name, (res, _ms, _err) in pairs.items()}


def _tendencia(filas, campo, dias=7):
    """Compare today's value to N days ago. Returns {delta, pct, dir} or None.

    dir: 'up' | 'down' | 'flat'. None if we don't have enough history.
    """
    if not filas or len(filas) < 2:
        return None
    vals = [float(r.get(campo) or 0) for r in filas]
    idx = max(0, len(vals) - 1 - dias)
    prev, last = vals[idx], vals[-1]
    delta = last - prev
    if prev:
        pct = (delta / abs(prev)) * 100.0
    else:
        pct = 0.0
    direc = "up" if delta > 0 else ("down" if delta < 0 else "flat")
    return {"delta": delta, "pct": pct, "dir": direc, "prev": prev, "last": last, "dias": dias}


def _sparkline(filas, campo="saldo", w=800, h=120, pad=8):
    """Turn a list of dicts into SVG-path geometry + summary stats.

    Returns {path, area, points, min_v, max_v, first, last, delta, n}.
    Everything a template needs to render a non-interactive line chart inline.
    """
    vals = [float(r.get(campo) or 0) for r in filas]
    if not vals:
        return None
    n = len(vals)
    min_v, max_v = min(vals), max(vals)
    span = max_v - min_v or 1.0

    def x(i): return pad + (w - 2 * pad) * (i / max(n - 1, 1))
    def y(v): return pad + (h - 2 * pad) * (1 - (v - min_v) / span)

    coords = [(x(i), y(v)) for i, v in enumerate(vals)]
    # smooth line (linear is fine for 30 data points)
    path = "M " + " L ".join(f"{cx:.1f},{cy:.1f}" for cx, cy in coords)
    # area: same path + close to baseline
    area_end = f"L {coords[-1][0]:.1f},{h - pad:.1f} L {coords[0][0]:.1f},{h - pad:.1f} Z"
    area = path + " " + area_end
    # discrete point markers for the last 5
    pts = [(cx, cy) for cx, cy in coords[-5:]]

    return {
        "path": path,
        "area": area,
        "points": pts,
        "min_v": min_v,
        "max_v": max_v,
        "first": vals[0],
        "last": vals[-1],
        "delta": vals[-1] - vals[0],
        "n": n,
        "w": w,
        "h": h,
    }


_VISTAS_DUENO = ("simple", "completa")


def _vista_preferida(rol: str) -> str:
    """Determina la vista del dashboard para el rol actual.

    Sólo se aplica al Dueño. Orden de precedencia:
      1) `?vista=simple|completa` en la URL (se persiste en sesión).
      2) `session["dashboard_vista"]` (lo elegido la última vez).
      3) default "simple" para el Dueño (el pedido operativo: 4 números gigantes).
    """
    if rol not in ("Accionista", "Dueño"):  # TMT 2026-05-19 v8 rename Dueño→Accionista
        return "completa"
    q = (request.args.get("vista") or "").strip().lower()
    if q in _VISTAS_DUENO:
        session["dashboard_vista"] = q
        return q
    prev = session.get("dashboard_vista")
    if prev in _VISTAS_DUENO:
        return prev
    return "simple"


@dashboard_bp.route("/")
@requiere_login
def index():
    """Home: redirige a /operaciones (decisión TMT 2026-05-12).

    Conservamos esta vista como `dashboard.index` apuntando a /tablero abajo
    para que `url_for('dashboard.index')` en otros lados siga funcionando —
    pero la entrada principal es operaciones.
    """
    return redirect(url_for("historial.operaciones"))


@dashboard_bp.route("/tablero")
@requiere_login
def tablero():
    """Vista clásica del tablero — métricas por rol.

    Ya no es la home. Si la dueña la quiere mirar puntualmente está en
    /tablero. Para volverla home, cambiar el decorador de `index` a esta función.
    """
    rol = g.user["nombre_rol"]
    vista = _vista_preferida(rol)
    data = {"rol": rol, "error": None, "vista": vista}

    EMPTY = {"total": 0, "n": 0}
    EMPTY_WEEK = {"facturas": EMPTY, "cheques": EMPTY, "rebotes": EMPTY}

    try:
        if rol in ("Accionista", "Dueño"):  # TMT 2026-05-19 v8 rename Dueño→Accionista
            # Tablero del Dueño (2026-04-29 batch 21): minimalista. Card
            # grande arriba con el PATRIMONIO NETO como número estrella +
            # link a /informes/balance para el desglose completo. Abajo,
            # 3 chips actionable de "esta semana".
            from modules.informes import queries as informes_queries
            res = _parallel({
                "balance":          (informes_queries.informe_balance, {}),
                "resumen_semana":   (queries.resumen_semana, EMPTY_WEEK),
                "cobranza_semana":  (queries.cobranza_semana, EMPTY),
                "compras_pagar":    (queries.compras_pagar_semana, EMPTY),
            })
            data.update(res)
        elif rol == "Gerente":
            data.update(_parallel({
                "kpis":              (queries.kpis_dueno, {}),
                "deudores":          (queries.top_deudores, []),
                "cobranza_semana":   (queries.cobranza_semana, EMPTY),
                "compras_pagar":     (queries.compras_pagar_semana, EMPTY),
                "facturas_vencidas": (queries.facturas_vencidas, EMPTY),
            }))
        elif rol == "Contabilidad":
            data.update(_parallel({
                "cheques_cartera":   (queries.cheques_sin_depositar, EMPTY),
                "cheques_rebotados": (queries.cheques_rebotados_30d, EMPTY),
                "facturas_vencidas": (queries.facturas_vencidas, EMPTY),
                "cobranza_semana":   (queries.cobranza_semana, EMPTY),
            }))
        elif rol == "Ventas":
            data.update(_parallel({
                "cobros_semana":     (queries.cobros_semana, EMPTY),
                "facturas_recientes": (queries.facturas_recientes, []),
                "deudores":          (queries.top_deudores, []),
            }))
    except Exception:
        # Never leak exception text to the template — it can contain SQL
        # fragments, schema names, or row values. Log with stack, show a
        # generic message.
        _log.exception("dashboard failed for role=%s user=%s", rol, g.user.get("username") if g.get("user") else "?")
        data["error"] = "No pudimos cargar el tablero. Si el problema persiste, avisá a soporte."

    return render_template("dashboard.html", **data)
