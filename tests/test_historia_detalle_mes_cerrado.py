"""Reconstrucción de las columnas de DETALLE de `scintela.historia` de un mes
YA CERRADO (kcom/ucom/ktej/utej/ktin/utin/gasto/gstotal/kvent/uvent), para
cuando el cierre cayó en la rama `as_of` y esas columnas quedaron sin fuente.

Tamara 2026-09-01, incidente del cierre de agosto: "no de julio. del cierre.
busca y ponelo bien" — rechazó explícitamente usar julio como placeholder.
Esta función arma agosto real a partir de tablas propias de PC filtradas por
fecha (`gastos_xgast_v1_a_v9_mes`, `amortizaciones_mensuales`,
`tejido_mes_componentes`, `compras_mes_corriente`,
`ventas_mes_corriente_resultado`, `tinto_mes_componentes`).

`utej`/`utin`/`gasto` tienen que dar IDÉNTICO a `_gastos_mes_anterior_componentes`
(la misma fórmula que ya muestra /informes/gastos, ya verificada en
producción: 164.551,72 para tejeduría de agosto). `ktin` sale de KTINT real
(scintela.tinto + formulas_app, date-scoped) -- no del kg de tejido (bug que
tenía el primer borrador de esta función, corregido antes de deployar).
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from modules.informes import queries


def _patch_hoy(monkeypatch, y, m, d):
    monkeypatch.setattr(queries, "today_ec", lambda: date(y, m, d))


def test_meses_atras_se_calcula_bien_y_rechaza_mes_no_cerrado(monkeypatch):
    _patch_hoy(monkeypatch, 2026, 9, 1)
    with patch.object(queries, "gastos_xgast_v1_a_v9_mes", return_value={}), \
         patch.object(queries, "amortizaciones_mensuales", return_value={}), \
         patch.object(queries, "tejido_mes_componentes", return_value={}) as m_tej, \
         patch.object(queries, "compras_mes_corriente", return_value={}) as m_mp, \
         patch.object(queries, "ventas_mes_corriente_resultado", return_value={}) as m_vt, \
         patch.object(queries, "tinto_mes_componentes", return_value={}) as m_tin:
        out = queries.historia_detalle_mes_cerrado(2026, 8)
        m_tej.assert_called_once_with(meses_atras=1)
        m_mp.assert_called_once_with(meses_atras=1)
        m_vt.assert_called_once_with(meses_atras=1)
        m_tin.assert_called_once_with(2026, 8)
    assert out["ok"] is True

    # el mes en curso (o futuro) no es "ya cerrado" -> se rechaza.
    out2 = queries.historia_detalle_mes_cerrado(2026, 9)
    assert out2["ok"] is False


def test_campos_usan_las_mismas_formulas_que_gastos_mes_anterior(monkeypatch):
    """utej/utin/gasto deben coincidir con `_gastos_mes_anterior_componentes`
    (mismo cálculo verificado en producción para agosto: tej=164.551,72)."""
    _patch_hoy(monkeypatch, 2026, 9, 1)
    v = {"v1": 50000.0, "v2": 30000.0, "v3": 10000.0,
         "v4": 100000.0, "v5": 50000.0, "v6": 20000.0,
         "v7": 80000.0, "v8": 40000.0, "v9": 10000.0}
    a = {"dtj": 1000.0, "dcc": 2000.0, "deprcar": 500.0}
    tej = {"us_externo": 40649.60, "kg_total": 12345.0}
    mp = {"importe": 5555.0, "kg": 999.0}
    vt = {"importe": 7777.0, "kg": 888.0}
    tin = {"itin": 111.0, "ktint": 2222.0, "kr": 333.0}

    with patch.object(queries, "gastos_xgast_v1_a_v9_mes", return_value=v), \
         patch.object(queries, "amortizaciones_mensuales", return_value=a), \
         patch.object(queries, "tejido_mes_componentes", return_value=tej), \
         patch.object(queries, "compras_mes_corriente", return_value=mp), \
         patch.object(queries, "ventas_mes_corriente_resultado", return_value=vt), \
         patch.object(queries, "tinto_mes_componentes", return_value=tin):
        out = queries.historia_detalle_mes_cerrado(2026, 8)

    campos = out["campos"]
    assert campos["utej"] == 50000.0 + 30000.0 + 10000.0 + 1000.0 + 40649.60
    assert campos["utin"] == 100000.0 + 50000.0 + 20000.0 + 2000.0
    assert campos["gasto"] == 80000.0 + 40000.0 + 10000.0 + 500.0
    assert campos["gstotal"] == campos["utej"] + campos["utin"] + campos["gasto"]
    assert campos["ucom"] == 5555.0
    assert campos["kcom"] == 999.0
    assert campos["kvent"] == 888.0
    assert campos["uvent"] == 7777.0
    assert campos["ktej"] == 12345.0
    # ktin sale de KTINT real (tinto_mes_componentes), NO del kg de tejido.
    assert campos["ktin"] == 2222.0
    assert campos["ktin"] != campos["ktej"]


def test_tinto_mes_componentes_no_toca_dbase_para_un_mes_post_corte(monkeypatch):
    """Agosto 2026 es 100% posterior a CORTE_TINTURA (2026-07-01): la parte
    scintela.tinto no debe correr ninguna query, todo sale de formulas_app."""
    class _NoDbCalls:
        def fetch_one(self, *a, **kw):
            raise AssertionError("no debería leer scintela.tinto para agosto")

    fake_ordenes = []

    class _FakeSvc:
        @staticmethod
        def tinto_equiv_formulas(desde, hasta, excluir_lavados=False):
            assert desde == date(2026, 8, 1)
            assert hasta == date(2026, 8, 31)
            return fake_ordenes

    with patch.object(queries, "db", _NoDbCalls()), \
         patch.dict("sys.modules", {"modules.tintura.service": _FakeSvc}):
        out = queries.tinto_mes_componentes(2026, 8)
    assert out == {"itin": 0.0, "ktint": 0.0, "kr": 0.0}


def test_tinto_mes_componentes_suma_formulas_app_excluyendo_lavados(monkeypatch):
    class _Orden:
        def __init__(self, importe, color, kg, kgn):
            self.importe, self.color, self.kg, self.kgn = importe, color, kg, kgn

    fake_ordenes = [
        _Orden(100.0, "AZUL", 50.0, 45.0),
        _Orden(20.0, "LAVADO MAQ", 30.0, 28.0),  # lavado: NO cuenta kg/kr, SI itin
    ]

    class _FakeSvc:
        @staticmethod
        def tinto_equiv_formulas(desde, hasta, excluir_lavados=False):
            return fake_ordenes

    class _NoDbCalls:
        def fetch_one(self, *a, **kw):
            raise AssertionError("no debería leer scintela.tinto para agosto")

    with patch.object(queries, "db", _NoDbCalls()), \
         patch.dict("sys.modules", {"modules.tintura.service": _FakeSvc}):
        out = queries.tinto_mes_componentes(2026, 8)

    assert out["itin"] == 120.0  # itin suma TODAS las filas, incl. lavados
    assert out["ktint"] == 50.0  # ktint excluye lavados
    assert out["kr"] == 45.0     # kr excluye lavados
