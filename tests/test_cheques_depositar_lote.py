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

    def execute(self, sql, params=None, conn=None):
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

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.banco" in s and "where no_banco" in s:
            return self.banco_row
        if "select saldo from scintela.transacciones_bancarias" in s:
            return {"saldo": 0}
        if "from scintela.transacciones_bancarias" in s and "coalesce(sum" in s:
            return {"sum_signed": 0}
        return None

    def fetch_all(self, sql, params=None, conn=None):
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


# ---------------------------------------------------------------------------
# TMT 2026-08-05 — el depósito escribe `fechaout`, NO `fechaing`.
#
# `scintela.cheque.fechaing` significaba DOS cosas: en las filas del dBase es
# FECHING = el día que el cheque ENTRÓ a cartera (`ALTAS.PRG` L30), y las dos
# rutas de depósito de PC escribían ahí la fecha de SALIDA. Depositar un
# cheque viejo le borraba el día en que entró, y el resumen de cobranza —que
# agrupa por día de ingreso— lo imprimía como cobranza del día del depósito:
# la hoja del 04/08 que va a contabilidad salió con 46 cheques fantasma por
# $74.165,81 (459 filas afectadas desde el 13/07).
#
# El depósito es una SALIDA de cartera y las otras once salidas (C, 9, X, E,
# T y sus deshacer) ya escribían `fechaout`. Era el único que no.
# ---------------------------------------------------------------------------


def test_depositar_lote_escribe_fechaout_y_no_toca_fechaing():
    """La fecha de depósito va a `fechaout`; `fechaing` queda intacto.

    Se inspecciona el FUENTE y no la ejecución: los dos happy-path de este
    archivo están XFAIL por deuda del stub (`_DBStub` no implementa
    `execute_returning`), así que un test que corriera la función pasaría en
    verde por la razón equivocada.
    """
    import inspect
    import re

    from modules.cheques import queries as q
    src = " ".join(inspect.getsource(q.depositar_lote).split()).lower()
    assert "fechaout = %s" in src, "el depósito tiene que escribir fechaout"
    assert not re.search(r"fechaing\s*=\s*%s", src), (
        "el depósito NO puede escribir en fechaing: en las filas del dBase es "
        "el día de INGRESO y pisarlo las convierte en cobranza del día del "
        "depósito (hoja del 04/08: 46 fantasmas por $74.165,81)"
    )
