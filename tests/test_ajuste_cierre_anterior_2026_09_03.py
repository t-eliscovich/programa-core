"""El ajuste al cierre anterior baja el PATANT y deja rastro.

TMT 2026-09-03 (dueña): pasó a compra un anticipo de MH del 2024 y la
utilidad de septiembre bajó 21.253 que se gastaron hace dos años. *"quiero que
entre a plata que ya está en el hilo"*. Ver modules/informes/ajuste_cierre.py.
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

from modules.informes import ajuste_cierre  # noqa: E402


class _DBStub:
    def __init__(self, cierre):
        self.cierre = cierre
        self.executes: list[tuple] = []

    def fetch_one(self, sql, params=None, conn=None):
        return dict(self.cierre) if self.cierre else None

    def execute(self, sql, params=None, conn=None):
        self.executes.append((" ".join(sql.split()).lower(), tuple(params or ())))
        return 1

    @contextlib.contextmanager
    def tx(self):
        yield object()


_CIERRE = {"id_historia": 517, "fecha": date(2026, 8, 31), "patrimonio": 21732772.07}


@pytest.fixture
def stub(monkeypatch):
    import db
    import mov_doble
    s = _DBStub(_CIERRE)
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "tx", s.tx)
    monkeypatch.setattr(ajuste_cierre, "cierre_anterior", lambda: dict(_CIERRE))
    s.regs = []
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: s.regs.append(kw) or 1)
    return s


def test_baja_el_patrimonio_del_cierre_en_el_importe(stub):
    r = ajuste_cierre.aplicar(importe=21253.0, motivo="MH 2024: el hilo ya estaba en el stock",
                              usuario="tamara", id_mov_doble_origen=32118)
    assert r["antes"] == pytest.approx(21732772.07)
    assert r["despues"] == pytest.approx(21732772.07 - 21253.0)
    sql, params = stub.executes[0]
    assert "update scintela.historia set patrimonio = %s where id_historia = %s" in sql
    assert params == (pytest.approx(21711519.07), 517)
    reg = stub.regs[0]
    assert reg["tipo"] == "ajuste_cierre_anterior"
    assert (reg["origen_table"], reg["origen_id"]) == ("mov_doble", 32118)
    assert (reg["destino_table"], reg["destino_id"]) == ("historia", 517)
    assert reg["metadata"]["patrimonio_antes"] == pytest.approx(21732772.07)
    assert reg["metadata"]["motivo"] == "MH 2024: el hilo ya estaba en el stock"


def test_sin_movimiento_el_origen_es_el_cierre(stub):
    ajuste_cierre.aplicar(importe=100.0, motivo="x")
    reg = stub.regs[0]
    assert (reg["origen_table"], reg["origen_id"]) == ("historia", 517)


@pytest.mark.parametrize("importe, motivo, msg", [
    (0, "x", "no puede ser cero"),
    (10, "", "motivo"),
])
def test_guards(stub, importe, motivo, msg):
    with pytest.raises(ValueError, match=msg):
        ajuste_cierre.aplicar(importe=importe, motivo=motivo)
    assert not stub.executes


def test_sin_cierre_no_toca_nada(stub, monkeypatch):
    monkeypatch.setattr(ajuste_cierre, "cierre_anterior", lambda: None)
    with pytest.raises(ValueError, match="No hay un cierre anterior"):
        ajuste_cierre.aplicar(importe=10, motivo="x")
    assert not stub.executes
