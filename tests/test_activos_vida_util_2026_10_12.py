"""Cambiar los meses a amortizar de un activo (Tamara 2026-10-12)."""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _Stub:
    def __init__(self, activo):
        self.activo = activo
        self.executes: list[tuple] = []
        self.movs: list[dict] = []

    def fetch_one(self, sql, params=None, conn=None):
        return self.activo

    def execute(self, sql, params=None, conn=None):
        self.executes.append((" ".join(sql.split()), tuple(params or ())))
        return 1

    @contextlib.contextmanager
    def tx(self):
        yield object()


@pytest.fixture
def stub(monkeypatch):
    import db
    import mov_doble

    s = _Stub({"id_activos": 7, "concepto": "Tejedora", "inicial": 12000,
               "amortizac": 0, "cuota": 200, "vida_util": 60})
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "tx", s.tx)
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: s.movs.append(kw) or 1)
    return s


def test_sin_amortizar_es_inicial_sobre_meses(stub):
    from modules.activos import queries as q

    r = q.cambiar_vida_util(7, 120)
    assert r["cuota"] == 100.0 and r["vida_util"] == 120
    sql, params = stub.executes[0]
    assert "set vida_util" in sql.lower() and params == (120, 100.0, 7)
    assert stub.movs[0]["tipo"] == "activo_vida_util"


def test_lo_que_falta_en_los_meses_que_quedan(stub):
    from modules.activos import queries as q

    # 10 meses de 200 ya hechos → faltan 10.000 en 120 − 10 = 110 meses.
    stub.activo.update(amortizac=2000)
    r = q.cambiar_vida_util(7, 120)
    assert r["meses_hechos"] == 10 and r["meses_restan"] == 110
    assert r["cuota"] == round(10000 / 110, 2)


def test_menos_meses_que_los_ya_hechos_frena(stub):
    from modules.activos import queries as q

    stub.activo.update(amortizac=2000)
    with pytest.raises(ValueError, match="más de 10 meses"):
        q.cambiar_vida_util(7, 10)


@pytest.mark.parametrize("malo", [0, -3, "abc", None])
def test_meses_invalidos(stub, malo):
    from modules.activos import queries as q

    with pytest.raises(ValueError):
        q.cambiar_vida_util(7, malo)
