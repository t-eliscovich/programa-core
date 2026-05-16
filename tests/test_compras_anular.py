"""Tests para compras.anular — invariantes:

1. Happy path: stat='Y', observacion apendea [ANUL], borra posdat hermana.
2. Idempotencia: anular una compra ya anulada raisa ValueError.
3. Sin motivo: raisa ValueError antes de tocar la DB.
4. Sin id: raisa ValueError de inexistente.
5. Sin posdat hermana: anula sin error (sólo UPDATE compra).
6. Período cerrado: NO debería levantar ValueError porque el período
   guard NO se aplica a anulaciones (es retroactivo). Pendiente decidir.
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


class _DBRecorder:
    """Stub mínimo de db para los tests de compras.anular.

    Maneja:
      - fetch_one para SELECT id_compra/codigo_prov/numero/stat
      - tx() context manager
      - execute() recorder de DELETE de posdat y UPDATE de compra
    """

    def __init__(self, compra_row=None):
        self.compra_row = compra_row
        self.executes: list[tuple[str, tuple]] = []

    def fetch_one(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.compra" in s and "where id_compra" in s:
            return self.compra_row
        raise AssertionError(f"fetch_one inesperado: {s[:80]}")

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    @contextlib.contextmanager
    def tx(self):
        yield object()


@pytest.fixture
def stub_db(monkeypatch):
    import db
    rec = _DBRecorder()
    monkeypatch.setattr(db, "fetch_one", rec.fetch_one)
    monkeypatch.setattr(db, "execute", rec.execute)
    monkeypatch.setattr(db, "tx", rec.tx)
    return rec


@pytest.fixture
def stub_periodo_guard(monkeypatch):
    """No-op asegurar_fecha_abierta — el test no chequea períodos cerrados."""
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **kw: None)
    # El módulo queries lo importó con `from periodo_guard import ...` así
    # que también patcheamos su referencia local
    import modules.compras.queries as cq
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **kw: None)


def test_happy_path_anular_actualiza_stat_y_borra_posdat(stub_db, stub_periodo_guard):
    from modules.compras import queries as q
    stub_db.compra_row = {
        "id_compra": 42,
        "codigo_prov": "TEX",
        "numero": 100,
        "stat": None,
    }

    n = q.anular(42, motivo="Error de carga", usuario="tmt")
    assert n == 1
    assert len(stub_db.executes) == 2

    sql_update, params_update = stub_db.executes[0]
    assert "update scintela.compra" in sql_update.lower()
    assert "stat = 'y'" in sql_update.lower()
    # observacion debe apendearse con [ANUL]
    assert "[anul]" in str(params_update[0]).lower()
    assert "error de carga" in str(params_update[0]).lower()

    sql_delete, params_delete = stub_db.executes[1]
    assert "delete from scintela.posdat" in sql_delete.lower()
    assert params_delete == ("TEX", 100)


def test_motivo_vacio_raisa_value_error(stub_db, stub_periodo_guard):
    from modules.compras import queries as q
    with pytest.raises(ValueError, match="Motivo"):
        q.anular(42, motivo="", usuario="tmt")
    # NO debería tocar la DB
    assert stub_db.executes == []


def test_motivo_solo_espacios_raisa_value_error(stub_db, stub_periodo_guard):
    from modules.compras import queries as q
    with pytest.raises(ValueError, match="Motivo"):
        q.anular(42, motivo="   \t\n  ", usuario="tmt")
    assert stub_db.executes == []


def test_compra_inexistente_raisa_value_error(stub_db, stub_periodo_guard):
    from modules.compras import queries as q
    stub_db.compra_row = None  # SELECT devuelve None
    with pytest.raises(ValueError, match="inexistente"):
        q.anular(999, motivo="test", usuario="tmt")
    assert stub_db.executes == []  # no se intentó UPDATE


def test_compra_ya_anulada_raisa_value_error(stub_db, stub_periodo_guard):
    from modules.compras import queries as q
    stub_db.compra_row = {
        "id_compra": 42,
        "codigo_prov": "TEX",
        "numero": 100,
        "stat": "Y",  # ya anulada
    }
    with pytest.raises(ValueError, match="ya está anulada"):
        q.anular(42, motivo="reintento", usuario="tmt")
    assert stub_db.executes == []


def test_compra_sin_numero_no_borra_posdat(stub_db, stub_periodo_guard):
    """Si la compra no tiene numero, no hay posdat hermana — sólo UPDATE."""
    from modules.compras import queries as q
    stub_db.compra_row = {
        "id_compra": 42,
        "codigo_prov": "TEX",
        "numero": None,  # sin número de compra
        "stat": None,
    }
    n = q.anular(42, motivo="test", usuario="tmt")
    assert n == 1
    assert len(stub_db.executes) == 1  # SOLO el UPDATE, no el DELETE
    sql, _ = stub_db.executes[0]
    assert "update" in sql.lower()
