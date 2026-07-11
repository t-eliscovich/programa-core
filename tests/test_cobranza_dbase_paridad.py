"""Paridad ALTAS.PRG para cobranza con bancos virtuales (TMT 2026-06-11).

Pedido dueña: "fijate que hace el dbase y hagamos lo mismo (con todos los
codigos)". Comportamiento canónico (ALTAS.PRG):

  90/91 → stat 'B' + APPEND movimiento bancario DOC='DE' en el banco REAL
          (L170-186) con numreferencia=doc_banco + link chequextransaccion.
  99    → stat 'C' (PASOCAJA L870-893) + entrada en CAJA "CH."+cliente.
  97    → espejo negativo NB=98, BANCO='ANTICIPO', FECHAD+30 (L156).
  95    → cancela anticipo: cheque y espejo NB=97/98 → stat 'X' (L159-168).
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import date, timedelta

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


class _DBStub:
    def __init__(self, espejo_95=None):
        self.execute_returning_calls: list[tuple] = []
        self.executes: list[tuple] = []
        self.espejo_95 = espejo_95
        self._next_id = 100

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.banco" in s:
            return {"no_banco": 10}  # resolver banco real
        if "no_banco in (97, 98)" in s:
            return self.espejo_95
        return None

    def fetch_all(self, sql, params=None, conn=None):
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((" ".join(sql.split()).lower(), tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.execute_returning_calls.append(
            (" ".join(sql.split()).lower(), tuple(params or ()))
        )
        s = self.execute_returning_calls[-1][0]
        if "insert into scintela.cheque" in s:
            self._next_id += 1
            return {"id_cheque": self._next_id, "no_cheque": "100"}
        if "insert into scintela.caja" in s:
            return {"id_caja": 7}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()


@pytest.fixture
def env(monkeypatch):
    def _mk(espejo_95=None):
        import bank_helpers
        import mov_doble
        from modules.cheques import queries as cq

        stub = _DBStub(espejo_95=espejo_95)
        monkeypatch.setattr(cq, "db", stub)
        monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **kw: None)
        monkeypatch.setattr(mov_doble, "registrar", lambda **kw: None)
        bank_calls = []

        import inspect as _inspect
        _firma_real = _inspect.signature(bank_helpers.insert_movimiento_bancario)

        def _fake_insert_mov(conn, **kw):
            # TMT 2026-06-12: validar contra la firma REAL — un stub que
            # acepta cualquier kwarg dejo pasar un TypeError a prod
            # (faltaba importe= en el insert de crear 90/91).
            _firma_real.bind(conn, **kw)
            bank_calls.append(kw)
            return {"id_transaccion": 555, "saldo_nuevo": 0.0}

        monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario", _fake_insert_mov)
        return cq, stub, bank_calls

    return _mk


def _params_cheque_insert(stub, idx=0):
    return stub.execute_returning_calls[idx][1]


def test_90_crea_mov_banco_de_con_referencia_y_link(env):
    cq, stub, bank_calls = env()
    r = cq.crear(
        fecha=date(2026, 6, 11), codigo_cli="gpu", no_cheque="",
        importe=4000, no_banco=90, doc_banco="155032144",
    )
    # cheque quedo B
    assert "B" in _params_cheque_insert(stub)
    # mov banco DOC='DE' al banco REAL con la referencia de la duena
    assert len(bank_calls) == 1
    mv = bank_calls[0]
    assert mv["documento"] == "DE"
    assert mv["no_banco"] == 10  # real (lookup), no el virtual 90
    assert mv["numreferencia"] == 155032144  # int: la columna es INTEGER
    assert abs(mv["importe"] - 4000) < 0.01
    assert mv["concepto"].startswith("1 ch.GPU")
    # link chequextransaccion
    assert any("insert into scintela.chequextransaccion" in s for s, _ in stub.executes)
    assert r.get("id_transaccion_deposito") == 555


def test_90_negativo_queda_z_sin_mov_banco(env):
    cq, stub, bank_calls = env()
    cq.crear(
        fecha=date(2026, 6, 11), codigo_cli="PIB", no_cheque="",
        importe=-177.17, no_banco=90,
    )
    assert "Z" in _params_cheque_insert(stub)
    assert bank_calls == []


def test_99_efectivo_stat_c_y_caja_ch_cliente(env):
    cq, stub, bank_calls = env()
    cq.crear(
        fecha=date(2026, 6, 11), codigo_cli="GPU", no_cheque="",
        importe=250, no_banco=99,
    )
    params = _params_cheque_insert(stub)
    assert "C" in params and "B" not in params
    cajas = [c for c in stub.execute_returning_calls if "insert into scintela.caja" in c[0]]
    assert len(cajas) == 1
    assert "CH.GPU" in cajas[0][1]
    assert bank_calls == []  # efectivo va a caja, no a banco


def test_97_espejo_nb98_anticipo_fechad_mas_30(env):
    cq, stub, _ = env()
    cq.crear(
        fecha=date(2026, 6, 11), codigo_cli="JTX", no_cheque="100",
        importe=500, no_banco=97, es_anticipo=True,
    )
    inserts = [c for c in stub.execute_returning_calls if "insert into scintela.cheque" in c[0]]
    assert len(inserts) == 2
    espejo = inserts[1][1]
    assert -500.0 in espejo
    assert 98 in espejo
    assert "ANTICIPO" in espejo
    assert date(2026, 6, 11) + timedelta(days=30) in espejo


def test_95_cancela_anticipo_marca_ambos_x(env):
    cq, stub, _ = env(espejo_95={"id_cheque": 42})
    r = cq.crear(
        fecha=date(2026, 6, 11), codigo_cli="JTX", no_cheque="",
        importe=500, no_banco=95,
    )
    assert r.get("id_cheque_anticipo_cancelado") == 42
    assert any("set stat='x'" in s for s, _ in stub.executes)


def test_95_sin_anticipo_warning_y_queda_z(env):
    cq, stub, _ = env(espejo_95=None)
    r = cq.crear(
        fecha=date(2026, 6, 11), codigo_cli="JTX", no_cheque="",
        importe=500, no_banco=95,
    )
    assert "No se encontró el anticipo" in (r.get("warning") or "")
    assert not any("set stat='x'" in s for s, _ in stub.executes)


# ── Cambio de banco emisor con migración de movimientos (TMT 2026-06-11) ──
# Dueña: 'este tendría que ser editable o no? era un depósito' — cheque 99
# EFECTIVO que en realidad fue DEP.PICH. editar(no_banco=90) compensa la
# caja y crea el mov banco + link.

class _DBStubEdit(_DBStub):
    """fetch_one por tabla para el flujo editar() + _migrar_deposito_directo."""

    def __init__(self, ch_row, caja_alta=True, cxt_movs=None):
        super().__init__()
        self.ch_row = ch_row
        self.caja_alta = caja_alta
        self.cxt_movs = cxt_movs or []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque where id_cheque" in s:
            return dict(self.ch_row)
        if "from scintela.banco where no_banco" in s:
            nb = params[0]
            return {"no_banco": nb, "nombre": {90: "DEP.PICH.", 99: "EFECTIVO", 10: "PICHINCHA"}.get(nb, f"B{nb}")}
        if "from scintela.banco where no_banco < 90" in s.replace("  ", " "):
            return {"no_banco": 10}
        if "from scintela.caja" in s and "id_caja" in s:
            return {"id_caja": 77} if self.caja_alta else None
        if "select 1 as x from scintela.chequextransaccion" in s:
            return {"x": 1}  # tiene movimientos
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.chequextransaccion cxt" in s:
            return list(self.cxt_movs)
        return []


def _env_edit(monkeypatch, ch_row, **kw):
    import bank_helpers
    import caja_helpers
    import mov_doble
    from modules.cheques import queries as cq

    stub = _DBStubEdit(ch_row, **kw)
    monkeypatch.setattr(cq, "db", stub)
    monkeypatch.setattr(cq, "asegurar_fecha_abierta", lambda *a, **k: None)
    monkeypatch.setattr(mov_doble, "registrar", lambda **k: None)
    bank_calls, caja_calls = [], []
    # TMT 2026-06-12: stubs ESTRICTOS — validan contra la firma real para
    # que un kwarg faltante (como importe=) no llegue a prod nunca mas.
    import inspect as _inspect
    _f_bank = _inspect.signature(bank_helpers.insert_movimiento_bancario)
    _f_caja = _inspect.signature(caja_helpers.insert_movimiento_caja)

    def _fake_bank(conn, **k):
        _f_bank.bind(conn, **k)
        bank_calls.append(k)
        return {"id_transaccion": 901}

    def _fake_caja(conn, **k):
        _f_caja.bind(conn, **k)
        caja_calls.append(k)
        return {"id_caja": 902}

    monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario", _fake_bank)
    monkeypatch.setattr(caja_helpers, "insert_movimiento_caja", _fake_caja)
    return cq, stub, bank_calls, caja_calls


def test_editar_99_a_90_compensa_caja_y_crea_mov_banco(monkeypatch):
    ch = {"id_cheque": 59626, "no_cheque": "", "stat": "B", "fechad": date(2026, 6, 11),
          "doc_banco": "", "no_banco": 99, "codigo_cli": "GPU", "importe": 350.0,
          "fecha": date(2026, 6, 11)}
    cq, stub, bank_calls, caja_calls = _env_edit(monkeypatch, ch)
    cq.editar(59626, no_banco=90, usuario="tamara")
    # compensó la caja con S y creó la entrada DE en el banco real
    assert any(c["tipo"] == "S" for c in caja_calls)
    assert len(bank_calls) == 1 and bank_calls[0]["documento"] == "DE"
    assert bank_calls[0]["no_banco"] == 10
    # link nuevo + update del cheque a no_banco 90 stat B
    assert any("insert into scintela.chequextransaccion" in s for s, _ in stub.executes)
    upd = [pa for s, pa in stub.executes if "update scintela.cheque" in s and "no_banco=" in s]
    assert upd and 90 in upd[0] and "B" in upd[0]


def test_editar_90_a_99_compensa_banco_y_crea_caja(monkeypatch):
    ch = {"id_cheque": 1, "no_cheque": "", "stat": "B", "fechad": date(2026, 6, 11),
          "doc_banco": "", "no_banco": 90, "codigo_cli": "LMS", "importe": 200.0,
          "fecha": date(2026, 6, 11)}
    cq, stub, bank_calls, caja_calls = _env_edit(
        monkeypatch, ch, cxt_movs=[{"id_transaccion": 55, "no_banco": 10, "importe": 200.0}])
    cq.editar(1, no_banco=99, usuario="tamara")
    # ND compensatorio en el banco viejo + caja E nueva + stat C
    assert any(b["documento"] == "ND" for b in bank_calls)
    assert any(c["tipo"] == "E" and c["concepto"].startswith("CH.LMS") for c in caja_calls)
    assert any("delete from scintela.chequextransaccion" in s for s, _ in stub.executes)
    upd = [pa for s, pa in stub.executes if "update scintela.cheque" in s and "no_banco=" in s]
    assert upd and 99 in upd[0] and "C" in upd[0]


def test_editar_a_banco_normal_con_movs_sigue_bloqueado(monkeypatch):
    ch = {"id_cheque": 2, "no_cheque": "", "stat": "B", "fechad": date(2026, 6, 11),
          "doc_banco": "", "no_banco": 99, "codigo_cli": "GPU", "importe": 350.0,
          "fecha": date(2026, 6, 11)}
    cq, _, _, _ = _env_edit(monkeypatch, ch)
    with pytest.raises(ValueError, match="movimientos de banco/caja"):
        cq.editar(2, no_banco=66, usuario="tamara")
