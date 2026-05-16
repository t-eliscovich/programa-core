"""Tests para modules.facturas.queries.editar() — paridad con MODIFICA.PRG.

No tocan Postgres. Reglas verificadas:
  - editar abono recompute saldo + recalcula stat (Z/A/T).
  - condic ' '→'C' aplica 5% pronto pago (importe×0.95).
  - condic 'C'→' ' lo revierte (importe/0.95).
  - primera vez stat='T' stampa vencim=CURRENT_DATE.
  - factura ya anulada (X/Y) no se puede editar.
  - abono > importe falla.
  - abono negativo falla.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest


class _FakeFacturaDB:
    """Stub para la tabla scintela.factura."""

    def __init__(self, fact: dict):
        self.fact = dict(fact)
        self.executes: list[tuple[str, tuple]] = []

    def fetch_one(self, sql: str, params: Any = None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.factura where id_factura" in s:
            return dict(self.fact) if self.fact else None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql: str, params: Any = None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        s = " ".join((sql or "").split()).lower()
        if "update scintela.factura" in s:
            # Aplicar el SET — primero N campos posicionales, último id_factura
            # No es perfecto pero suficiente para verificar el escenario
            return 1
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        return None

    def apply_to(self, monkeypatch, db_mod):
        monkeypatch.setattr(db_mod, "fetch_one", self.fetch_one)
        monkeypatch.setattr(db_mod, "fetch_all", self.fetch_all)
        monkeypatch.setattr(db_mod, "execute", self.execute)
        monkeypatch.setattr(db_mod, "execute_returning", self.execute_returning)


@pytest.fixture(autouse=True)
def _no_periodo_guard(monkeypatch):
    """Stub asegurar_fecha_abierta — no consulta DB en tests."""
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda f: None)


def test_editar_abono_recalcula_saldo_y_stat_a(monkeypatch):
    import db as db_mod
    from modules.facturas import queries

    fake = _FakeFacturaDB({
        "id_factura": 1, "fecha": date(2026, 4, 30),
        "importe": 1000, "abono": 0, "saldo": 1000,
        "stat": "Z", "condic": "", "vencimiento": date(2026, 5, 30),
    })
    fake.apply_to(monkeypatch, db_mod)

    res = queries.editar(1, abono=300, usuario="tmt")
    assert res["abono"] == 300
    assert res["saldo"] == 700.0
    assert res["stat_nuevo"] == "A"  # abono parcial


def test_editar_abono_total_pasa_a_t_y_stampa_vencim(monkeypatch):
    import db as db_mod
    from modules.facturas import queries

    fake = _FakeFacturaDB({
        "id_factura": 1, "fecha": date(2026, 4, 30),
        "importe": 500, "abono": 0, "saldo": 500,
        "stat": "Z", "condic": "", "vencimiento": date(2026, 5, 30),
    })
    fake.apply_to(monkeypatch, db_mod)

    res = queries.editar(1, abono=500, usuario="tmt")
    assert res["saldo"] == 0.0
    assert res["stat_nuevo"] == "T"
    assert res["vencimiento_stamp"] is True
    # El UPDATE debe incluir vencimiento=CURRENT_DATE
    sql_emitido = fake.executes[-1][0].lower()
    assert "vencimiento=current_date" in " ".join(sql_emitido.split())


def test_editar_abono_cero_vuelve_a_z(monkeypatch):
    import db as db_mod
    from modules.facturas import queries

    fake = _FakeFacturaDB({
        "id_factura": 1, "fecha": date(2026, 4, 30),
        "importe": 1000, "abono": 200, "saldo": 800,
        "stat": "A", "condic": "",
    })
    fake.apply_to(monkeypatch, db_mod)

    res = queries.editar(1, abono=0, usuario="tmt")
    assert res["abono"] == 0
    assert res["saldo"] == 1000.0
    assert res["stat_nuevo"] == "Z"


def test_condic_blanco_a_c_aplica_5_pct_descuento(monkeypatch):
    """Paridad MODIFICA.PRG L435-442: ' '→'C' multiplica importe por 0.95."""
    import db as db_mod
    from modules.facturas import queries

    fake = _FakeFacturaDB({
        "id_factura": 1, "fecha": date(2026, 4, 30),
        "importe": 1000, "abono": 0, "saldo": 1000,
        "stat": "Z", "condic": "",
    })
    fake.apply_to(monkeypatch, db_mod)

    res = queries.editar(1, condic="C", usuario="tmt")
    assert res["importe"] == 950.0   # 1000 * 0.95
    assert res["saldo"] == 950.0
    assert res["condic_nueva"] == "C"


def test_condic_c_a_blanco_revierte_descuento(monkeypatch):
    """Paridad: 'C'→' ' divide importe por 0.95 (vuelve al original)."""
    import db as db_mod
    from modules.facturas import queries

    fake = _FakeFacturaDB({
        "id_factura": 1, "fecha": date(2026, 4, 30),
        "importe": 950, "abono": 0, "saldo": 950,
        "stat": "Z", "condic": "C",
    })
    fake.apply_to(monkeypatch, db_mod)

    res = queries.editar(1, condic="", usuario="tmt")
    assert abs(res["importe"] - 1000.0) < 0.01  # 950 / 0.95
    assert res["condic_nueva"] == ""


def test_factura_anulada_no_se_puede_editar(monkeypatch):
    import db as db_mod
    from modules.facturas import queries

    fake = _FakeFacturaDB({
        "id_factura": 1, "fecha": date(2026, 4, 30),
        "importe": 1000, "abono": 0, "saldo": 1000,
        "stat": "X", "condic": "",
    })
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="anulada"):
        queries.editar(1, abono=100, usuario="tmt")


def test_abono_excede_importe_falla(monkeypatch):
    import db as db_mod
    from modules.facturas import queries

    fake = _FakeFacturaDB({
        "id_factura": 1, "fecha": date(2026, 4, 30),
        "importe": 500, "abono": 0, "saldo": 500,
        "stat": "Z", "condic": "",
    })
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="excede"):
        queries.editar(1, abono=600, usuario="tmt")


def test_abono_negativo_falla(monkeypatch):
    import db as db_mod
    from modules.facturas import queries

    fake = _FakeFacturaDB({
        "id_factura": 1, "fecha": date(2026, 4, 30),
        "importe": 500, "abono": 0, "saldo": 500,
        "stat": "Z", "condic": "",
    })
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError, match="negativo"):
        queries.editar(1, abono=-50, usuario="tmt")


def test_factura_inexistente(monkeypatch):
    import db as db_mod
    from modules.facturas import queries

    fake = _FakeFacturaDB({})
    fake.fact = {}  # vacío → fetch_one devuelve None
    monkeypatch.setattr(db_mod, "fetch_one", lambda *a, **kw: None)
    monkeypatch.setattr(db_mod, "execute", lambda *a, **kw: 1)

    with pytest.raises(ValueError, match="inexistente"):
        queries.editar(999, abono=100, usuario="tmt")
