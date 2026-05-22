"""Tests de resultados_costos_tabla — la tabla RESULTADOS del balance.

Funcion pura: no necesita Postgres. Rediseno Federico 2026-05-21,
definido fila por fila con el dueno.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.informes.queries import resultados_costos_tabla


def _tabla(**over):
    base = dict(
        venta_kg=200000.0,
        venta_us=1000000.0,
        dia_actual=20,
        mp_ukg=2.911,
        v1=10000.0, v2=5000.0, v3=3000.0, dtj=2000.0,
        kg_tejidos=180000.0,
        v4=8000.0, v5=4000.0, v6=2000.0, dcc=1000.0,
        itin=120000.0, ktint=197000.0,
        v7=15000.0, v8=6000.0, v9=4000.0, deprcar=3000.0,
        patr=21000000.0, patant=20000000.0, uret=150000.0,
    )
    base.update(over)
    return resultados_costos_tabla(**base)


def _row(tabla, label):
    return next(r for r in tabla if r.get("label") == label)


def test_venta_precio_promedio():
    """Venta: u$/kg = u$s / kg."""
    v = _row(_tabla(), "Venta")
    assert v["kg"] == 200000.0
    assert v["us"] == 1000000.0
    assert abs(v["ukg"] - 5.0) < 1e-9


def test_proyeccion_regla_de_3_al_dia_30():
    """Proyeccion: kg vendidos escalados al dia 30, mismo precio."""
    p = _row(_tabla(dia_actual=20), "Proyección")
    assert abs(p["kg"] - 300000.0) < 1e-6      # 200000 * 30 / 20
    assert abs(p["us"] - 1500000.0) < 1e-6
    assert abs(p["ukg"] - 5.0) < 1e-9


def test_materia_prima_solo_unitario():
    """Materia Prima: solo el costo unitario; kg y u$s vacios."""
    mp = _row(_tabla(), "Materia Prima")
    assert mp["kg"] is None
    assert mp["us"] is None
    assert abs(mp["ukg"] - 2.911) < 1e-9


def test_tejeduria_costo_sobre_kg_tejidos():
    """Tejeduria: (V1+V2+V3+dtj) / kg tejidos."""
    t = _row(_tabla(), "Tejeduría")
    assert abs(t["us"] - 20000.0) < 1e-6       # 10000+5000+3000+2000
    assert abs(t["ukg"] - 20000.0 / 180000.0) < 1e-9


def test_tintoreria_y_colorantes_sobre_ktint():
    """Tintoreria y Colorantes dividen por KTINT (kg que salen)."""
    tab = _tabla()
    tin = _row(tab, "Tintorería")
    col = _row(tab, "Colorantes/Quím.")
    assert abs(tin["us"] - 15000.0) < 1e-6     # 8000+4000+2000+1000
    assert abs(tin["ukg"] - 15000.0 / 197000.0) < 1e-9
    assert abs(col["us"] - 120000.0) < 1e-6
    assert abs(col["ukg"] - 120000.0 / 197000.0) < 1e-9


def test_subtotal_aplica_4_5_pct():
    """Subtotal = 1.045 * (MP + Tejeduria + Tintoreria + Colorantes)."""
    tab = _tabla()
    mp = _row(tab, "Materia Prima")["ukg"]
    tej = _row(tab, "Tejeduría")["ukg"]
    tin = _row(tab, "Tintorería")["ukg"]
    col = _row(tab, "Colorantes/Quím.")["ukg"]
    sub = _row(tab, "Subtotal +4.5%")
    assert abs(sub["ukg"] - 1.045 * (mp + tej + tin + col)) < 1e-9
    assert sub["kg"] is None and sub["us"] is None


def test_costo_total_subtotal_mas_admin():
    """Costo Total u$/kg = Subtotal + Administracion."""
    tab = _tabla()
    sub = _row(tab, "Subtotal +4.5%")["ukg"]
    adm = _row(tab, "Administración")
    ct = _row(tab, "Costo Total")
    assert abs(adm["ukg"] - 28000.0 / 200000.0) < 1e-9
    assert abs(ct["ukg"] - (sub + adm["ukg"])) < 1e-9
    assert abs(ct["us"] - 200000.0 * ct["ukg"]) < 1e-6
    assert ct["kg"] is None


def test_utilidad_no_estandarizada_precio_menos_costo():
    """Utilidad no estandarizada u$/kg = precio - Costo Total."""
    tab = _tabla()
    ct = _row(tab, "Costo Total")["ukg"]
    ue = _row(tab, "Utilidad no estandarizada")
    assert abs(ue["ukg"] - (5.0 - ct)) < 1e-9
    assert abs(ue["us"] - 200000.0 * ue["ukg"]) < 1e-6


def test_utilidad_real_delta_patrimonio_mas_dividendos():
    """Utilidad Real u$s = (patr - patant) + dividendos del mes."""
    ur = _row(_tabla(), "Utilidad Real")
    assert abs(ur["us"] - 1150000.0) < 1e-6    # 1.000.000 + 150.000
    assert abs(ur["ukg"] - 1150000.0 / 200000.0) < 1e-9


def test_sin_ventas_no_rompe():
    """venta_kg = 0 no debe lanzar ZeroDivisionError."""
    tab = _tabla(venta_kg=0.0, venta_us=0.0)
    assert _row(tab, "Venta")["ukg"] == 0.0
    assert len(tab) == 12


def test_seccion_costos_presente():
    """Hay una fila divisoria COSTOS de clase seccion."""
    assert _row(_tabla(), "COSTOS")["clase"] == "seccion"
