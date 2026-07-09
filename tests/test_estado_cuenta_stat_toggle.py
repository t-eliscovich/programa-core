"""Tests del toggle A/Z ↔ T de facturas en el estado de cuenta.

TMT 2026-07-09 (dueña): "poder pasar facturas de A→T y T→A". CERRAR una viva
(Z/A) → stat T, saldo 0 (snapshot para revertir); REABRIR una T → stat A,
restaurando el snapshot (o fallback saldo=importe−abono).
"""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _DBStub:
    def __init__(self, factura, snapshot_md=None):
        self.factura = factura
        self.snapshot_md = snapshot_md
        self.updates: list[tuple] = []
        self.mov_dobles: list[tuple] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.factura" in s:
            return dict(self.factura) if self.factura else None
        if "from scintela.mov_doble" in s:
            return {"metadata": self.snapshot_md} if self.snapshot_md else None
        return None

    def execute(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "update scintela.factura" in s:
            self.updates.append(tuple(params))
            return 1
        return 0

    def execute_returning(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "insert into scintela.mov_doble" in s:
            self.mov_dobles.append(tuple(params))
            return {"id_mov_doble": 999}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _patch(monkeypatch, stub):
    import db
    import periodo_guard
    monkeypatch.setattr(db, "fetch_one", stub.fetch_one)
    monkeypatch.setattr(db, "execute", stub.execute)
    monkeypatch.setattr(db, "execute_returning", stub.execute_returning)
    monkeypatch.setattr(db, "tx", stub.tx)
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **k: None)


def _fac(stat, importe=100.0, abono=60.0, saldo=40.0, cli="AAA"):
    return {
        "id_factura": 5, "numf": 5, "numf_completo": "001-001-000000005",
        "codigo_cli": cli, "importe": importe, "abono": abono,
        "saldo": saldo, "stat": stat,
    }


def test_cerrar_A_a_T(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStub(_fac("A", 100.0, 60.0, 40.0))
    _patch(monkeypatch, stub)
    res = q.factura_cambiar_stat_a_t(5, "aaa", usuario="tester")
    assert res["accion"] == "cerrada"
    assert (res["stat_previo"], res["stat_nuevo"]) == ("A", "T")
    assert res["saldo_nuevo"] == 0.0
    # UPDATE: stat T, saldo 0, usuario, id
    assert stub.updates == [("tester", 5)] or stub.updates[0][-1] == 5
    # mov_doble con snapshot del saldo/abono previos
    assert stub.mov_dobles[0][1] == "factura_cerrada_a_t"


def test_cerrar_Z_a_T(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStub(_fac("Z", 100.0, 0.0, 100.0))
    _patch(monkeypatch, stub)
    res = q.factura_cambiar_stat_a_t(5, "aaa", usuario="t")
    assert res["accion"] == "cerrada" and res["stat_nuevo"] == "T"


def test_reabrir_T_con_snapshot(monkeypatch):
    from modules.informes import queries as q
    # T con saldo 0 (cerrada), snapshot dice que antes era abono 60 / saldo 40.
    stub = _DBStub(
        _fac("T", 100.0, 100.0, 0.0),
        snapshot_md={"saldo_previo": 40.0, "abono_previo": 60.0},
    )
    _patch(monkeypatch, stub)
    res = q.factura_cambiar_stat_a_t(5, "aaa", usuario="t")
    assert res["accion"] == "reabierta"
    assert (res["stat_previo"], res["stat_nuevo"]) == ("T", "A")
    assert res["saldo_nuevo"] == 40.0
    # UPDATE restaura saldo 40 y abono 60
    upd = stub.updates[0]
    assert 40.0 in upd and 60.0 in upd
    assert stub.mov_dobles[0][1] == "factura_reabierta_de_t"


def test_reabrir_T_sin_snapshot_fallback(monkeypatch):
    from modules.informes import queries as q
    # Sin snapshot (T de un totalizar): fallback saldo = importe − abono.
    stub = _DBStub(_fac("T", 100.0, 30.0, 0.0), snapshot_md=None)
    _patch(monkeypatch, stub)
    res = q.factura_cambiar_stat_a_t(5, "aaa", usuario="t")
    assert res["saldo_nuevo"] == 70.0  # 100 − 30


def test_snapshot_str_json(monkeypatch):
    from modules.informes import queries as q
    # metadata puede venir como str JSON del driver.
    stub = _DBStub(
        _fac("T", 100.0, 100.0, 0.0),
        snapshot_md='{"saldo_previo": 25.0, "abono_previo": 75.0}',
    )
    _patch(monkeypatch, stub)
    res = q.factura_cambiar_stat_a_t(5, "aaa", usuario="t")
    assert res["saldo_nuevo"] == 25.0


def test_factura_inexistente(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStub(None)
    _patch(monkeypatch, stub)
    with pytest.raises(ValueError, match="no existe"):
        q.factura_cambiar_stat_a_t(5, "aaa")


def test_otro_cliente(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStub(_fac("A", cli="BBB"))
    _patch(monkeypatch, stub)
    with pytest.raises(ValueError, match="es de"):
        q.factura_cambiar_stat_a_t(5, "aaa")


def test_stat_no_toggleable(monkeypatch):
    from modules.informes import queries as q
    stub = _DBStub(_fac("X"))
    _patch(monkeypatch, stub)
    with pytest.raises(ValueError, match="solo se"):
        q.factura_cambiar_stat_a_t(5, "aaa")
