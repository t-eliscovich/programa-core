"""Anticipos: la cuenta se puede mudar, y el filtro entiende "C2".

TMT 2026-09-03 (dueña):
  1. *"C2 es en realidad el proveedor MC, ¿me ayudás a cambiarlo?"* — el dBase
     abría una cuenta por máquina (C2 = "MCS 2DA MAQ.") y esos anticipos tienen
     que sumar en MC. Ahora hay lapicito en la columna Cuenta (sólo vivos).
  2. *"cuando filtro en anticipos no lo veo bien"* — tipeando "C2" salía el GP
     "2 DO PAGO": la cuenta con un dígito no entraba en `[A-Za-z]{2,3}` y el 2
     se tomaba como número de importación.
"""
from __future__ import annotations

import contextlib
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.dolares import queries as dq  # noqa: E402
from modules.dolares.views import _parsear_codigo  # noqa: E402


# ── el filtro ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("codigo, esperado", [
    ("C2", ("C2", None)),          # el caso que salía mal
    ("c2", ("C2", None)),
    ("P1 15", ("P1", 15)),
    ("AC 15", ("AC", 15)),
    ("15 AC", ("AC", 15)),
    ("AC", ("AC", None)),
    ("15", (None, 15)),
    ("AI 26/26", ("AI", 26)),
    ("AC15", ("AC", 15)),          # pegado: cae al parseo viejo
    ("MCS", ("MCS", None)),
])
def test_parsear_codigo(codigo, esperado):
    assert _parsear_codigo(codigo) == esperado


# ── mudar la cuenta ──────────────────────────────────────────────────────────
class _DBStub:
    def __init__(self, fila, prov):
        self.fila = fila
        self.prov = prov
        self.executes: list[tuple] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.dolares" in s:
            return dict(self.fila) if self.fila else None
        if "from scintela.proveedor" in s:
            return dict(self.prov) if self.prov else None
        return None

    def execute(self, sql, params=None, conn=None):
        self.executes.append((" ".join(sql.split()).lower(), tuple(params or ())))
        return 1

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _armar(monkeypatch, fila, prov, registros):
    import db
    import mov_doble
    s = _DBStub(fila, prov)
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "tx", s.tx)
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: registros.append(kw) or 1)
    return s


_VIVO = {"id_dolares": 2903, "fecha": "2026-04-21", "cta": "C2",
         "concepto": "INI", "importe": 64607.0, "st": None}


def test_muda_un_anticipo_vivo_y_deja_rastro(monkeypatch):
    regs: list = []
    s = _armar(monkeypatch, _VIVO, {"id_proveedor": 228, "nombre": "MCS TEXTILE MACHI"}, regs)
    r = dq.editar_cuenta(id_dolares=2903, cta="mc", usuario="tamara")
    assert r == {"anterior": "C2", "nuevo": "MC", "importe": 64607.0, "cambio": True}
    sql, params = s.executes[0]
    assert "update scintela.dolares set cta = %s" in sql
    assert params[0] == "MC" and params[-1] == 2903
    assert regs and regs[0]["tipo"] == "anticipo_cambio_cuenta"
    assert regs[0]["metadata"] == {"cta_anterior": "C2", "cta_nueva": "MC",
                                   "nombre_nuevo": "MCS TEXTILE MACHI"}
    assert regs[0]["importe"] == 64607.0


def test_la_cuenta_nueva_tiene_que_existir(monkeypatch):
    s = _armar(monkeypatch, _VIVO, None, [])
    with pytest.raises(ValueError, match="No hay proveedor con código ZZ"):
        dq.editar_cuenta(id_dolares=2903, cta="ZZ")
    assert not s.executes


def test_un_anticipo_consumido_no_se_muda(monkeypatch):
    s = _armar(monkeypatch, dict(_VIVO, st="B"), {"id_proveedor": 228, "nombre": "MC"}, [])
    with pytest.raises(ValueError, match="consumido o cancelado"):
        dq.editar_cuenta(id_dolares=2903, cta="MC")
    assert not s.executes


def test_misma_cuenta_no_toca_nada(monkeypatch):
    regs: list = []
    s = _armar(monkeypatch, _VIVO, {"id_proveedor": 278, "nombre": "x"}, regs)
    r = dq.editar_cuenta(id_dolares=2903, cta="c2")
    assert r["cambio"] is False
    assert not s.executes and not regs
