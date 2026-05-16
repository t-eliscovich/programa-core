"""Test de paridad — cheque → balance.

Cadena verificada:
  1. ALTA cheque cartera Z, $400.
  2. APLICAR a factura JTX $1000 → factura.saldo=600, stat=A.
  3. TRANSICIONAR cheque Z→B (depositado Pichincha) → INSERT tx_bancarias.
  4. TRANSICIONAR B→9 (rebotado) → INSERT posdat + cliente.stop=S.
  5. ANULAR_POR_ERROR_DE_CARGA un cheque Z → stat=X, no marca stop.

Stubs — no toca Postgres. Verifica los SQL emitidos.
"""
from __future__ import annotations

import contextlib
from datetime import date
from typing import Any

import pytest


class _ParidadCheque:
    def __init__(self):
        self.cheque: dict | None = None
        self.facturas: dict[int, dict] = {}
        self.aplic: list[dict] = []
        self.executes: list[tuple[str, tuple]] = []
        self.execute_returnings: list[tuple[str, tuple]] = []
        self.next_tx_id = 1000
        self.next_caja_id = 2000
        self.next_cheque_id = 100

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        params = tuple(params or ())
        if "from scintela.cheque where id_cheque" in s:
            return dict(self.cheque) if self.cheque else None
        if "from scintela.factura where id_factura" in s:
            id_f = params[0] if params else None
            return dict(self.facturas[id_f]) if id_f in self.facturas else None
        if "from scintela.cliente where codigo_cli" in s:
            return {"pago": 30}
        if (
            "from scintela.transacciones_bancarias" in s
            and "order by fecha desc, id_transaccion desc" in s
        ):
            return None  # banco vacío
        if (
            "from scintela.caja" in s
            and "order by fecha desc, id_caja desc" in s
        ):
            return None
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.chequesxfact where id_cheque" in s:
            return [dict(a) for a in self.aplic]
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.execute_returnings.append((sql, tuple(params or ())))
        s = " ".join((sql or "").split()).lower()
        if "insert into scintela.cheque" in s:
            id_c = self.next_cheque_id
            self.next_cheque_id += 1
            return {"id_cheque": id_c, "no_cheque": params[0]}
        if "insert into scintela.transacciones_bancarias" in s:
            tx = self.next_tx_id
            self.next_tx_id += 1
            return {"id_transaccion": tx}
        if "insert into scintela.caja" in s:
            cid = self.next_caja_id
            self.next_caja_id += 1
            return {"id_caja": cid}
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield object()

    def apply_to(self, monkeypatch, db_mod):
        monkeypatch.setattr(db_mod, "fetch_one", self.fetch_one)
        monkeypatch.setattr(db_mod, "fetch_all", self.fetch_all)
        monkeypatch.setattr(db_mod, "execute", self.execute)
        monkeypatch.setattr(db_mod, "execute_returning", self.execute_returning)
        monkeypatch.setattr(db_mod, "tx", self.tx)


@pytest.fixture(autouse=True)
def _no_periodo_guard(monkeypatch):
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda f: None)


def _all_sqls(fake):
    sqls = "\n".join(" ".join((s or "").split()).lower() for s, _ in fake.executes)
    rets = "\n".join(" ".join((s or "").split()).lower() for s, _ in fake.execute_returnings)
    return sqls + "\n" + rets


