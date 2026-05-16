"""Tests para el flag `es_anticipo` en cheques.crear (CONCEPTO=9999 legacy).

Cuando `es_anticipo=True`:
1. Se inserta el cheque normal.
2. Se inserta un "espejo" con importe negativo, stat='Z', id_cheque_padre=cheque_principal.

Cuando `es_anticipo=False` (default):
3. Solo se inserta el cheque normal — sin espejo.

Cuando `es_anticipo=True` pero `importe<=0`:
4. NO se inserta espejo (no tiene sentido).
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _DBStub:
    def __init__(self):
        self.execute_returning_calls: list[tuple] = []
        self._next_id = 100

    def fetch_one(self, sql, params=None):
        return None

    def execute(self, sql, params=None, conn=None):
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.execute_returning_calls.append((sql, tuple(params or ())))
        s = " ".join(sql.split()).lower()
        if "insert into scintela.cheque" in s:
            self._next_id += 1
            return {"id_cheque": self._next_id, "no_cheque": "100"}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


@pytest.fixture
def stub(monkeypatch):
    import db
    s = _DBStub()
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    monkeypatch.setattr(db, "tx", s.tx)
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **kw: None)
    import modules.cheques.queries as cq
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **kw: None)
    return s


def test_cheque_normal_no_crea_espejo(stub):
    from modules.cheques import queries as q
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="100",
        importe=500, no_banco=1, banco_texto="Pichincha",
        es_anticipo=False,  # explícito
    )
    assert r.get("id_cheque_anticipo") is None
    assert len(stub.execute_returning_calls) == 1


def test_cheque_anticipo_crea_espejo_negativo(stub):
    from modules.cheques import queries as q
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="100",
        importe=500, no_banco=1, banco_texto="Pichincha",
        es_anticipo=True,
    )
    assert r.get("id_cheque_anticipo") is not None
    # Dos INSERTs: cheque principal + espejo
    assert len(stub.execute_returning_calls) == 2

    # El segundo es el espejo, importe debe ser negativo (-500)
    sql_espejo, params_espejo = stub.execute_returning_calls[1]
    assert "insert into scintela.cheque" in sql_espejo.lower()
    assert -500.0 in params_espejo


def test_cheque_anticipo_pero_importe_cero_no_crea_espejo(stub):
    from modules.cheques import queries as q
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="100",
        importe=0, no_banco=1, banco_texto="Pichincha",
        es_anticipo=True,
    )
    # Solo el cheque principal (con importe=0), no espejo
    assert r.get("id_cheque_anticipo") is None
    assert len(stub.execute_returning_calls) == 1


def test_cheque_anticipo_default_es_false(stub):
    from modules.cheques import queries as q
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="100",
        importe=500, no_banco=1, banco_texto="Pichincha",
        # no paso es_anticipo
    )
    assert r.get("id_cheque_anticipo") is None
    assert len(stub.execute_returning_calls) == 1
