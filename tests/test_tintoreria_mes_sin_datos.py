"""COSTOS DE TINTORERÍA — el mes sin tintura va EN CERO, no borrado.

Dueña 2026-08-01, mirando /informes/flujo-produccion?anio=2026&mes=8 el día 1:
*"pero debería aparecer en 0 la tabla, no borrada"*.

Una tabla que desaparece no dice nada: no se distingue "todavía no tinturaron
este mes" de "se cayó el puente a formulas_app". En cero, sí lo dice.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.comparativa_tintoreria import views as v  # noqa: E402


def _sin_fuentes(monkeypatch, raw=(), raw_f=()):
    """Neutraliza las 3 fuentes: scintela.tinto, formulas y gs. producción."""
    monkeypatch.setattr(v.queries, "tinto_bajos_fuertes_por_mes",
                        lambda *a, **k: list(raw))
    monkeypatch.setattr(v.queries, "tinto_formulas_terminadas_por_mes",
                        lambda *a, **k: list(raw_f))
    monkeypatch.setattr(v.queries, "gs_produccion_tintoreria_por_mes",
                        lambda *a, **k: {})


def test_mes_sin_tintura_devuelve_la_fila_en_cero(monkeypatch):
    _sin_fuentes(monkeypatch)

    d = v._build_tintoreria_mensual(2026, 8)

    assert d["sin_datos"] is True
    assert len(d["filas"]) == 1, "la tabla no puede quedar vacía"
    fila = d["filas"][0]
    assert fila["label"] == "08/2026"
    assert fila["b_kg"] == 0.0 and fila["f_kg"] == 0.0 and fila["t_kg"] == 0.0
    assert fila["t_imp"] == 0.0
    # $/kg sin kg es None → el template imprime "—", no un 0 que miente
    assert fila["t_ukg"] is None


def test_el_mes_en_cero_no_arrastra_el_promedio_del_anio(monkeypatch):
    """⭐ El promedio se calcula sobre los meses CON data. Un agosto vacío no
    tiene que bajar el promedio 2026."""
    # julio con data (pre-corte, para que entre por scintela.tinto)
    julio = [
        {"yy": 2026, "mm": 6, "tipo": "Bajos", "kg": 100.0, "importe": 30.0},
        {"yy": 2026, "mm": 6, "tipo": "Fuertes", "kg": 100.0, "importe": 90.0},
    ]
    _sin_fuentes(monkeypatch, raw=julio)

    con_datos = v._build_tintoreria_mensual(2026, 6)
    sin_datos = v._build_tintoreria_mensual(2026, 8)

    # el promedio del año es el MISMO se mire el mes que se mire
    assert sin_datos["promedio"]["t_ukg"] == con_datos["promedio"]["t_ukg"]
    assert sin_datos["promedio"]["t_kg"] == 200.0
    assert sin_datos["sin_datos"] is True
    assert con_datos["sin_datos"] is False


def test_mes_con_tintura_no_marca_sin_datos(monkeypatch):
    filas = [
        {"yy": 2026, "mm": 5, "tipo": "Bajos", "kg": 10.0, "importe": 3.0},
        {"yy": 2026, "mm": 5, "tipo": "Fuertes", "kg": 10.0, "importe": 9.0},
    ]
    _sin_fuentes(monkeypatch, raw=filas)

    d = v._build_tintoreria_mensual(2026, 5)

    assert d["sin_datos"] is False
    assert d["filas"][0]["t_kg"] == 20.0


def test_el_mes_seleccionado_viaja_al_template(monkeypatch):
    """El cartel necesita saber QUÉ mes está vacío."""
    _sin_fuentes(monkeypatch)

    d = v._build_tintoreria_mensual(2026, 8)

    assert d["mes"] == 8
    assert d["anio"] == 2026
