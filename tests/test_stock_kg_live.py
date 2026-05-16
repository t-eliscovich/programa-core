"""Tests para `modules.informes.queries.stock_kg_live`.

El cálculo es trivial pero el helper tiene tres aristas que conviene fijar:
  1. Sin snapshot (DB nuevita) → todo en 0 sin levantar.
  2. Con snapshot → live = snapshot + compras desde el snapshot - ventas.
  3. Facturas anuladas (stat='Y') NO salen de stock aunque tengan kg.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _FakeDB:
    """Stub mínimo: intercepta `fetch_one` y devuelve un dict según la forma
    del SQL. No pretende ser un motor — sólo reconocer 3 consultas.
    """

    def __init__(self, *, historia=None, kg_com=Decimal("0"), kg_ven=Decimal("0")):
        self.historia = historia
        self.kg_com = kg_com
        self.kg_ven = kg_ven
        self.calls: list[tuple[str, tuple]] = []

    def fetch_one(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        self.calls.append((s, tuple(params or ())))
        if "from scintela.historia" in s and "order by fecha desc" in s:
            return self.historia
        if "from scintela.compra" in s:
            return {"kg": self.kg_com}
        if "from scintela.factura" in s:
            return {"kg": self.kg_ven}
        raise AssertionError(f"fetch_one inesperado: {s[:80]}")


@pytest.fixture
def patch_db(monkeypatch):
    """Instala _FakeDB en el módulo queries."""
    from modules.informes import queries as mod
    fake = _FakeDB()
    monkeypatch.setattr(mod.db, "fetch_one", fake.fetch_one)
    return fake, mod


def test_sin_snapshot_todo_en_cero(patch_db):
    fake, mod = patch_db
    fake.historia = None
    r = mod.stock_kg_live(hoy=date(2026, 4, 17))
    assert r["snapshot_fecha"] is None
    assert r["snapshot_kg"] == 0.0
    assert r["kg_comprados"] == 0.0
    assert r["kg_vendidos"] == 0.0
    assert r["live_kg"] == 0.0
    assert r["dias_desde_snapshot"] is None


def test_live_es_snapshot_mas_compras_menos_ventas(patch_db):
    fake, mod = patch_db
    fake.historia = {
        "fecha": date(2026, 3, 31),
        "stock": Decimal("10000"),  # historia.stock = kg, NO ustock (que es US$)
    }
    fake.kg_com = Decimal("500")
    fake.kg_ven = Decimal("300")
    r = mod.stock_kg_live(hoy=date(2026, 4, 17))
    assert r["snapshot_fecha"] == date(2026, 3, 31)
    assert r["snapshot_kg"] == 10000.0
    assert r["kg_comprados"] == 500.0
    assert r["kg_vendidos"] == 300.0
    assert r["live_kg"] == 10200.0  # 10000 + 500 - 300
    assert r["dias_desde_snapshot"] == 17


def test_rango_de_compras_y_ventas_excluye_snapshot_incluye_hoy(patch_db):
    """El range debe ser (snapshot_fecha, hoy] — strict >, inclusive <=."""
    fake, mod = patch_db
    fake.historia = {"fecha": date(2026, 3, 31), "stock": Decimal("0")}
    mod.stock_kg_live(hoy=date(2026, 4, 17))
    # Verificar parámetros: los SQLs esperan (snapshot_fecha, hoy)
    compra_call = next(c for c in fake.calls if "from scintela.compra" in c[0])
    factura_call = next(c for c in fake.calls if "from scintela.factura" in c[0])
    assert compra_call[1] == (date(2026, 3, 31), date(2026, 4, 17))
    assert factura_call[1] == (date(2026, 3, 31), date(2026, 4, 17))
    # Los SQLs usan fecha > snapshot AND fecha <= hoy
    assert "fecha > %s and fecha <= %s" in compra_call[0]
    assert "fecha > %s and fecha <= %s" in factura_call[0]


def test_factura_anulada_no_se_cuenta_en_la_query(patch_db):
    """El SQL debe excluir stat='Y'. Lo chequeamos en el SQL, no en el stub."""
    fake, mod = patch_db
    fake.historia = {"fecha": date(2026, 3, 31), "stock": Decimal("0")}
    mod.stock_kg_live(hoy=date(2026, 4, 17))
    factura_sql = next(c for c in fake.calls if "from scintela.factura" in c[0])[0]
    assert "stat is null or stat <> 'y'" in factura_sql


def test_dias_desde_snapshot_usa_hoy_param(patch_db):
    """dias_desde_snapshot debe respetar el parámetro hoy (no date.today())."""
    fake, mod = patch_db
    fake.historia = {"fecha": date(2026, 1, 1), "stock": Decimal("100")}
    r = mod.stock_kg_live(hoy=date(2026, 1, 15))
    assert r["dias_desde_snapshot"] == 14
