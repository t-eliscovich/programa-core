"""Retiro OP en NEGATIVO = aporte de capital (dueña 2026-07-20, paridad dBase).

crear_op acepta monto negativo (solo bloquea 0): el retiro negativo baja
dividendos (URET) y, imputado a una línea OP, agranda el crédito
(posdat.importe += monto). Stub mock — sin DB real."""
from __future__ import annotations

import contextlib
import sys

import pytest

from tests.test_logicas_contables import _DBStub  # stub reusable


@pytest.fixture
def stub(monkeypatch):
    import db
    s = _DBStub()
    for fn in ("fetch_one", "fetch_all", "execute", "execute_returning", "tx"):
        monkeypatch.setattr(db, fn, getattr(s, fn))
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **k: None)
    m = sys.modules.get("modules.retiros.queries")
    if m and hasattr(m, "asegurar_fecha_abierta"):
        monkeypatch.setattr(m, "asegurar_fecha_abierta", lambda *a, **k: None)
    return s


def test_crear_op_negativo_es_aporte(stub, monkeypatch):
    from modules.retiros import queries as q
    import mov_doble
    monkeypatch.setattr(mov_doble, "registrar", lambda *a, **k: None)
    stub.execute_returning_results.append({"id_retiro": 77})
    r = q.crear_op(monto=-1.00, usuario="t")
    assert r["monto"] == -1.00
    assert "APORTE" in r["concepto"].upper()
    ins = next(e for e in stub.executes if "insert into scintela.retiros" in e[0].lower())
    assert -1.00 in ins[1]


def test_crear_op_cero_rechaza(stub):
    from modules.retiros import queries as q
    with pytest.raises(ValueError, match="cero"):
        q.crear_op(monto=0, usuario="t")


def test_crear_op_positivo_sigue_siendo_rr(stub, monkeypatch):
    from modules.retiros import queries as q
    import mov_doble
    monkeypatch.setattr(mov_doble, "registrar", lambda *a, **k: None)
    stub.execute_returning_results.append({"id_retiro": 78})
    r = q.crear_op(monto=100.0, usuario="t")
    assert r["monto"] == 100.0
    assert r["concepto"].upper().startswith("RR")
