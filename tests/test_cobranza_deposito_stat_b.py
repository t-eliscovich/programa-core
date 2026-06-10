"""Regresión TMT 2026-06-10 — depósito directo en Nueva Cobranza.

Con banco de depósito (90 DEP.PICH / 91 / 99 EFECTIVO), `crear()` flipea el
cheque a stat='B'. El flujo de Nueva Cobranza aplica ese cheque a facturas
EN LA MISMA transacción → `aplicar_a_factura` lo rechazaba con
"stat='B' no se puede aplicar" y la cobranza entera fallaba (y elegir 'Z'
en el dropdown daba el mismo error, porque el auto-flip lo volvía a 'B').

Fix: `permitir_depositado=True` (sólo lo pasa el flujo de creación).
El guard #26 sigue intacto por default para cheques viejos ya depositados.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _DBStub:
    """fetch_one despacha por tabla; execute acumula llamadas."""

    def __init__(self):
        self.executes: list[str] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque" in s:
            return {
                "id_cheque": 59563,
                "codigo_cli": "GAM",
                "no_banco": 90,
                "importe": 5.25,
                "stat": "B",
                "fecha": None,
            }
        if "from scintela.factura" in s:
            return {
                "id_factura": 1,
                "numf": 169632,
                "importe": 5.57,
                "abono": 0.32,
                "saldo": 5.25,
                "stat": "Z",
            }
        return None

    def execute(self, sql, params=None, conn=None):
        self.executes.append(" ".join(sql.split()).lower())
        return 1


def _patch(monkeypatch):
    from modules.cheques import queries

    stub = _DBStub()
    monkeypatch.setattr(queries, "db", stub)
    import mov_doble

    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: None)
    return queries, stub


def test_guard_sigue_rechazando_stat_b_por_default(monkeypatch):
    queries, _ = _patch(monkeypatch)
    with pytest.raises(ValueError, match="no se puede aplicar"):
        queries.aplicar_a_factura(
            id_cheque=59563,
            aplicaciones=[{"id_fact": 1, "importe": 5.25}],
            conn=object(),
        )


def test_permitir_depositado_aplica_cheque_b_recien_creado(monkeypatch):
    queries, stub = _patch(monkeypatch)
    r = queries.aplicar_a_factura(
        id_cheque=59563,
        aplicaciones=[{"id_fact": 1, "importe": 5.25}],
        conn=object(),
        permitir_depositado=True,
    )
    assert r["n"] == 1
    assert abs(r["total_aplicado"] - 5.25) < 0.005
    # Insertó chequesxfact y actualizó la factura.
    assert any("insert into scintela.chequesxfact" in s for s in stub.executes)
    assert any("update scintela.factura" in s for s in stub.executes)
