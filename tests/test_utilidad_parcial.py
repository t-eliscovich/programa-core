"""Bug #5 — la "Utilidad Real" del mes en curso se marca como PARCIAL.

`ur = (PATR − PATANT) + URET` sólo cuadra al CIERRE de mes; a principio de mes
sale negativa y confunde. La fila ahora lleva `parcial=True` + `parcial_dias`
para que el template muestre un badge "parcial · N días". TMT 2026-07-11.
"""

from __future__ import annotations

from modules.informes import queries


def _tabla(*, dia_actual, patr, patant):
    return queries.resultados_costos_tabla(
        venta_kg=0.0, venta_us=0.0, dia_actual=dia_actual, mp_ukg=0.0,
        v1=0.0, v2=0.0, v3=0.0, dtj=0.0, kg_tejidos=0.0,
        v4=0.0, v5=0.0, v6=0.0, dcc=0.0, itin=0.0, ktint=0.0,
        v7=0.0, v8=0.0, v9=0.0, deprcar=0.0,
        patr=patr, patant=patant, uret=0.0,
    )


def _fila(tabla, label):
    return next(f for f in tabla if f.get("label") == label)


def test_utilidad_real_marcada_parcial_con_dias():
    ur = _fila(_tabla(dia_actual=8, patr=100.0, patant=500.0), "Utilidad Real")
    assert ur["parcial"] is True
    assert ur["parcial_dias"] == 8
    # A principio de mes (patr<patant) sale negativa — el caso que confundía.
    assert ur["us"] < 0
    assert "cierre" in ur["ayuda"].lower()


def test_utilidad_proyectada_no_se_marca_parcial():
    # Sólo la Utilidad Real (PATR−PATANT) lleva la marca; la Proyectada ya es
    # explícitamente una proyección.
    up = _fila(_tabla(dia_actual=8, patr=100.0, patant=500.0), "Utilidad Proyectada")
    assert not up.get("parcial")
