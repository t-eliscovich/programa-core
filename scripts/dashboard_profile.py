#!/usr/bin/env python3
"""Benchmark del dashboard Dueño: parallel vs sequential.

Uso:
    python scripts/dashboard_profile.py              # 10 iter c/ modo
    python scripts/dashboard_profile.py --iter 30    # más precisión
    python scripts/dashboard_profile.py --only seq   # sólo secuencial
    python scripts/dashboard_profile.py --warmup 3   # warm-up iter descartadas

Corre cada query del dashboard del Dueño en modo paralelo y secuencial varias
veces contra la DB configurada en .env y reporta:

    - min / mediana / p95 / max para cada modo
    - overhead real del ThreadPoolExecutor
    - desglose por query: tiempo medio e identificación del cuello de botella

Decisión:
    - Si mediana(sequential) ≲ mediana(parallel) + 10ms, simplificar a
      secuencial. El pool aporta complejidad sin beneficio real.
    - Si mediana(parallel) es >2x más rápido, mantener pool.
    - Si alguna query individual > 200ms, no es problema del pool — indexar.

Este script NO modifica datos. Sólo lee y reporta.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _pctl(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    i = int(q * (len(s) - 1))
    return s[i]


def _resumen(samples: list[float]) -> dict:
    if not samples:
        return {"n": 0}
    return {
        "n": len(samples),
        "min": min(samples),
        "p50": statistics.median(samples),
        "p95": _pctl(samples, 0.95),
        "max": max(samples),
        "mean": statistics.mean(samples),
    }


def _fmt(d: dict) -> str:
    if not d.get("n"):
        return "—"
    return (f"min={d['min']:.0f}ms  p50={d['p50']:.0f}ms  "
            f"p95={d['p95']:.0f}ms  max={d['max']:.0f}ms  mean={d['mean']:.0f}ms")


def bench_mode(mode: str, iterations: int, warmup: int) -> tuple[list[float], dict[str, list[float]]]:
    """Corre `iterations` iteraciones con el mode dado.

    Devuelve (samples_wall_ms, {task_name: [ms]}).
    """
    os.environ["DASHBOARD_MODE"] = mode
    # import tardío para que el cambio de env tome efecto
    from modules.dashboard import queries
    from modules.dashboard.views import _parallel

    EMPTY = {"total": 0, "n": 0}
    EMPTY_WEEK = {"facturas": EMPTY, "cheques": EMPTY, "rebotes": EMPTY}
    tasks = {
        "kpis":             (queries.kpis_dueno, {}),
        "deudores":         (queries.top_deudores, []),
        "flujo_30d":        (queries.flujo_30_dias, []),
        "resumen_semana":   (queries.resumen_semana, EMPTY_WEEK),
        "actividad":        (lambda: queries.actividad_reciente(8), []),
        "cobranza_semana":  (queries.cobranza_semana, EMPTY),
        "compras_pagar":    (queries.compras_pagar_semana, EMPTY),
        "mes_actual":       (queries.saldo_mes_en_curso, {
            "facturado": 0.0, "cobrado_cheques": 0.0, "neto": 0.0,
            "n_facturas": 0, "n_cheques": 0, "mes_desde": None,
        }),
    }

    # Warm-up — cargar plan cache y índices en memoria
    for _ in range(warmup):
        _parallel(tasks)

    wall_samples: list[float] = []
    per_task: dict[str, list[float]] = {k: [] for k in tasks}

    # Monkeypatch _run_with_fallback una sola vez para recolectar timings
    # por tarea. Se define `_wrap` como closure sobre `original` y
    # `collected` fuera del loop para evitar B023.
    import modules.dashboard.views as v
    original = v._run_with_fallback
    collected: dict[str, float] = {}

    def _wrap(fn_default, name=""):
        res, ms, err = original(fn_default, name)
        collected[name] = ms
        return res, ms, err

    v._run_with_fallback = _wrap
    try:
        for _ in range(iterations):
            collected.clear()
            t0 = time.perf_counter()
            _parallel(tasks)
            wall_samples.append((time.perf_counter() - t0) * 1000)
            for k, ms in collected.items():
                per_task[k].append(ms)
    finally:
        v._run_with_fallback = original

    return wall_samples, per_task


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iter", type=int, default=10, help="iteraciones por modo (default 10)")
    ap.add_argument("--warmup", type=int, default=2, help="iteraciones de warm-up (default 2)")
    ap.add_argument("--only", choices=["parallel", "sequential", "both"], default="both")
    args = ap.parse_args()

    import db
    db.init_pool()

    modes = ["parallel", "sequential"] if args.only == "both" else [args.only]
    wall_by_mode: dict[str, list[float]] = {}
    per_task_by_mode: dict[str, dict[str, list[float]]] = {}

    for mode in modes:
        print(f"\n--- benchmark modo={mode!r}  iter={args.iter}  warmup={args.warmup} ---")
        samples, per_task = bench_mode(mode, args.iter, args.warmup)
        wall_by_mode[mode] = samples
        per_task_by_mode[mode] = per_task
        print(f"wall (incl. overhead): {_fmt(_resumen(samples))}")

    # Comparación lado a lado
    if len(modes) == 2:
        p_med = _resumen(wall_by_mode["parallel"])["p50"]
        s_med = _resumen(wall_by_mode["sequential"])["p50"]
        gap = s_med - p_med
        print("\n=== comparación (p50) ===")
        print(f"  parallel   : {p_med:.0f}ms")
        print(f"  sequential : {s_med:.0f}ms")
        print(f"  gap        : {gap:+.0f}ms  ({'secuencial más lento' if gap > 0 else 'paralelo más lento'})")
        if abs(gap) < 10:
            print("  → simplificar a secuencial. El pool no aporta beneficio claro.")
        elif gap > 50:
            print("  → mantener paralelo. Hay paralelismo real en las queries.")
        else:
            print("  → decisión marginal. Probar con tráfico real o reducir queries.")

    # Desglose por tarea (en el último modo corrido)
    last_mode = modes[-1]
    print(f"\n=== desglose por tarea (modo={last_mode!r}, p50) ===")
    tasks_sorted = sorted(
        per_task_by_mode[last_mode].items(),
        key=lambda kv: -statistics.median(kv[1]) if kv[1] else 0,
    )
    for name, samples in tasks_sorted:
        if samples:
            print(f"  {name:<20}  {_fmt(_resumen(samples))}")

    # Cuello de botella: >200ms mediana = indexar
    heavy = [
        (n, statistics.median(ms)) for n, ms in per_task_by_mode[last_mode].items()
        if ms and statistics.median(ms) > 200
    ]
    if heavy:
        print("\n!! Queries con p50 > 200ms — candidatas a indexar o reescribir:")
        for n, m in heavy:
            print(f"   - {n}: p50={m:.0f}ms")


if __name__ == "__main__":
    main()
