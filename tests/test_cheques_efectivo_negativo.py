"""Efectivo NEGATIVO (banco 99) — devolución de plata al cliente.

TMT 2026-07-30, dueña: *"se trata de un ingreso en negativo por caja... es una
entrada en negativo a la caja que se convierte en salida"* y *"¿por qué no lo
cargás desde caja? porque necesito que se registre el abono en negativo en la
factura del cliente"*.

Tres cosas tenían que ceder para que eso funcione:

1. `crear()` dejaba el 99 negativo en 'Z' (cartera) — el gate era `> 0`. La
   plata salía de la lata y el sistema no se enteraba.
2. La fila de caja iba siempre con tipo='E'. El trigger `fn_caja_set_saldo`
   toma el signo del TIPO y usa ABS(importe): un importe negativo con 'E'
   SUMABA a la caja. El signo va en el tipo.
3. `aplicar_a_factura` topeaba todo negativo contra el ABONO de la factura.
   Una nota de crédito nace con abono 0, así que saldarla con una devolución
   era imposible ("excede el abono (0.00)").
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


class _StubCrear:
    def __init__(self):
        self.inserts: list[tuple] = []
        self._next = 700

    def fetch_one(self, sql, params=None, conn=None):
        return None

    def execute(self, sql, params=None, conn=None):
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        self.inserts.append((s, tuple(params or ())))
        if "insert into scintela.cheque" in s:
            self._next += 1
            return {"id_cheque": self._next, "no_cheque": ""}
        if "insert into scintela.caja" in s:
            return {"id_caja": 9001}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


@pytest.fixture
def _crear(monkeypatch):
    import db
    import mov_doble
    import periodo_guard
    s = _StubCrear()
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    monkeypatch.setattr(db, "tx", s.tx)
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: {"id_mov_doble": 1})
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **kw: None)
    return s


def _caja_insert(stub):
    return next((i for i in stub.inserts if "insert into scintela.caja" in i[0]), None)


def test_efectivo_negativo_sale_de_caja_como_S(_crear):
    from modules.cheques import queries as q
    q.crear(fecha=date(2026, 7, 30), fechad=date(2026, 7, 30),
            codigo_cli="ADI", no_cheque="", importe=-1679.26,
            no_banco=99, usuario="test")
    ins = _caja_insert(_crear)
    assert ins is not None, "el efectivo negativo no generó fila de caja"
    # (fecha, tipo, importe, concepto, clave, id_cheque, usuario)
    assert ins[1][1] == "S", "una devolución tiene que ser SALIDA de caja"
    assert ins[1][2] == 1679.26, "el importe de caja va SIEMPRE positivo"
    assert ins[1][3].startswith("DEV."), "se lee qué es sin abrir el cheque"


def test_efectivo_positivo_sigue_entrando_como_E(_crear):
    from modules.cheques import queries as q
    q.crear(fecha=date(2026, 7, 30), fechad=date(2026, 7, 30),
            codigo_cli="ADI", no_cheque="", importe=500.0,
            no_banco=99, usuario="test")
    ins = _caja_insert(_crear)
    assert ins is not None
    assert ins[1][1] == "E"
    assert ins[1][2] == 500.0
    assert ins[1][3] == "CH.ADI", "paridad PASOCAJA"


# ── El negativo tiene que poder SALDAR una nota de crédito ────────────────

class _StubAplicarNeg:
    def __init__(self, saldo, abono):
        self.saldo, self.abono = saldo, abono
        self.updates: list[tuple] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque" in s:
            return {"id_cheque": 777, "codigo_cli": "ADI", "no_banco": 99,
                    "importe": -1679.26, "stat": "C", "fecha": date(2026, 7, 30)}
        if "from scintela.factura" in s:
            return {"id_factura": 10985, "numf": 10985, "importe": -16.20,
                    "abono": self.abono, "saldo": self.saldo, "stat": "Z"}
        return None

    def execute(self, sql, params=None, conn=None):
        self.updates.append((" ".join(sql.split()).lower(), tuple(params or ())))
        return 1


def _aplicar(monkeypatch, saldo, abono, imp):
    import db
    import mov_doble
    from modules.cheques import queries as q
    s = _StubAplicarNeg(saldo, abono)
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(mov_doble, "registrar", lambda **kw: None)
    q.aplicar_a_factura(id_cheque=777,
                        aplicaciones=[{"id_fact": 10985, "importe": imp}],
                        conn=object(), permitir_depositado=True)
    return s


def test_el_negativo_salda_una_nota_de_credito(monkeypatch):
    # NC: importe -16,20, abono 0, saldo -16,20. La devolución la cierra.
    s = _aplicar(monkeypatch, saldo=-16.20, abono=0.0, imp=-16.20)
    upd = [u for u in s.updates if "update scintela.factura" in u[0]]
    assert upd, "no actualizó la factura"
    abono, saldo, stat = upd[0][1][0], upd[0][1][1], upd[0][1][2]
    assert round(abono, 2) == -16.20
    assert abs(saldo) < 0.005
    assert stat == "T", "la nota de crédito queda saldada"


def test_el_negativo_no_puede_exceder_el_credito(monkeypatch):
    with pytest.raises(ValueError, match="excede el crédito"):
        _aplicar(monkeypatch, saldo=-16.20, abono=0.0, imp=-900.0)


def test_contra_saldo_positivo_el_tope_sigue_siendo_el_abono(monkeypatch):
    with pytest.raises(ValueError, match="excede el abono"):
        _aplicar(monkeypatch, saldo=500.0, abono=100.0, imp=-300.0)
