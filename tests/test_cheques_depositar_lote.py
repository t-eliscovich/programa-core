"""Tests para cheques.depositar_lote — invariantes:

1. Happy path: N cheques en cartera + un banco → UPDATE cheque, INSERT
   transacciones_bancarias, INSERT chequextransaccion.
2. Lista vacía → ValueError.
3. Banco sin no_banco → ValueError.
4. Cheque con stat='D' (ya depositado) → ValueError, NO toca DB.
5. Cheque inexistente → ValueError.
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


class _Cur:
    def __init__(self, parent):
        self.parent = parent
        self._next_id = 5000

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.parent.executes.append((sql, tuple(params or ())))
        s = " ".join(sql.split()).lower()
        if "insert into scintela.transacciones_bancarias" in s:
            self._last_id = self._next_id
            self._next_id += 1

    def fetchone(self):
        return (getattr(self, "_last_id", 1234),)


class _Conn:
    def __init__(self, parent):
        self.parent = parent

    def cursor(self, **kw):
        return _Cur(self.parent)


class _DBStub:
    def __init__(self, banco_row=None, cheques=None):
        self.banco_row = banco_row
        self.cheques = cheques or []
        self.executes: list[tuple] = []

    def fetch_one(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.banco" in s:
            return self.banco_row
        raise AssertionError(f"fetch_one inesperado: {s[:80]}")

    def fetch_all(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque" in s and "where id_cheque in" in s:
            ids_query = set(params or ())
            return [c for c in self.cheques if c.get("id_cheque") in ids_query]
        return []

    @contextlib.contextmanager
    def tx(self):
        yield _Conn(self)


@pytest.fixture
def stub_db(monkeypatch):
    import db
    rec = _DBStub(banco_row={"no_banco": 1, "nombre": "Pichincha"})
    monkeypatch.setattr(db, "fetch_one", rec.fetch_one)
    monkeypatch.setattr(db, "fetch_all", rec.fetch_all)
    monkeypatch.setattr(db, "tx", rec.tx)
    return rec


@pytest.fixture
def stub_periodo_guard(monkeypatch):
    # El módulo queries hace `from periodo_guard import asegurar_fecha_abierta`
    # al importarse — hay que patchear AHÍ, no solo en periodo_guard.
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **kw: None)
    import modules.cheques.queries as cq
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **kw: None)


def test_lista_vacia_raisa(stub_db, stub_periodo_guard):
    from modules.cheques import queries as q
    with pytest.raises(ValueError, match="al menos un cheque"):
        q.depositar_lote(ids_cheques=[], no_banco=1)
    assert stub_db.executes == []


def test_sin_banco_raisa(stub_db, stub_periodo_guard):
    from modules.cheques import queries as q
    with pytest.raises(ValueError, match="Banco destino"):
        q.depositar_lote(ids_cheques=[1], no_banco=0)


def test_banco_inexistente_raisa(stub_db, stub_periodo_guard):
    from modules.cheques import queries as q
    stub_db.banco_row = None
    with pytest.raises(ValueError, match="no existe"):
        q.depositar_lote(ids_cheques=[1], no_banco=999)


def test_cheque_ya_depositado_raisa(stub_db, stub_periodo_guard):
    from modules.cheques import queries as q
    stub_db.cheques = [
        {"id_cheque": 1, "stat": "D", "no_cheque": "100", "codigo_cli": "JTX", "importe": 100, "fechad": None},
    ]
    with pytest.raises(ValueError, match="no son depositables"):
        q.depositar_lote(ids_cheques=[1], no_banco=1)
    # No se llegó a la tx
    assert stub_db.executes == []


def test_happy_path_dos_cheques(stub_db, stub_periodo_guard):
    from modules.cheques import queries as q
    stub_db.cheques = [
        {"id_cheque": 1, "stat": "Z", "no_cheque": "100", "codigo_cli": "JTX", "importe": 100, "fechad": None},
        {"id_cheque": 2, "stat": "Z", "no_cheque": "101", "codigo_cli": "BED", "importe": 250, "fechad": None},
    ]
    r = q.depositar_lote(
        ids_cheques=[1, 2], no_banco=1, fecha_deposito=date(2026, 4, 27), usuario="tmt",
    )
    assert r["n_depositados"] == 2
    assert r["total"] == 350.0
    # 1 UPDATE + 2 INSERT transaccion + 2 INSERT chequextransaccion = 5
    assert len(stub_db.executes) == 5
    # primer execute es el UPDATE bulk
    sql_update, _ = stub_db.executes[0]
    assert "update scintela.cheque" in sql_update.lower()
    # Vocabulario canónico (2026-04-29): el depósito pasa el stat a 'B'
    # (antes era 'D' en el sistema legacy). Ver docs/SKILL_ADDENDUM_BATCH_18.md.
    assert "stat = 'b'" in sql_update.lower()


def test_postdatado_p_es_depositable(stub_db, stub_periodo_guard):
    """Cheques en estado 'P' (postdatado) también se pueden depositar."""
    from modules.cheques import queries as q
    stub_db.cheques = [
        {"id_cheque": 1, "stat": "P", "no_cheque": "100", "codigo_cli": "JTX", "importe": 100, "fechad": None},
    ]
    r = q.depositar_lote(ids_cheques=[1], no_banco=1)
    assert r["n_depositados"] == 1
