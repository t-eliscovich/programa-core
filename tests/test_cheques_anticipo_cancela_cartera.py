"""Tests del feature "anticipo (97) aplicado a cheques en cartera".

TMT 2026-07-06 (dueña): "esto va a ser un anticipo que se lo aplicamos a
los cheques... si el anticipo era 3000 y había 3 cheques de 1000, tengo que
cancelar (X) todos esos cheques. Si me dio 10.000, cancelo los 3 de 1000 y
además sumo una nota de crédito por 7000."

Cubre el backend:
1. distribuir_espejos_anticipo — suma cancelada vs anticipo (tope +$0.01),
   sobrante exacto, FIFO entre varios cheques-anticipo.
2. crear(anticipo_espejo_importe=...) — el espejo NB=98 se crea SOLO por el
   sobrante; sobrante < $1 = sin espejo; None = flujo clásico intacto.
3. cancelar_por_anticipo — UPDATE a stat='X' + mov_doble
   'cheque_cancelado_por_anticipo' + validaciones (dueño, stat vivo,
   importe > 0, sin aplicaciones a facturas).

Mismo estilo stub que tests/test_cheques_anticipo.py.
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


# ─────────────────────────── distribuir_espejos_anticipo ──────────────────

def test_distribuir_sin_sobrante():
    from modules.cheques import queries as q
    # anticipo 3000, cancela 3 cheques de 1000 → espejo 0 (no se crea NC)
    assert q.distribuir_espejos_anticipo([3000.0], 3000.0) == [0.0]


def test_distribuir_con_sobrante():
    from modules.cheques import queries as q
    # anticipo 10000, cancela 3000 → NC por 7000
    assert q.distribuir_espejos_anticipo([10000.0], 3000.0) == [7000.0]


def test_distribuir_sin_cancelados_espejo_total():
    from modules.cheques import queries as q
    # nada cancelado → espejo por el total (flujo clásico)
    assert q.distribuir_espejos_anticipo([500.0], 0.0) == [500.0]


def test_distribuir_fifo_multi_anticipo():
    from modules.cheques import queries as q
    # dos cheques-anticipo (2000 + 3000), cancela 2500: el 1ro se consume
    # entero (espejo 0), al 2do le queda 2500 de sobrante.
    assert q.distribuir_espejos_anticipo([2000.0, 3000.0], 2500.0) == [0.0, 2500.0]


def test_distribuir_suma_supera_anticipo_error():
    from modules.cheques import queries as q
    with pytest.raises(ValueError, match="no se puede cancelar más que el anticipo"):
        q.distribuir_espejos_anticipo([3000.0], 3000.02)


def test_distribuir_tolerancia_un_centavo_ok():
    from modules.cheques import queries as q
    # tope = anticipo + $0.01 — un centavo de redondeo pasa
    esp = q.distribuir_espejos_anticipo([3000.0], 3000.01)
    assert esp == [0.0]


def test_distribuir_suma_negativa_error():
    from modules.cheques import queries as q
    with pytest.raises(ValueError, match="negativa"):
        q.distribuir_espejos_anticipo([3000.0], -1.0)


# ─────────────────────────── crear() con espejo acotado ───────────────────

class _DBStubCrear:
    def __init__(self):
        self.execute_returning_calls: list[tuple] = []
        self._next_id = 100

    def fetch_one(self, sql, params=None, conn=None):
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.execute_returning_calls.append((sql, tuple(params or ())))
        s = " ".join(sql.split()).lower()
        if "insert into scintela.cheque" in s:
            self._next_id += 1
            return {"id_cheque": self._next_id, "no_cheque": "100"}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


@pytest.fixture
def stub(monkeypatch):
    import db
    s = _DBStubCrear()
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "fetch_all", s.fetch_all)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    monkeypatch.setattr(db, "tx", s.tx)
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **kw: None)
    import modules.cheques.queries as cq
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **kw: None)
    return s


def _inserts_cheque(stub):
    return [
        (sql, params)
        for sql, params in stub.execute_returning_calls
        if "insert into scintela.cheque" in " ".join(sql.split()).lower()
    ]


def test_crear_espejo_solo_por_sobrante(stub):
    from modules.cheques import queries as q
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="",
        importe=10000, no_banco=99, banco_texto=None,
        es_anticipo=True, anticipo_espejo_importe=7000.0,
    )
    assert r.get("id_cheque_anticipo") is not None
    ins = _inserts_cheque(stub)
    assert len(ins) == 2  # principal + espejo
    _, params_espejo = ins[1]
    assert -7000.0 in params_espejo   # espejo por el SOBRANTE, no el total
    assert -10000.0 not in params_espejo


def test_crear_sobrante_menor_a_un_dolar_sin_espejo(stub):
    from modules.cheques import queries as q
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="",
        importe=3000, no_banco=99, banco_texto=None,
        es_anticipo=True, anticipo_espejo_importe=0.40,
    )
    assert r.get("id_cheque_anticipo") is None
    assert len(_inserts_cheque(stub)) == 1  # solo el principal


def test_crear_sobrante_cero_sin_espejo(stub):
    from modules.cheques import queries as q
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="",
        importe=3000, no_banco=99, banco_texto=None,
        es_anticipo=True, anticipo_espejo_importe=0.0,
    )
    assert r.get("id_cheque_anticipo") is None
    assert len(_inserts_cheque(stub)) == 1


def test_crear_sin_override_espejo_total_compat(stub):
    from modules.cheques import queries as q
    # anticipo_espejo_importe=None → flujo clásico (espejo por el total)
    r = q.crear(
        fecha=date.today(), codigo_cli="JTX", no_cheque="100",
        importe=500, no_banco=1, banco_texto="Pichincha",
        es_anticipo=True,
    )
    assert r.get("id_cheque_anticipo") is not None
    ins = _inserts_cheque(stub)
    assert len(ins) == 2
    assert -500.0 in ins[1][1]


# ─────────────────────────── cancelar_por_anticipo ────────────────────────

class _DBStubCancelar:
    """Stub con un cheque configurable + tracking de UPDATEs y mov_doble."""

    def __init__(self, cheque_row=None, aplicaciones=None):
        self.cheque_row = cheque_row
        self.aplicaciones = aplicaciones or []
        self.execute_calls: list[tuple] = []
        self.execute_returning_calls: list[tuple] = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque" in s:
            return self.cheque_row
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.chequesxfact" in s:
            return self.aplicaciones
        return []

    def execute(self, sql, params=None, conn=None):
        self.execute_calls.append((sql, tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.execute_returning_calls.append((sql, tuple(params or ())))
        return {"id_mov_doble": 999}

    @contextlib.contextmanager
    def tx(self):
        yield object()


def _wire(monkeypatch, s):
    import db
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "fetch_all", s.fetch_all)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    monkeypatch.setattr(db, "tx", s.tx)
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **kw: None)
    import modules.cheques.queries as cq
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **kw: None)
    return s


def _cheque_vivo(**kw):
    base = {
        "id_cheque": 777, "no_cheque": "123", "stat": "Z",
        "codigo_cli": "JTX", "importe": 1000.0, "fechad": date.today(),
    }
    base.update(kw)
    return base


def test_cancelar_marca_x_y_registra_mov_doble(monkeypatch):
    from modules.cheques import queries as q
    s = _wire(monkeypatch, _DBStubCancelar(cheque_row=_cheque_vivo()))
    r = q.cancelar_por_anticipo(
        id_cheque=777, codigo_cli="JTX",
        id_cheque_anticipo=888, monto_anticipo=3000.0, usuario="test",
    )
    assert r["stat_previo"] == "Z"
    assert r["stat_nuevo"] == "X"
    assert r["importe"] == 1000.0
    # UPDATE a stat='X' con el tag "cancelado por anticipo <id/monto>"
    upd = [
        (sql, params) for sql, params in s.execute_calls
        if "stat='x'" in " ".join(sql.split()).lower()
    ]
    assert len(upd) == 1
    _, params = upd[0]
    marca = next(p for p in params if isinstance(p, str) and "cancelado por anticipo" in p)
    assert "#888" in marca and "3,000.00" in marca
    # mov_doble tipo='cheque_cancelado_por_anticipo' (reversible individual)
    md = [
        (sql, params) for sql, params in s.execute_returning_calls
        if "insert into scintela.mov_doble" in " ".join(sql.split()).lower()
    ]
    assert len(md) == 1
    assert "cheque_cancelado_por_anticipo" in md[0][1]
    assert 1000.0 in md[0][1]  # importe del cheque cancelado


def test_cancelar_borra_posdat_hermana(monkeypatch):
    from modules.cheques import queries as q
    s = _wire(monkeypatch, _DBStubCancelar(cheque_row=_cheque_vivo(stat="P")))
    q.cancelar_por_anticipo(id_cheque=777, codigo_cli="JTX", monto_anticipo=1000.0)
    dels = [
        sql for sql, _ in s.execute_calls
        if "delete from scintela.posdat" in " ".join(sql.split()).lower()
    ]
    assert len(dels) == 1


def test_cancelar_cheque_inexistente_error(monkeypatch):
    from modules.cheques import queries as q
    _wire(monkeypatch, _DBStubCancelar(cheque_row=None))
    with pytest.raises(ValueError, match="no existe"):
        q.cancelar_por_anticipo(id_cheque=1, codigo_cli="JTX", monto_anticipo=100.0)


def test_cancelar_cheque_de_otro_cliente_error(monkeypatch):
    from modules.cheques import queries as q
    _wire(monkeypatch, _DBStubCancelar(cheque_row=_cheque_vivo(codigo_cli="BED")))
    with pytest.raises(ValueError, match="no se puede cancelar con este anticipo"):
        q.cancelar_por_anticipo(id_cheque=777, codigo_cli="JTX", monto_anticipo=100.0)


def test_cancelar_stat_no_vivo_error(monkeypatch):
    from modules.cheques import queries as q
    # 'B' (depositado) NO está en el grupo vivo Z123PD
    _wire(monkeypatch, _DBStubCancelar(cheque_row=_cheque_vivo(stat="B")))
    with pytest.raises(ValueError, match="cheques vivos"):
        q.cancelar_por_anticipo(id_cheque=777, codigo_cli="JTX", monto_anticipo=100.0)


def test_cancelar_importe_negativo_error(monkeypatch):
    from modules.cheques import queries as q
    # espejo NB=98 (importe negativo) no se cancela por anticipo
    _wire(monkeypatch, _DBStubCancelar(cheque_row=_cheque_vivo(importe=-500.0)))
    with pytest.raises(ValueError, match="notas de crédito"):
        q.cancelar_por_anticipo(id_cheque=777, codigo_cli="JTX", monto_anticipo=100.0)


def test_cancelar_con_aplicaciones_error(monkeypatch):
    from modules.cheques import queries as q
    _wire(monkeypatch, _DBStubCancelar(
        cheque_row=_cheque_vivo(),
        aplicaciones=[{"id_chequexfact": 1}],
    ))
    with pytest.raises(ValueError, match="aplicado a factura"):
        q.cancelar_por_anticipo(id_cheque=777, codigo_cli="JTX", monto_anticipo=100.0)


def test_cancelar_stats_vivos_todos_pasan(monkeypatch):
    from modules.cheques import queries as q
    for st in ("Z", "1", "2", "3", "P", "D"):
        s = _wire(monkeypatch, _DBStubCancelar(cheque_row=_cheque_vivo(stat=st)))
        r = q.cancelar_por_anticipo(
            id_cheque=777, codigo_cli="JTX", monto_anticipo=1000.0,
        )
        assert r["stat_nuevo"] == "X", f"stat {st} debería ser cancelable"
