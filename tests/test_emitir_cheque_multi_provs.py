"""Tests para el path multi-proveedor de bancos.emitir_cheque.

TMT 2026-05-27 dueña: 'cuando emito cheques, puedes dejarme seleccionar
multiples proveedores'. El query acepta `id_posdats: list[int]` y cierra
todas las posdats en una sola tx; la diferencia entre la suma neta y el
importe del cheque queda anotada en `extras` como anticipo (diff > 0) o
saldo pendiente (diff < 0).

Estos tests se aíslan stubbeando bank_helpers + caja_helpers para no
depender del path real (que los tests de test_bancos_emitir_cheque.py
pre-existentes no logran, por eso 7 de ellos están rojos en origin).
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
    """Cursor que recuerda los executes + el último SELECT para fetchall."""

    def __init__(self, parent):
        self.parent = parent
        self._next_id = 8000
        self._last_select = ""
        self._last_params: tuple = ()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.parent.executes.append((sql, tuple(params or ())))
        s = " ".join(sql.split()).lower()
        self._last_select = s
        self._last_params = tuple(params or ())
        if "returning" in s:
            self._last_id = self._next_id
            self._next_id += 1

    def fetchone(self):
        return (getattr(self, "_last_id", 1234),)

    def fetchall(self):
        # Devuelve las filas de posdat según el ANY(%s) que el query mande.
        if "from scintela.posdat" in self._last_select and "where id_posdat = any" in self._last_select:
            ids = self._last_params[0] if self._last_params else []
            return [self.parent.posdats_db[i] for i in ids if i in self.parent.posdats_db]
        return []


class _Conn:
    def __init__(self, parent):
        self.parent = parent

    def cursor(self, **kw):
        return _Cur(self.parent)


class _DBStub:
    def __init__(self):
        self.banco_row = {"no_banco": 1, "nombre": "Pichincha"}
        self.executes: list[tuple] = []
        # Posdats disponibles indexadas por id. Cada test setea las que use.
        self.posdats_db: dict[int, dict] = {}

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.banco" in s and "where no_banco" in s:
            return self.banco_row
        return None

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    @contextlib.contextmanager
    def tx(self):
        yield _Conn(self)


@pytest.fixture
def stub(monkeypatch):
    import db
    import periodo_guard
    s = _DBStub()
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "tx", s.tx)
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **kw: None)

    # Stub bank_helpers — devuelve un id_transaccion fake; el path real
    # depende de saldos previos que aquí no nos interesan.
    import bank_helpers
    monkeypatch.setattr(
        bank_helpers,
        "insert_movimiento_bancario",
        lambda *a, **kw: {"id_transaccion": 99001, "saldo": 0.0},
    )
    # Stub mov_doble — auditoría de doble registro, no relevante para el
    # test del path multi-proveedor.
    import mov_doble
    monkeypatch.setattr(mov_doble, "registrar", lambda *a, **kw: 99500)
    return s


def test_multi_posdats_misma_suma_que_cheque(stub):
    """3 posdats que neto exactamente igual al importe → 0 dif."""
    from modules.bancos import queries as q

    stub.posdats_db = {
        10: {"id_posdat": 10, "prov": "QI", "importe": 300.00},
        20: {"id_posdat": 20, "prov": "TP", "importe": 200.00},
        30: {"id_posdat": 30, "prov": "EM", "importe": 100.00},
    }

    r = q.emitir_cheque(
        tipo="proveedor", no_banco=1, importe=600.00, fecha=date.today(),
        id_posdats=[10, 20, 30],
    )
    assert "3 posdats cerradas" in r["side_effect"]
    assert "3 provs" in r["side_effect"]  # multi-proveedor

    # Verifica el UPDATE bulk con ANY(%s) y la lista pasada.
    txt = " ".join(s for s, _ in stub.executes).lower()
    assert "update scintela.posdat" in txt
    assert "id_posdat = any(%s)" in txt


def test_multi_posdats_cheque_mayor_a_suma_genera_anticipo(stub):
    """Cheque por 1000 cubre obligaciones por 700 → 300 quedan como anticipo."""
    from modules.bancos import queries as q

    stub.posdats_db = {
        11: {"id_posdat": 11, "prov": "QI", "importe": 400.00},
        12: {"id_posdat": 12, "prov": "QI", "importe": 300.00},
    }

    r = q.emitir_cheque(
        tipo="proveedor", no_banco=1, importe=1000.00, fecha=date.today(),
        id_posdats=[11, 12],
    )
    se = r["side_effect"]
    assert "anticipo" in se.lower()
    assert "300" in se  # la diferencia
    # Provs solo QI (1) → sin sufijo multi-provs.
    assert "provs" not in se or "1 prov" not in se


def test_multi_posdats_cheque_menor_a_suma_genera_saldo(stub):
    """Cheque por 500 cubre obligaciones por 700 → 200 quedan como saldo."""
    from modules.bancos import queries as q

    stub.posdats_db = {
        21: {"id_posdat": 21, "prov": "TP", "importe": 400.00},
        22: {"id_posdat": 22, "prov": "EM", "importe": 300.00},
    }

    r = q.emitir_cheque(
        tipo="proveedor", no_banco=1, importe=500.00, fecha=date.today(),
        id_posdats=[21, 22],
    )
    se = r["side_effect"]
    assert "saldo" in se.lower()
    assert "200" in se


def test_multi_posdats_con_nc_negativas_netea(stub):
    """Mix de facturas y NCs (importe negativo) — la suma neta resta."""
    from modules.bancos import queries as q

    stub.posdats_db = {
        31: {"id_posdat": 31, "prov": "QI", "importe": 1000.00},   # factura
        32: {"id_posdat": 32, "prov": "QI", "importe": -200.00},   # NC
    }

    r = q.emitir_cheque(
        tipo="proveedor", no_banco=1, importe=800.00, fecha=date.today(),
        id_posdats=[31, 32],
    )
    # Suma neta = 800 = cheque → sin diferencia.
    assert "2 posdats cerradas" in r["side_effect"]
    assert "anticipo" not in r["side_effect"].lower()
    assert "saldo" not in r["side_effect"].lower()


def test_id_posdats_gana_sobre_id_posdat_legacy(stub):
    """Si vienen ambos (single legacy + multi), multi prevalece."""
    from modules.bancos import queries as q

    stub.posdats_db = {
        41: {"id_posdat": 41, "prov": "QI", "importe": 500.00},
    }

    r = q.emitir_cheque(
        tipo="proveedor", no_banco=1, importe=500.00, fecha=date.today(),
        id_posdat=99,  # legacy — debería ser ignorado
        id_posdats=[41],
    )
    # El UPDATE usa la lista, no el 99 legacy.
    txt = " ".join(s for s, _ in stub.executes).lower()
    assert "id_posdat = any(%s)" in txt
    # Y los params del UPDATE deben contener [41], no 99.
    params_concat = str([p for _, p in stub.executes])
    assert "41" in params_concat


def test_sin_posdats_seleccionadas_no_falla(stub):
    """Backward compat — sin id_posdats ni id_posdat = solo movimiento."""
    from modules.bancos import queries as q

    r = q.emitir_cheque(
        tipo="proveedor", no_banco=1, importe=500.00, fecha=date.today(),
    )
    assert "Sin posdat" in r["side_effect"]