def test_paridad_cheque_alta_aplicar_depositar_rebotar(monkeypatch):
    """E2E del ciclo de cheque feliz + rebote. Verifica que cada paso
    impacta los SQL correctos para mantener BALANCE/CARTERA en paridad."""
    import db as db_mod
    from modules.cheques import queries

    fake = _ParidadCheque()
    # Setear factura con saldo $1000
    fake.facturas[55] = {
        "id_factura": 55, "numf": 5500,
        "importe": 1000, "abono": 0, "saldo": 1000, "stat": "Z",
    }
    fake.apply_to(monkeypatch, db_mod)

    # === Step 1: ALTA cheque JTX $400 ===
    creada = queries.crear(
        fecha=date(2026, 4, 30),
        codigo_cli="JTX",
        no_cheque="00100",
        importe=400,
        usuario="tmt",
    )
    assert creada
    sqls = _all_sqls(fake)
    assert "insert into scintela.cheque" in sqls

    # Inyectamos el cheque para los siguientes pasos
    id_cheque = creada.get("id_cheque") or 100
    fake.cheque = {
        "id_cheque": id_cheque, "no_cheque": "00100", "stat": "Z",
        "codigo_cli": "JTX", "importe": 400, "no_banco": None,
        "fechad": date(2026, 4, 30), "banco": None, "concepto": None,
    }

    # === Step 2: APLICAR cheque a factura ===
    queries.aplicar_a_factura(
        id_cheque=id_cheque,
        aplicaciones=[{"id_fact": 55, "importe": 400}],
        usuario="tmt",
    )
    sqls = _all_sqls(fake)
    assert "insert into scintela.chequesxfact" in sqls
    assert "update scintela.factura" in sqls

    # === Step 3: TRANSICIONAR Z→B (depositar Pichincha) ===
    res = queries.transicionar_stat(id_cheque, stat_destino="B", no_banco=1, usuario="tmt")
    assert res["stat_nuevo"] == "B"
    assert res["side_effect_id"] is not None
    sqls = _all_sqls(fake)
    assert "insert into scintela.transacciones_bancarias" in sqls

    # Inyectamos cheque depositado
    fake.cheque = {
        "id_cheque": id_cheque, "no_cheque": "00100", "stat": "B",
        "codigo_cli": "JTX", "importe": 400, "no_banco": 1,
        "fechad": date(2026, 4, 30), "banco": "Pichincha", "concepto": None,
    }

    # === Step 4: TRANSICIONAR B→9 (rebotado) ===
    res = queries.transicionar_stat(id_cheque, stat_destino="9",
                                       motivo="rebotado banco", usuario="tmt")
    assert res["stat_nuevo"] == "9"
    sqls = _all_sqls(fake)
    assert "insert into scintela.posdat" in sqls
    # Cliente debería pasar a stop
    assert "update scintela.cliente" in sqls and "stop='s'" in sqls


def test_paridad_anular_error_carga_no_marca_stop(monkeypatch):
    """Crítico: anular cheque por error administrativo NO marca cliente.stop."""
    import db as db_mod
    from modules.cheques import queries

    fake = _ParidadCheque()
    fake.cheque = {
        "id_cheque": 50, "no_cheque": "TST", "stat": "Z",
        "codigo_cli": "JTX", "importe": 100, "no_banco": None,
        "fechad": date(2026, 4, 30), "banco": None,
    }
    fake.apply_to(monkeypatch, db_mod)

    queries.anular_por_error_de_carga(
        50, motivo="cheque cargado dos veces por error", usuario="tmt"
    )
    sqls = _all_sqls(fake)
    # Cheque pasó a X
    assert "stat='x'" in sqls
    # NO se actualizó cliente.stop
    cliente_sqls = "\n".join(
        " ".join(s.split()).lower() for s, _ in fake.executes
        if "update scintela.cliente" in " ".join(s.split()).lower()
    )
    assert "stop='s'" not in cliente_sqls


def test_paridad_anular_error_carga_b_inserta_compensacion_banco(monkeypatch):
    """Cheque depositado anulado por error → INSERT ND compensatoria."""
    import db as db_mod
    from modules.cheques import queries

    fake = _ParidadCheque()
    fake.cheque = {
        "id_cheque": 50, "no_cheque": "TST", "stat": "B",
        "codigo_cli": "JTX", "importe": 100, "no_banco": 1,
        "fechad": date(2026, 4, 30), "banco": "Pichincha",
    }
    fake.apply_to(monkeypatch, db_mod)

    res = queries.anular_por_error_de_carga(
        50, motivo="importe mal cargado, era 10 no 100", usuario="tmt"
    )
    assert res["compensacion"]["tipo"] == "banco"
    sqls = _all_sqls(fake)
    assert "insert into scintela.transacciones_bancarias" in sqls
    # El concepto debería contener "ANUL"
    encontrado = False
    for _sql, params in fake.execute_returnings:
        for p in params:
            if isinstance(p, str) and "ANUL" in p:
                encontrado = True
    assert encontrado
