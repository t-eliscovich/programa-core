"""Reconstrucción de maquinaria/realty/stock de `scintela.historia` para un
mes YA CERRADO -- complemento de `historia_detalle_mes_cerrado`, que deja
afuera estas 3 columnas a propósito.

Tamara 2026-09-01, segunda vuelta del incidente de agosto: "ventas, esta
mal el numero en historia" -- después de aplicar el detalle real seguían
con julio pisado en anticipos/maquinaria/realty/stock, y Total Activo no
cerraba contra Pasivo+Patrimonio (faltaban ~$976K). La cuenta correcta es
`SUM(inicial-amortizac)` de `scintela.activos` SIN restar ningún
coeficiente adicional -- pero SÓLO si `actualizar_amortizacion()` ya
corrió para el mes target (si no, hay que usar la rama as_of con su
coeficiente prorateado, que este helper deliberadamente NO reemplaza).
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from modules.informes import queries


def _patch_hoy(monkeypatch, y, m, d):
    monkeypatch.setattr(queries, "today_ec", lambda: date(y, m, d))


class _FakeDb:
    """Devuelve fetch_one() en el orden que la función los pide:
    1) MIN(ult_mes_amortizado)  2) SUM activos  3) iniciales."""

    def __init__(self, respuestas):
        self._respuestas = list(respuestas)
        self.llamadas = 0

    def fetch_one(self, *a, **kw):
        r = self._respuestas[self.llamadas]
        self.llamadas += 1
        return r


def test_rechaza_mes_no_cerrado(monkeypatch):
    _patch_hoy(monkeypatch, 2026, 9, 1)
    out = queries.historia_balance_fijo_mes_cerrado(2026, 9)
    assert out["ok"] is False


def test_rechaza_si_la_amortizacion_del_mes_target_todavia_no_corrio(monkeypatch):
    """Si ult_mes_amortizado <= yyyymm_target, todavía estamos "dentro" del
    mes en curso desde el punto de vista de la amortización -- usar esta
    cuenta doble-contaría la cuota (el mismo bug que rompió agosto la
    primera vez, vía informe_balance_as_of)."""
    _patch_hoy(monkeypatch, 2026, 9, 1)
    fake = _FakeDb([{"m": 202608}])  # todavía no pasó de agosto
    with patch.object(queries, "db", fake):
        out = queries.historia_balance_fijo_mes_cerrado(2026, 8)
    assert out["ok"] is False
    assert "todavía no corrió" in out["razon"]
    assert fake.llamadas == 1  # no debería ni consultar activos/iniciales


def test_reconstruye_maquinaria_realty_stock_cuando_ya_corrio_la_amortizacion(monkeypatch):
    _patch_hoy(monkeypatch, 2026, 9, 1)
    fake = _FakeDb([
        {"m": 202609},  # ult_mes_amortizado ya avanzó más allá de agosto
        {"maquinaria": 1038550.0, "realty": 2364564.0},
        {"hilado": 1974107.41, "tejido": 292146.94, "terminado": 321062.96},
    ])
    with patch.object(queries, "db", fake):
        out = queries.historia_balance_fijo_mes_cerrado(2026, 8)
    assert out["ok"] is True
    campos = out["campos"]
    assert campos["maquinaria"] == 1038550.0
    assert campos["realty"] == 2364564.0
    assert campos["stock"] == 1974107.41 + 292146.94 + 321062.96
    assert "maquinaria" in out["fuente"] and "realty" in out["fuente"] and "stock" in out["fuente"]


def test_maneja_filas_vacias_sin_reventar(monkeypatch):
    _patch_hoy(monkeypatch, 2026, 9, 1)
    fake = _FakeDb([{"m": 202609}, {}, {}])
    with patch.object(queries, "db", fake):
        out = queries.historia_balance_fijo_mes_cerrado(2026, 8)
    assert out["ok"] is True
    assert out["campos"] == {"maquinaria": 0.0, "realty": 0.0, "stock": 0.0}
