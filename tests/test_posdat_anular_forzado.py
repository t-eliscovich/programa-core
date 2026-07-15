"""TMT 2026-07-15 (dueña: "los gastos forzados no los puedo eliminar").

Blinda el fix: los GASTOS FORZADOS (posdat.banc=9 — compras futuras de hilado,
proyecciones importadas del dBase) SÍ se pueden anular desde el flujo, porque no
tienen cheque ni partida bancaria PC que reversar. Los cheques modernos con
movimiento bancario real (banc=10/32) siguen bloqueados.
"""
import contextlib

import pytest


class _Conn:
    def __init__(self, stub):
        self.stub = stub


class _DBStub:
    def __init__(self):
        self.fetch_one_responses: list = []
        self.executes: list = []

    def fetch_one(self, sql, params=None, conn=None):
        return self.fetch_one_responses.pop(0) if self.fetch_one_responses else None

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    @contextlib.contextmanager
    def tx(self):
        yield _Conn(self)


@pytest.fixture
def stub(monkeypatch):
    import db
    s = _DBStub()
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "tx", s.tx)
    # mov_doble.registrar hace su propia I/O — lo neutralizamos: acá probamos la
    # regla de anular, no el historial.
    import mov_doble
    monkeypatch.setattr(mov_doble, "registrar", lambda *a, **k: 1)
    return s


def _posdat(banc):
    return {"id_posdat": 5, "num": 101, "prov": "AC", "importe": 780000.0,
            "banc": banc, "fecha": None, "anulada": False}


def test_anular_gasto_forzado_banc9_se_permite(stub):
    """banc=9 (gasto forzado) SE puede anular — no hay cheque que reversar."""
    from modules.posdat import queries as q
    stub.fetch_one_responses = [_posdat(9), None]  # posdat, luego lookup mov_doble
    rc = q.anular(5, motivo="prueba borrar forzado", usuario="test")
    assert rc == 1
    assert any("UPDATE scintela.posdat" in sql for sql, _ in stub.executes), \
        "debió correr el UPDATE de soft-delete (anulada=TRUE)"


def test_anular_deuda_viva_banc0_se_permite(stub):
    """banc=0 (deuda viva sin cheque) sigue permitido (baseline)."""
    from modules.posdat import queries as q
    stub.fetch_one_responses = [_posdat(0), None]
    rc = q.anular(5, motivo="prueba", usuario="test")
    assert rc == 1


def test_anular_cheque_real_banc10_se_bloquea(stub):
    """banc=10 (cheque moderno con movimiento bancario real) SIGUE bloqueado."""
    from modules.posdat import queries as q
    stub.fetch_one_responses = [_posdat(10)]
    with pytest.raises(ValueError, match="pagada con cheque"):
        q.anular(5, motivo="prueba", usuario="test")
    assert not stub.executes, "no debió tocar la DB si está bloqueado"
