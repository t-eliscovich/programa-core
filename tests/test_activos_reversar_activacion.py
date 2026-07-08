"""Tests para activos.reversar_activacion (deshacer activación de maquinaria).

Pedido dueña 2026-07-08: reversar la activación → volver anticipos, eliminar
cuotas, borrar la máquina, y que aparezca/reverse desde /historial.

Cubrimos los guards de validación (que no requieren tx real) + el happy path
con un stub de db.
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
    def __init__(self, md=None, maq=None, pagadas=None):
        self.md = md
        self.maq = maq
        self.pagadas = pagadas or []
        self.executes: list[tuple] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.mov_doble" in s:
            return self.md
        if "from scintela.activos" in s and "for update" in s:
            return self.maq
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.posdat" in s and "banc" in s:
            return self.pagadas
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 0

    @contextlib.contextmanager
    def tx(self):
        yield object()


_MD_OK = {
    "id_mov_doble": 500, "tipo": "activacion_maquinaria", "origen_id": 42,
    "estado": "activo",
    "metadata": {"id_activos": 42, "ids_anticipos": [7, 8], "ids_posdat": [11, 12],
                 "valor_total": 30000.0},
}


@pytest.fixture
def stub(monkeypatch):
    import db

    s = _DBStub(md=dict(_MD_OK), maq={"id_activos": 42, "amortizac": 0, "ult_mes_amortizado": None})
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "fetch_all", s.fetch_all)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "tx", s.tx)
    import mov_doble
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: 999)
    return s


def _txt(executes):
    return " | ".join(e[0].lower() for e in executes)


def test_reversa_ok(stub):
    from modules.activos import queries as q

    r = q.reversar_activacion(500, usuario="tamara")
    assert r["id_activos"] == 42
    assert r["cuotas_eliminadas"] == 2
    t = _txt(stub.executes)
    assert "update scintela.dolares" in t and "set st = ''" in t
    assert "delete from scintela.posdat" in t
    assert "delete from scintela.activos" in t


def test_mov_inexistente(stub):
    from modules.activos import queries as q

    stub.md = None
    with pytest.raises(ValueError, match="no encontrado"):
        q.reversar_activacion(500)


def test_tipo_incorrecto(stub):
    from modules.activos import queries as q

    stub.md = {**_MD_OK, "tipo": "deposito"}
    with pytest.raises(ValueError, match="no es una activación"):
        q.reversar_activacion(500)


def test_ya_reversada(stub):
    from modules.activos import queries as q

    stub.md = {**_MD_OK, "estado": "reversado"}
    with pytest.raises(ValueError, match="ya fue reversada"):
        q.reversar_activacion(500)


def test_maquina_ya_amortizo(stub):
    from modules.activos import queries as q

    stub.maq = {"id_activos": 42, "amortizac": 500, "ult_mes_amortizado": None}
    with pytest.raises(ValueError, match="amortizó"):
        q.reversar_activacion(500)


def test_cuota_ya_pagada_bloquea(stub):
    from modules.activos import queries as q

    stub.pagadas = [{"id_posdat": 11, "num": 300, "banc": 10}]
    with pytest.raises(ValueError, match="registrada"):
        q.reversar_activacion(500)
    # No debe haber borrado nada.
    assert "delete from scintela.activos" not in _txt(stub.executes)
