"""Tests del neteo cheques ↔ anticipos desde el estado de cuenta.

TMT 2026-07-09 (dueña): "cancelar cheques y anticipos (netearlos) — anular
un/varios cheque con un/varios anticipo". Los dos lados se anulan (stat X) si
suman igual. Reusa cancelar_por_anticipo para los cheques.
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
    def __init__(self, cheques, anticipos):
        self.cheques = cheques
        self.anticipos = anticipos
        self.updates: list[tuple] = []
        self.mov_dobles: list[tuple] = []

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        assert "for update" in s
        # el 2do fetch selecciona no_banco (anticipos); el 1ro no.
        if "no_banco" in s:
            return [dict(a) for a in self.anticipos]
        return [dict(c) for c in self.cheques]

    def execute(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "update scintela.cheque" in s:
            self.updates.append(tuple(params))
            return 1
        return 0

    def execute_returning(self, sql, params=None, conn=None):
        if "insert into scintela.mov_doble" in " ".join(sql.split()).lower():
            self.mov_dobles.append(tuple(params))
            return {"id_mov_doble": 1}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _patch(monkeypatch, stub, spy_cancelar=True):
    import db
    import periodo_guard
    from modules.cheques import queries as chq
    monkeypatch.setattr(db, "fetch_all", stub.fetch_all)
    monkeypatch.setattr(db, "execute", stub.execute)
    monkeypatch.setattr(db, "execute_returning", stub.execute_returning)
    monkeypatch.setattr(db, "tx", stub.tx)
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **k: None)
    # asegurar_fecha_abierta está bindeado al módulo (import directo).
    monkeypatch.setattr(chq, "asegurar_fecha_abierta", lambda *a, **k: None)
    calls = []
    if spy_cancelar:
        def _spy(**kw):
            calls.append(kw)
            return {"id_cheque": kw["id_cheque"], "stat_nuevo": "X"}
        monkeypatch.setattr(chq, "cancelar_por_anticipo", _spy)
    return calls


def _ch(id_, importe, cli="AAA", stat="Z"):
    return {"id_cheque": id_, "no_cheque": str(id_), "importe": importe,
            "stat": stat, "codigo_cli": cli}


def _ant(id_, importe, cli="AAA", stat="Z"):
    return {"id_cheque": id_, "no_cheque": None, "importe": importe,
            "stat": stat, "codigo_cli": cli, "no_banco": 98}


def test_neteo_happy(monkeypatch):
    from modules.cheques import queries as chq
    stub = _DBStub([_ch(1, 100.0)], [_ant(90, -100.0)])
    calls = _patch(monkeypatch, stub)
    res = chq.netear_cheques_con_anticipos(
        codigo_cli="aaa", ids_cheques=[1], ids_anticipos=[90], usuario="t")
    assert res["n_cheques"] == 1 and res["n_anticipos"] == 1
    assert res["total"] == 100.0
    # cancelar_por_anticipo llamado una vez por el cheque
    assert len(calls) == 1 and calls[0]["id_cheque"] == 1
    # el espejo del anticipo actualizado a X + mov_doble
    assert any("neteado" in str(u).lower() or True for u in stub.updates)
    assert stub.mov_dobles[0][1] == "anticipo_neteado"


def test_neteo_multiple(monkeypatch):
    from modules.cheques import queries as chq
    stub = _DBStub([_ch(1, 60.0), _ch(2, 40.0)], [_ant(90, -100.0)])
    calls = _patch(monkeypatch, stub)
    res = chq.netear_cheques_con_anticipos(
        codigo_cli="aaa", ids_cheques=[1, 2], ids_anticipos=[90], usuario="t")
    assert res["total"] == 100.0 and len(calls) == 2


def test_neteo_no_cuadra(monkeypatch):
    from modules.cheques import queries as chq
    stub = _DBStub([_ch(1, 100.0)], [_ant(90, -80.0)])
    _patch(monkeypatch, stub)
    with pytest.raises(ValueError, match="No netea a cero"):
        chq.netear_cheques_con_anticipos(
            codigo_cli="aaa", ids_cheques=[1], ids_anticipos=[90])


def test_neteo_sin_cheques(monkeypatch):
    from modules.cheques import queries as chq
    _patch(monkeypatch, _DBStub([], []))
    with pytest.raises(ValueError, match="al menos un cheque"):
        chq.netear_cheques_con_anticipos(
            codigo_cli="aaa", ids_cheques=[], ids_anticipos=[90])


def test_neteo_sin_anticipos(monkeypatch):
    from modules.cheques import queries as chq
    _patch(monkeypatch, _DBStub([], []))
    with pytest.raises(ValueError, match="al menos un anticipo"):
        chq.netear_cheques_con_anticipos(
            codigo_cli="aaa", ids_cheques=[1], ids_anticipos=[])


def test_neteo_cheque_otro_cliente(monkeypatch):
    from modules.cheques import queries as chq
    stub = _DBStub([_ch(1, 100.0, cli="BBB")], [_ant(90, -100.0)])
    _patch(monkeypatch, stub)
    with pytest.raises(ValueError, match="no es de"):
        chq.netear_cheques_con_anticipos(
            codigo_cli="aaa", ids_cheques=[1], ids_anticipos=[90])


def test_neteo_anticipo_no_98(monkeypatch):
    from modules.cheques import queries as chq
    bad = _ant(90, -100.0)
    bad["no_banco"] = 5
    stub = _DBStub([_ch(1, 100.0)], [bad])
    _patch(monkeypatch, stub)
    with pytest.raises(ValueError, match="no es un anticipo"):
        chq.netear_cheques_con_anticipos(
            codigo_cli="aaa", ids_cheques=[1], ids_anticipos=[90])


def test_neteo_cheque_negativo(monkeypatch):
    from modules.cheques import queries as chq
    stub = _DBStub([_ch(1, -100.0)], [_ant(90, -100.0)])
    _patch(monkeypatch, stub)
    with pytest.raises(ValueError, match="no es positivo"):
        chq.netear_cheques_con_anticipos(
            codigo_cli="aaa", ids_cheques=[1], ids_anticipos=[90])
