"""Tests para bancos.registrar_debito_posdat (botón "Registrar banco" de /posdat).

Pedido dueña 2026-07-08: mandar a debitar un posdatado a Pichincha.
Invariantes:
1. posdat banc=0 → INSERT ND en Pichincha + UPDATE posdat (banc=Pichincha) +
   mov_doble tipo='nota_debito' con metadata.id_posdat_debito.
2. posdat inexistente → ValueError.
3. posdat ya debitado (banc!=0) → ValueError (evita doble débito).
4. posdat anulado → ValueError.
5. importe enviado != importe del posdat → ValueError (defensa anti-stale).
6. no_banco resuelto por nombre 'PICHINC'.
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
    def __init__(self, posdat_row=None, banco_no=10):
        self.posdat_row = posdat_row
        self.banco_no = banco_no
        self.executes: list[tuple] = []
        self._next_id = 9000

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.banco" in s and "pichinc" in s:
            return {"no_banco": self.banco_no} if self.banco_no else None
        if "from scintela.posdat" in s and "where id_posdat" in s:
            return self.posdat_row
        # bank_helpers._saldo_previo + later_row checks → saldo 0 / sin filas.
        if "saldo" in s and "transacciones_bancarias" in s:
            return {"saldo": 0}
        return None

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))

    def execute_returning(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        self._next_id += 1
        return {"id_transaccion": self._next_id}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _sql_text(executes):
    return " | ".join(e[0].lower() for e in executes)


@pytest.fixture
def stub(monkeypatch):
    import db

    s = _DBStub(posdat_row={
        "id_posdat": 42, "num": 100, "prov": "AC", "importe": 20200.0,
        "concepto": "6/6 14", "banc": 0, "anulada": False,
    })
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    monkeypatch.setattr(db, "tx", s.tx)

    import bank_helpers
    monkeypatch.setattr(bank_helpers.db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(bank_helpers.db, "execute", s.execute)
    monkeypatch.setattr(bank_helpers.db, "execute_returning", s.execute_returning)

    import modules.bancos.queries as bq
    monkeypatch.setattr(bq, "asegurar_fecha_abierta", lambda *a, **kw: None)

    import mov_doble
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: 777)
    return s


def test_debita_pichincha_y_cierra_posdat(stub):
    from modules.bancos import queries as q

    r = q.registrar_debito_posdat(id_posdat=42, fecha=date(2026, 7, 8), usuario="tamara")
    assert r["id_posdat"] == 42
    assert r["no_banco"] == 10
    assert r["importe"] == 20200.0
    txt = _sql_text(stub.executes)
    # INSERT del ND en el banco + UPDATE del posdat a banc=Pichincha.
    assert "transacciones_bancarias" in txt
    assert "update scintela.posdat" in txt
    assert "set banc =" in txt
    # El UPDATE del posdat setea banc = no_banco (10).
    upd = [e for e in stub.executes if "update scintela.posdat" in e[0].lower()]
    assert upd and 10 in upd[0][1]


def test_posdat_inexistente(stub):
    from modules.bancos import queries as q

    stub.posdat_row = None
    with pytest.raises(ValueError, match="no existe"):
        q.registrar_debito_posdat(id_posdat=999)


def test_posdat_ya_debitado(stub):
    from modules.bancos import queries as q

    stub.posdat_row = {**stub.posdat_row, "banc": 10}
    with pytest.raises(ValueError, match="deuda viva"):
        q.registrar_debito_posdat(id_posdat=42)


def test_posdat_anulado(stub):
    from modules.bancos import queries as q

    stub.posdat_row = {**stub.posdat_row, "anulada": True}
    with pytest.raises(ValueError, match="anulado"):
        q.registrar_debito_posdat(id_posdat=42)


def test_importe_no_coincide(stub):
    from modules.bancos import queries as q

    with pytest.raises(ValueError, match="no coincide"):
        q.registrar_debito_posdat(id_posdat=42, importe=999.0)


def test_no_cheque_va_como_numreferencia(stub):
    from modules.bancos import queries as q

    q.registrar_debito_posdat(id_posdat=42, no_cheque="12345", fecha=date(2026, 7, 8))
    # El INSERT del movimiento bancario lleva numreferencia=12345.
    ins = [e for e in stub.executes if "insert into scintela.transacciones_bancarias" in e[0].lower()]
    assert ins and 12345 in ins[0][1]
