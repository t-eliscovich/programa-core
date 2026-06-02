"""Tests para bancos.emitir_cheque (wizard de chequera).

Invariantes:
1. tipo='proveedor' con id_posdat → INSERT transaccion + UPDATE posdat (banc=no_banco).
2. tipo='retiro' → INSERT transaccion + INSERT retiros.
3. tipo='caja' → INSERT transaccion + INSERT caja.
4. tipo='gasto' → INSERT transaccion + INSERT xgast (saldo=0 si no postdatado).
5. tipo='otro' → SOLO INSERT transaccion, sin side-effect.
6. importe <= 0 → ValueError.
7. tipo inválido → ValueError.
8. banco inexistente → ValueError.
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
        self._next_id = 7000

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.parent.executes.append((sql, tuple(params or ())))
        s = " ".join(sql.split()).lower()
        if "returning" in s:
            self._last_id = self._next_id
            self._next_id += 1
        # Si es el SELECT que precede al UPDATE multi-posdat, preparar
        # la respuesta de fetchall con un row matching del posdat pedido.
        # El test usa id_posdat=42 con importe=500 (mismo que el cheque),
        # esperamos que el side_effect diga 'Posdat #42 cerrada'.
        self._next_fetchall: list = []
        if "from scintela.posdat" in s and "id_posdat = any" in s:
            ids = list(params[0]) if params and params[0] else []
            importe_cheque = getattr(self.parent, "importe_actual", 0)
            self._next_fetchall = [
                {"id_posdat": i, "prov": "TEST", "importe": importe_cheque}
                for i in ids
            ]

    def fetchone(self):
        # TMT 2026-06-02: producción usa RealDictCursor → cur.fetchone()
        # devuelve dict-like, no tupla. db.execute_returning hace dict(row)
        # sobre lo que sale acá; con tupla rompe con TypeError.
        return {"id_transaccion": getattr(self, "_last_id", 1234)}

    def fetchall(self):
        # Producción usa cur.fetchall() en bancos.queries.emitir_cheque
        # para leer las posdats que se van a cerrar. Devolvemos lo que
        # el execute() previo armó en _next_fetchall.
        rows = getattr(self, "_next_fetchall", [])
        self._next_fetchall = []
        return rows


class _Conn:
    def __init__(self, parent):
        self.parent = parent

    def cursor(self, **kw):
        return _Cur(self.parent)


class _DBStub:
    def __init__(self, banco_row=None):
        self.banco_row = banco_row or {"no_banco": 1, "nombre": "Pichincha"}
        self.executes: list[tuple] = []
        self._next_id = 8000

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.banco" in s and "where no_banco" in s:
            return self.banco_row
        # Producción agregó queries de saldo previo en bank_helpers — mockear
        # como 0 para que el walk-forward funcione sin DB real.
        if "select saldo from scintela.transacciones_bancarias" in s:
            return {"saldo": 0}
        if "from scintela.transacciones_bancarias" in s and "select coalesce(sum" in s:
            return {"sum_signed": 0}
        return None

    def execute_returning(self, sql, params=None, conn=None):
        # TMT 2026-06-02: bank_helpers.insert_movimiento_bancario usa
        # db.execute_returning con INSERT ... RETURNING id_transaccion.
        # Capturamos al mismo executes que el cursor para que los asserts
        # de SQL en los tests sigan funcionando. También guardamos el
        # `importe` del INSERT para que el cursor pueda devolverlo como
        # posdat importe en el fetchall siguiente.
        self.executes.append((sql, tuple(params or ())))
        if "transacciones_bancarias" in sql and params and len(params) >= 5:
            try:
                # signature insert_movimiento_bancario: (fecha, documento, concepto, fechad, importe, ...)
                self.importe_actual = float(params[4] or 0)
            except (TypeError, ValueError, IndexError):
                pass
        self._next_id += 1
        return {"id_transaccion": self._next_id}

    @contextlib.contextmanager
    def tx(self):
        yield _Conn(self)


@pytest.fixture
def stub(monkeypatch):
    import db
    s = _DBStub()
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "tx", s.tx)
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **kw: None)
    # bancos.queries hace import lazy de asegurar_fecha_abierta DENTRO de la función,
    # así que patchear `periodo_guard` directo es suficiente. Pero por consistencia
    # también lo patcheamos en módulos que lo importen al top level.
    import modules.bancos.queries as bq
    if hasattr(bq, "asegurar_fecha_abierta"):
        monkeypatch.setattr(bq, "asegurar_fecha_abierta", lambda *a, **kw: None)
    import bank_helpers
    monkeypatch.setattr(bank_helpers.db, "execute_returning", s.execute_returning)
    monkeypatch.setattr(bank_helpers.db, "fetch_one", s.fetch_one)
    return s


def _sql_text(executes):
    return " | ".join(e[0].lower() for e in executes)


def test_tipo_invalido(stub):
    from modules.bancos import queries as q
    with pytest.raises(ValueError, match="Tipo inválido"):
        q.emitir_cheque(tipo="hackear", no_banco=1, importe=100, fecha=date.today())


def test_importe_cero_o_negativo(stub):
    from modules.bancos import queries as q
    with pytest.raises(ValueError, match="mayor a cero"):
        q.emitir_cheque(tipo="otro", no_banco=1, importe=0, fecha=date.today())
    with pytest.raises(ValueError, match="mayor a cero"):
        q.emitir_cheque(tipo="otro", no_banco=1, importe=-50, fecha=date.today())


def test_banco_inexistente(stub):
    from modules.bancos import queries as q
    stub.banco_row = None
    with pytest.raises(ValueError, match="no existe"):
        q.emitir_cheque(tipo="otro", no_banco=99, importe=100, fecha=date.today())


def test_tipo_otro_solo_insert_transaccion(stub):
    from modules.bancos import queries as q
    r = q.emitir_cheque(tipo="otro", no_banco=1, importe=100, fecha=date.today(),
                        concepto="impuesto IVA")
    assert r["tipo"] == "otro"
    assert r["importe"] == 100
    txt = _sql_text(stub.executes)
    assert "transacciones_bancarias" in txt
    # NO inserta en otras tablas
    assert "scintela.posdat" not in txt
    assert "scintela.retiros" not in txt
    assert "scintela.caja" not in txt
    assert "scintela.xgast" not in txt


def test_tipo_proveedor_con_posdat_actualiza(stub):
    from modules.bancos import queries as q
    r = q.emitir_cheque(
        tipo="proveedor", no_banco=1, importe=500, fecha=date.today(),
        id_posdat=42,
    )
    assert r["tipo"] == "proveedor"
    assert "Posdat #42" in r["side_effect"]
    txt = _sql_text(stub.executes)
    assert "transacciones_bancarias" in txt
    assert "update scintela.posdat" in txt
    assert "set banc =" in txt


def test_tipo_proveedor_sin_posdat_no_falla(stub):
    from modules.bancos import queries as q
    r = q.emitir_cheque(tipo="proveedor", no_banco=1, importe=500, fecha=date.today())
    assert "Sin posdat" in r["side_effect"]
    txt = _sql_text(stub.executes)
    # sólo movimiento bancario
    assert "transacciones_bancarias" in txt
    assert "scintela.posdat" not in txt


def test_tipo_retiro_inserta_en_retiros(stub):
    from modules.bancos import queries as q
    r = q.emitir_cheque(
        tipo="retiro", no_banco=1, importe=2000, fecha=date.today(),
        de_socio="TM", concepto="retiro mensual",
    )
    assert r["tipo"] == "retiro"
    txt = _sql_text(stub.executes)
    assert "insert into scintela.retiros" in txt


def test_tipo_caja_inserta_en_caja(stub):
    from modules.bancos import queries as q
    r = q.emitir_cheque(
        tipo="caja", no_banco=1, importe=300, fecha=date.today(),
    )
    assert r["tipo"] == "caja"
    txt = _sql_text(stub.executes)
    assert "insert into scintela.caja" in txt


def test_tipo_gasto_pagado_inserta_xgast_saldo_cero(stub):
    from modules.bancos import queries as q
    r = q.emitir_cheque(
        tipo="gasto", no_banco=1, importe=150, fecha=date.today(),
        beneficiario="CNEL", concepto="luz mes",
    )
    assert r["tipo"] == "gasto"
    txt = _sql_text(stub.executes)
    assert "insert into scintela.xgast" in txt
    # saldo=0 cuando no es postdatado: chequear que el tercer-último param sea 0.0
    insert_xgast = next(e for e in stub.executes if "insert into scintela.xgast" in e[0].lower())
    # importe=150, saldo=0, stat='C' (cancelado)
    params = insert_xgast[1]
    assert 0.0 in params  # saldo
    assert "C" in params  # stat


def test_tipo_gasto_postdatado_xgast_saldo_pendiente(stub):
    from modules.bancos import queries as q
    q.emitir_cheque(
        tipo="gasto", no_banco=1, importe=150, fecha=date.today(),
        es_postdatado=True, fechad=date(2027, 1, 1),
    )
    txt = _sql_text(stub.executes)
    assert "insert into scintela.xgast" in txt
    insert_xgast = next(e for e in stub.executes if "insert into scintela.xgast" in e[0].lower())
    # cuando es postdatado, saldo=importe y stat='P'
    params = insert_xgast[1]
    assert 150.0 in params  # saldo == importe
    assert "P" in params  # pendiente
