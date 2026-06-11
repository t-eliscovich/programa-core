"""Tests para bank_helpers + caja_helpers — running saldo primitives.

No tocan Postgres: monkeypatchean db.* y verifican que los helpers calculen
el saldo running correcto y emitan los SQL correctos.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest


class _FakeBankDB:
    """Fake en memoria de transacciones_bancarias para testear helpers."""

    def __init__(self, filas_pre: list[dict] | None = None):
        # filas_pre: filas existentes en el banco antes del test.
        # Cada fila: {id_transaccion, fecha, documento, importe, saldo,
        #             no_banco, no_cta}
        self.filas: list[dict] = list(filas_pre or [])
        self.next_id = max((f.get("id_transaccion") or 0 for f in self.filas), default=0) + 1
        self.executes: list[tuple[str, tuple]] = []
        self.execute_returnings: list[tuple[str, tuple]] = []

    def fetch_one(self, sql: str, params: Any = None, conn=None):
        s = " ".join((sql or "").split()).lower()
        params = tuple(params or ())
        # recompute_saldos_desde — second lookup por id (no por fecha)
        # SQL: "ORDER BY id_transaccion DESC LIMIT 1" sin fecha en order
        if (
            "from scintela.transacciones_bancarias" in s
            and "order by id_transaccion desc" in s
            and "order by fecha" not in s
        ):
            # params: (no_banco, no_cta, no_cta, ancla_id)
            no_banco, _, _, ancla = params
            candidatos = [
                f for f in self.filas
                if f["no_banco"] == no_banco and f["id_transaccion"] < ancla
            ]
            candidatos.sort(key=lambda f: f["id_transaccion"], reverse=True)
            return {"saldo": candidatos[0]["saldo"]} if candidatos else None
        # saldo_actual (sólo banco) — params (no_banco, no_cta, no_cta)
        if (
            "from scintela.transacciones_bancarias" in s
            and "order by fecha desc, id_transaccion desc" in s
            and len(params) == 3
        ):
            no_banco, _, _ = params
            candidatos = [f for f in self.filas if f["no_banco"] == no_banco]
            candidatos.sort(key=lambda f: (f["fecha"], f["id_transaccion"]), reverse=True)
            return {"saldo": candidatos[0]["saldo"]} if candidatos else None
        # later_row check de insert_movimiento_bancario — "SELECT 1 ... LIMIT 1"
        if s.startswith("select 1 from scintela.transacciones_bancarias"):
            no_banco, _, _, fecha, _, tx_id = params
            for f in self.filas:
                if f["no_banco"] == no_banco and (
                    f["fecha"] > fecha
                    or (f["fecha"] == fecha and f["id_transaccion"] > tx_id)
                ):
                    return {"?column?": 1}
            return None
        # _saldo_previo estricto (solo_dias_anteriores=True) — 4 params,
        # ancla en el cierre del día ANTERIOR (fix backdated 2026-06-11)
        if (
            "from scintela.transacciones_bancarias" in s
            and "order by fecha desc, id_transaccion desc" in s
            and len(params) == 4
        ):
            no_banco, _, _, fecha = params
            candidatos = [
                f for f in self.filas
                if f["no_banco"] == no_banco and f["fecha"] < fecha
            ]
            candidatos.sort(key=lambda f: (f["fecha"], f["id_transaccion"]), reverse=True)
            return {"saldo": candidatos[0]["saldo"]} if candidatos else None
        # _saldo_previo principal — 7 params
        if (
            "from scintela.transacciones_bancarias" in s
            and "order by fecha desc, id_transaccion desc" in s
            and len(params) == 7
        ):
            no_banco, _, _, fecha, _, excluir, _ = params
            candidatos = [
                f for f in self.filas
                if f["no_banco"] == no_banco
                and (
                    f["fecha"] < fecha
                    or (f["fecha"] == fecha and (excluir is None or f["id_transaccion"] < excluir))
                )
            ]
            candidatos.sort(key=lambda f: (f["fecha"], f["id_transaccion"]), reverse=True)
            return {"saldo": candidatos[0]["saldo"]} if candidatos else None
        # SELECT id_transaccion, fecha, documento, importe, ... WHERE id_transaccion = %s (insertar_compensacion lookup)
        if "from scintela.transacciones_bancarias" in s and "where id_transaccion =" in s:
            tx_id = params[0]
            for f in self.filas:
                if f["id_transaccion"] == tx_id:
                    return dict(f)
            return None
        return None

    def fetch_all(self, sql: str, params: Any = None, conn=None):
        s = " ".join((sql or "").split()).lower()
        params = tuple(params or ())
        # walk-forward fetch all rows after ancla
        if "from scintela.transacciones_bancarias" in s and "order by fecha, id_transaccion" in s:
            # params puede ser:
            #   (no_banco, no_cta, no_cta)                   → todo el ledger (1=1)
            #   (no_banco, no_cta, no_cta, ancla_id)         → id_transaccion >= ancla_id
            #   (no_banco, no_cta, no_cta, ancla_fecha)      → fecha >= ancla_fecha
            no_banco = params[0]
            extra = params[3] if len(params) >= 4 else None
            todas = [f for f in self.filas if f["no_banco"] == no_banco]
            if extra is not None:
                if isinstance(extra, int):
                    todas = [f for f in todas if f["id_transaccion"] >= extra]
                elif isinstance(extra, date):
                    todas = [f for f in todas if f["fecha"] >= extra]
            return sorted(todas, key=lambda f: (f["fecha"], f["id_transaccion"]))
        return []

    def execute(self, sql: str, params: Any = None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        s = " ".join((sql or "").split()).lower()
        if "update scintela.transacciones_bancarias" in s and "set saldo" in s:
            # walk-forward update
            saldo, tx_id = params[0], params[1]
            for f in self.filas:
                if f["id_transaccion"] == tx_id:
                    f["saldo"] = saldo
        return 1

    def execute_returning(self, sql: str, params: Any = None, conn=None):
        self.execute_returnings.append((sql, tuple(params or ())))
        s = " ".join((sql or "").split()).lower()
        if "insert into scintela.transacciones_bancarias" in s:
            (
                fecha, documento, concepto, fechad, importe, saldo, stat,
                no_banco, no_cta, prov, numref, clave, usuario,
            ) = params
            tx_id = self.next_id
            self.next_id += 1
            self.filas.append({
                "id_transaccion": tx_id,
                "fecha": fecha, "documento": documento,
                "concepto": concepto, "fechad": fechad,
                "importe": importe, "saldo": saldo, "stat": stat,
                "no_banco": no_banco, "no_cta": no_cta,
                "prov": prov, "numreferencia": numref,
                "clave": clave, "usuario_crea": usuario,
            })
            return {"id_transaccion": tx_id}
        return None

    def apply_to(self, monkeypatch, db_mod):
        monkeypatch.setattr(db_mod, "fetch_one", self.fetch_one)
        monkeypatch.setattr(db_mod, "fetch_all", self.fetch_all)
        monkeypatch.setattr(db_mod, "execute", self.execute)
        monkeypatch.setattr(db_mod, "execute_returning", self.execute_returning)


class _FakeCajaDB:
    """Análogo a _FakeBankDB pero para scintela.caja."""

    def __init__(self, filas_pre: list[dict] | None = None):
        self.filas: list[dict] = list(filas_pre or [])
        self.next_id = max((f.get("id_caja") or 0 for f in self.filas), default=0) + 1
        self.executes: list[tuple[str, tuple]] = []

    def fetch_one(self, sql: str, params: Any = None, conn=None):
        s = " ".join((sql or "").split()).lower()
        params = tuple(params or ())
        # recompute_saldos_desde — primer lookup por id antes de walk
        if (
            "from scintela.caja" in s
            and "order by id_caja desc" in s
            and "order by fecha" not in s
        ):
            (ancla,) = params
            candidatos = [f for f in self.filas if f["id_caja"] < ancla]
            candidatos.sort(key=lambda f: f["id_caja"], reverse=True)
            return {"saldo": candidatos[0]["saldo"]} if candidatos else None
        # saldo_actual — sin params
        if "from scintela.caja" in s and "order by fecha desc nulls last, id_caja desc" in s:
            if not self.filas:
                return None
            row = max(self.filas, key=lambda f: (f["fecha"] or date.min, f["id_caja"]))
            return {"saldo": row["saldo"]}
        # _saldo_previo — 4 params (fecha, fecha, excluir, excluir)
        if (
            "from scintela.caja" in s
            and "order by fecha desc, id_caja desc" in s
            and len(params) == 4
        ):
            fecha, _, excluir, _ = params
            candidatos = [
                f for f in self.filas
                if f["fecha"] < fecha
                or (f["fecha"] == fecha and (excluir is None or f["id_caja"] < excluir))
            ]
            if not candidatos:
                return None
            row = max(candidatos, key=lambda f: (f["fecha"], f["id_caja"]))
            return {"saldo": row["saldo"]}
        return None

    def fetch_all(self, sql: str, params: Any = None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.caja" in s and "order by fecha, id_caja" in s:
            return sorted(self.filas, key=lambda f: (f["fecha"], f["id_caja"]))
        return []

    def execute(self, sql: str, params: Any = None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    def execute_returning(self, sql: str, params: Any = None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "insert into scintela.caja" in s:
            fecha, tipo, importe, concepto, saldo, clave, id_cheque, usuario = params
            cid = self.next_id
            self.next_id += 1
            self.filas.append({
                "id_caja": cid, "fecha": fecha, "tipo": tipo,
                "importe": importe, "concepto": concepto, "saldo": saldo,
                "clave": clave, "id_cheque": id_cheque,
                "usuario_crea": usuario,
            })
            return {"id_caja": cid}
        return None

    def apply_to(self, monkeypatch, db_mod):
        monkeypatch.setattr(db_mod, "fetch_one", self.fetch_one)
        monkeypatch.setattr(db_mod, "fetch_all", self.fetch_all)
        monkeypatch.setattr(db_mod, "execute", self.execute)
        monkeypatch.setattr(db_mod, "execute_returning", self.execute_returning)


# --- bank_helpers tests ---------------------------------------------------


def test_signo_documento_entradas():
    import bank_helpers as bh
    for d in ("DE", "TR", "XX", "NC", "IN"):
        assert bh.signo_documento(d) == 1
    for d in ("CH", "ND", "GS", "PA"):
        assert bh.signo_documento(d) == -1


def test_insert_primera_fila_arranca_de_cero(monkeypatch):
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDB(filas_pre=[])
    fake.apply_to(monkeypatch, db_mod)

    res = bh.insert_movimiento_bancario(
        conn=object(),
        no_banco=1, no_cta=None,
        fecha=date(2026, 4, 30),
        documento="DE",
        importe=100.0,
        concepto="depósito test",
        usuario="tmt",
    )
    assert res["saldo_anterior"] == 0.0
    assert res["saldo_nuevo"] == 100.0
    assert res["signo"] == 1
    assert res["importe"] == 100.0
    assert len(fake.filas) == 1


def test_insert_3_movs_calcula_running_correcto(monkeypatch):
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDB(filas_pre=[])
    fake.apply_to(monkeypatch, db_mod)

    r1 = bh.insert_movimiento_bancario(
        conn=object(), no_banco=1, no_cta=None,
        fecha=date(2026, 4, 28),
        documento="DE", importe=500.0, concepto="dep1",
    )
    r2 = bh.insert_movimiento_bancario(
        conn=object(), no_banco=1, no_cta=None,
        fecha=date(2026, 4, 29),
        documento="CH", importe=200.0, concepto="cheque emitido",
    )
    r3 = bh.insert_movimiento_bancario(
        conn=object(), no_banco=1, no_cta=None,
        fecha=date(2026, 4, 30),
        documento="DE", importe=150.0, concepto="dep2",
    )
    assert r1["saldo_nuevo"] == 500.0
    assert r2["saldo_nuevo"] == 300.0  # 500 - 200
    assert r3["saldo_nuevo"] == 450.0  # 300 + 150


def test_insert_importe_negativo_falla(monkeypatch):
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDB(filas_pre=[])
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError):
        bh.insert_movimiento_bancario(
            conn=object(), no_banco=1, no_cta=None,
            fecha=date(2026, 4, 30),
            documento="DE", importe=-50, concepto="bug",
        )


def test_insert_importe_cero_falla(monkeypatch):
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDB(filas_pre=[])
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError):
        bh.insert_movimiento_bancario(
            conn=object(), no_banco=1, no_cta=None,
            fecha=date(2026, 4, 30),
            documento="DE", importe=0, concepto="bug",
        )


def test_no_banco_requerido(monkeypatch):
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDB(filas_pre=[])
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError):
        bh.insert_movimiento_bancario(
            conn=object(), no_banco=None, no_cta=None,
            fecha=date(2026, 4, 30),
            documento="DE", importe=100, concepto="x",
        )


def test_recompute_saldos_desde_anla_id(monkeypatch):
    """Walk-forward recalcula saldos cuando se inserta al medio."""
    import bank_helpers as bh
    import db as db_mod

    # Pre-existing rows with old saldos (we'll force recompute)
    fake = _FakeBankDB(filas_pre=[
        {"id_transaccion": 1, "fecha": date(2026, 4, 1),
         "documento": "DE", "importe": 100, "saldo": 100,
         "no_banco": 1, "no_cta": None},
        {"id_transaccion": 2, "fecha": date(2026, 4, 2),
         "documento": "CH", "importe": 30, "saldo": 70,
         "no_banco": 1, "no_cta": None},
        # We "manually" insert this at id=3 with WRONG saldo for the test
        {"id_transaccion": 3, "fecha": date(2026, 4, 3),
         "documento": "DE", "importe": 50, "saldo": 999,  # corrupto
         "no_banco": 1, "no_cta": None},
    ])
    fake.apply_to(monkeypatch, db_mod)

    n = bh.recompute_saldos_desde(
        conn=object(), no_banco=1, no_cta=None, ancla_id=3,
    )
    assert n == 1
    fila3 = next(f for f in fake.filas if f["id_transaccion"] == 3)
    assert fila3["saldo"] == 120.0  # 70 + 50


def test_insert_backdated_recompute_ancla_dia_anterior(monkeypatch):
    """Bug TMT 2026-06-11: insert BACKDATED con filas posteriores existentes.

    insert_movimiento_bancario detecta later_row y llama
    recompute_saldos_desde(ancla_fecha=fecha). El ancla del walk debe ser el
    saldo al CIERRE del día ANTERIOR a la fecha ancla — antes del fix,
    _saldo_previo(excluir_id=None) incluía las filas de la propia fecha
    ancla (incluida la recién insertada) y el walk re-aplicaba todo el día
    encima: la cadena corría un día-neto por insert (hero Pichincha llegó
    a 462.916,76 hasta que la mig 0093 lo recompuso).
    """
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDB(filas_pre=[
        {"id_transaccion": 1, "fecha": date(2026, 6, 1),
         "documento": "DE", "importe": 1000, "saldo": 1000,
         "no_banco": 10, "no_cta": None, "usuario_crea": "web"},
        {"id_transaccion": 2, "fecha": date(2026, 6, 5),
         "documento": "CH", "importe": 200, "saldo": 800,
         "no_banco": 10, "no_cta": None, "usuario_crea": "web"},
        {"id_transaccion": 3, "fecha": date(2026, 6, 8),
         "documento": "DE", "importe": 500, "saldo": 1300,
         "no_banco": 10, "no_cta": None, "usuario_crea": "web"},
    ])
    fake.apply_to(monkeypatch, db_mod)

    # Insert backdated al 05/06 (hay filas el 05/06 y el 08/06 después).
    bh.insert_movimiento_bancario(
        conn=object(), no_banco=10, no_cta=None,
        fecha=date(2026, 6, 5),
        documento="DE", importe=100.0,
        concepto="dep backdated", usuario="tmt",
    )

    por_id = {f["id_transaccion"]: f for f in fake.filas}
    # Cadena correcta: 1000 → -200 → +100 → +500
    assert por_id[2]["saldo"] == 800.0    # 1000 - 200
    assert por_id[4]["saldo"] == 900.0    # 800 + 100 (la backdated)
    assert por_id[3]["saldo"] == 1400.0   # 900 + 500
    # El saldo final NO corre un día-neto (bug daba 1300: ancla=900 en vez de 1000)
    assert bh.saldo_actual(no_banco=10) == 1400.0


def test_recompute_ancla_fecha_no_duplica_dia_ancla(monkeypatch):
    """recompute_saldos_desde(ancla_fecha=X) directo: el ancla es el cierre
    del día ANTERIOR a X aunque las filas de X tengan saldos ya escritos."""
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDB(filas_pre=[
        {"id_transaccion": 1, "fecha": date(2026, 6, 1),
         "documento": "DE", "importe": 1000, "saldo": 1000,
         "no_banco": 10, "no_cta": None, "usuario_crea": "web"},
        # saldo corrupto a propósito: si el recompute ancla acá, propaga basura
        {"id_transaccion": 2, "fecha": date(2026, 6, 5),
         "documento": "CH", "importe": 200, "saldo": 9999,
         "no_banco": 10, "no_cta": None, "usuario_crea": "web"},
        {"id_transaccion": 3, "fecha": date(2026, 6, 8),
         "documento": "DE", "importe": 500, "saldo": 9999,
         "no_banco": 10, "no_cta": None, "usuario_crea": "web"},
    ])
    fake.apply_to(monkeypatch, db_mod)

    n = bh.recompute_saldos_desde(
        conn=object(), no_banco=10, no_cta=None,
        ancla_fecha=date(2026, 6, 5),
    )
    assert n == 2
    por_id = {f["id_transaccion"]: f for f in fake.filas}
    assert por_id[2]["saldo"] == 800.0   # ancla 1000 (cierre 01/06), no 9999
    assert por_id[3]["saldo"] == 1300.0


def test_saldo_actual_devuelve_ultimo(monkeypatch):
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDB(filas_pre=[
        {"id_transaccion": 1, "fecha": date(2026, 4, 1),
         "documento": "DE", "importe": 100, "saldo": 100,
         "no_banco": 1, "no_cta": None},
        {"id_transaccion": 2, "fecha": date(2026, 4, 2),
         "documento": "DE", "importe": 50, "saldo": 150,
         "no_banco": 1, "no_cta": None},
    ])
    fake.apply_to(monkeypatch, db_mod)

    assert bh.saldo_actual(no_banco=1) == 150.0
    assert bh.saldo_actual(no_banco=99) == 0.0  # banco que no existe


def test_insertar_compensacion_de_a_nd(monkeypatch):
    """Cuando anulamos un depósito (DE), la compensación es ND con signo opuesto."""
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDB(filas_pre=[
        {"id_transaccion": 1, "fecha": date(2026, 4, 1),
         "documento": "DE", "importe": 100, "saldo": 100,
         "no_banco": 1, "no_cta": None,
         "concepto": "dep original", "prov": "JTX", "numreferencia": 999, "fechad": None},
    ])
    fake.apply_to(monkeypatch, db_mod)

    res = bh.insertar_compensacion(
        conn=object(),
        transaccion_origen_id=1,
        motivo="error de carga",
        usuario="tmt",
    )
    assert res["saldo_nuevo"] == 0.0  # 100 - 100
    # La fila compensatoria existe con doc='ND'
    nuevas = [f for f in fake.filas if f["documento"] == "ND"]
    assert len(nuevas) == 1
    assert nuevas[0]["importe"] == 100


# --- caja_helpers tests --------------------------------------------------


def test_caja_signo_tipo():
    import caja_helpers as ch
    assert ch.signo_tipo("E") == 1
    assert ch.signo_tipo("S") == -1


def test_caja_insert_running_correcto(monkeypatch):
    import caja_helpers as ch
    import db as db_mod

    fake = _FakeCajaDB(filas_pre=[])
    fake.apply_to(monkeypatch, db_mod)

    r1 = ch.insert_movimiento_caja(
        conn=object(), fecha=date(2026, 4, 28),
        tipo="E", importe=200, concepto="ingreso",
    )
    r2 = ch.insert_movimiento_caja(
        conn=object(), fecha=date(2026, 4, 29),
        tipo="S", importe=80, concepto="egreso",
    )
    assert r1["saldo_nuevo"] == 200.0
    assert r2["saldo_nuevo"] == 120.0  # 200 - 80


def test_caja_tipo_invalido_falla(monkeypatch):
    import caja_helpers as ch
    import db as db_mod

    fake = _FakeCajaDB(filas_pre=[])
    fake.apply_to(monkeypatch, db_mod)

    with pytest.raises(ValueError):
        ch.insert_movimiento_caja(
            conn=object(), fecha=date(2026, 4, 30),
            tipo="X", importe=100, concepto="bug",
        )


def test_caja_saldo_actual(monkeypatch):
    import caja_helpers as ch
    import db as db_mod

    fake = _FakeCajaDB(filas_pre=[
        {"id_caja": 1, "fecha": date(2026, 4, 1),
         "tipo": "E", "importe": 100, "saldo": 100},
        {"id_caja": 2, "fecha": date(2026, 4, 2),
         "tipo": "S", "importe": 30, "saldo": 70},
    ])
    fake.apply_to(monkeypatch, db_mod)

    assert ch.saldo_actual() == 70.0
