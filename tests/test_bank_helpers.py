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
        # --- ancla por (fecha, id) — fix TMT 2026-08-03 --------------------
        # fecha del ancla a partir de su id
        if s.startswith("select fecha from scintela.transacciones_bancarias"):
            no_banco, _, _, ancla = params
            for f in self.filas:
                if f["no_banco"] == no_banco and f["id_transaccion"] == ancla:
                    return {"fecha": f["fecha"]}
            return None
        # fallback MIN(fecha) cuando el ancla ya no existe
        if "select min(fecha)" in s and "from scintela.transacciones_bancarias" in s:
            no_banco, _, _, ancla = params
            cand = [f["fecha"] for f in self.filas
                    if f["no_banco"] == no_banco and f["id_transaccion"] >= ancla]
            return {"fecha": min(cand)} if cand else {"fecha": None}
        # saldo de arranque = última fila ESTRICTAMENTE anterior en (fecha, id)
        if (
            "from scintela.transacciones_bancarias" in s
            and "(fecha, id_transaccion) <" in s
            and len(params) == 5
        ):
            no_banco, _, _, fecha_a, ancla = params
            cand = [f for f in self.filas
                    if f["no_banco"] == no_banco
                    and (f["fecha"], f["id_transaccion"]) < (fecha_a, ancla)]
            if not cand:
                return None
            row = max(cand, key=lambda f: (f["fecha"], f["id_transaccion"]))
            return {"saldo": row["saldo"]}
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
        # bank_helpers.contar_quiebres — TIENE QUE IR ANTES del walk-forward,
        # porque también termina en "order by fecha, id_transaccion".
        # TMT 2026-08-04: implementarlo de verdad (y no devolver []) hace que
        # el candado de commit se ejerza en TODOS los tests que insertan.
        if s.startswith("with w as"):
            import bank_helpers as _bh
            no_banco = params[0]
            desde = params[3] if len(params) >= 4 else None
            filas = sorted(
                [f for f in self.filas
                 if f["no_banco"] == no_banco and f.get("saldo") is not None],
                key=lambda f: (f["fecha"], f["id_transaccion"]))
            out = []
            for prev, cur in zip(filas, filas[1:]):
                sgn = _bh.signo_documento(cur.get("documento") or "") * float(
                    cur.get("importe") or 0)
                if abs((float(cur["saldo"]) - float(prev["saldo"])) - sgn) <= 0.02:
                    continue
                if desde is not None and cur["fecha"] < desde:
                    continue
                out.append({**cur, "sgn": sgn, "saldo_prev": prev["saldo"],
                            "fecha_prev": prev["fecha"],
                            "concepto_prev": prev.get("concepto")})
            return out
        # walk-forward fetch all rows after ancla
        if "from scintela.transacciones_bancarias" in s and "order by fecha, id_transaccion" in s:
            # params puede ser:
            #   (no_banco, no_cta, no_cta)                   → todo el ledger (1=1)
            #   (no_banco, no_cta, no_cta, ancla_id)         → id_transaccion >= ancla_id
            #   (no_banco, no_cta, no_cta, ancla_fecha)      → fecha >= ancla_fecha
            no_banco = params[0]
            todas = [f for f in self.filas if f["no_banco"] == no_banco]
            if len(params) >= 5:
                # (fecha, id_transaccion) >= (ancla_fecha, ancla_id)
                fecha_a, id_a = params[3], params[4]
                todas = [f for f in todas
                         if (f["fecha"], f["id_transaccion"]) >= (fecha_a, id_a)]
            else:
                extra = params[3] if len(params) >= 4 else None
                if extra is not None:
                    if isinstance(extra, date):
                        todas = [f for f in todas if f["fecha"] >= extra]
                    elif isinstance(extra, int):
                        todas = [f for f in todas if f["id_transaccion"] >= extra]
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
        # --- insert_movimiento_caja: ¿quedó alguna fila DEBAJO de la nueva?
        # TMT 2026-08-14. El fake tiene que contestarla de verdad: si contesta
        # siempre "no", el re-encadenado posterior al insert backdated nunca
        # se ejerce en los tests y el candado se prueba sobre un caso que en
        # producción no existe.
        if s.startswith("select 1 as hay from scintela.caja"):
            fecha, _, id_nuevo = params
            for f in self.filas:
                if f["fecha"] > fecha or (
                    f["fecha"] == fecha and f["id_caja"] > id_nuevo
                ):
                    return {"hay": 1}
            return None
        # relectura del saldo GUARDADO de la fila recién insertada
        if s.startswith("select saldo from scintela.caja where id_caja"):
            (cid,) = params
            for f in self.filas:
                if f["id_caja"] == cid:
                    return {"saldo": f["saldo"]}
            return None
        # --- ancla por (fecha, id) — fix TMT 2026-08-03 -------------------
        if s.startswith("select fecha from scintela.caja"):
            (ancla,) = params
            for f in self.filas:
                if f["id_caja"] == ancla:
                    return {"fecha": f["fecha"]}
            return None
        if "select min(fecha)" in s and "from scintela.caja" in s:
            (ancla,) = params
            cand = [f["fecha"] for f in self.filas if f["id_caja"] >= ancla]
            return {"fecha": min(cand)} if cand else {"fecha": None}
        if "from scintela.caja" in s and "(fecha, id_caja) <" in s and len(params) == 2:
            fecha_a, ancla = params
            cand = [f for f in self.filas
                    if (f["fecha"], f["id_caja"]) < (fecha_a, ancla)]
            if not cand:
                return None
            row = max(cand, key=lambda f: (f["fecha"], f["id_caja"]))
            return {"saldo": row["saldo"]}
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
        # _saldo_previo ESTRICTO (solo_dias_anteriores=True) — 1 param.
        # TMT 2026-08-14: el fake no tenía esta forma (en banco son 4 params,
        # en caja 1) y devolvía None, o sea el walk-forward arrancaba de CERO.
        # Nadie lo notó mientras el fake ignoraba los UPDATE; en cuanto los
        # escribe, el re-encadenado tira toda la cadena al piso.
        if (
            "from scintela.caja" in s
            and "order by fecha desc, id_caja desc" in s
            and len(params) == 1
            and isinstance(params[0], date)
        ):
            (fecha,) = params
            candidatos = [f for f in self.filas if f["fecha"] < fecha]
            if not candidatos:
                return None
            row = max(candidatos, key=lambda f: (f["fecha"], f["id_caja"]))
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
        params = tuple(params or ())
        # caja_helpers.contar_quiebres — VA ANTES del walk-forward, porque
        # los dos terminan en "order by fecha, id_caja". Implementarlo de
        # verdad (y no devolver []) es lo que hace que el candado de commit
        # se ejerza en TODOS los tests que insertan en caja. TMT 2026-08-14.
        if s.startswith("with w as"):
            import caja_helpers as _cj
            apertura = params[0] if params else None
            desde = params[1] if len(params) >= 2 else None
            filas = sorted(
                [f for f in self.filas if f.get("saldo") is not None],
                key=lambda f: (f["fecha"], f["id_caja"]))
            out = []
            previas = [None] + filas[:-1]
            for prev, cur in zip(previas, filas, strict=True):
                sgn = _cj._delta_firmado(cur.get("tipo") or "",
                                         cur.get("importe") or 0)
                saldo_prev = (float(prev["saldo"]) if prev is not None
                              else (None if apertura is None else float(apertura)))
                if saldo_prev is None:
                    continue
                if abs((float(cur["saldo"]) - saldo_prev) - sgn) <= 0.02:
                    continue
                if desde is not None and cur["fecha"] < desde:
                    continue
                out.append({**cur, "sgn": sgn, "saldo_prev": saldo_prev,
                            "fecha_prev": prev["fecha"] if prev else None,
                            "concepto_prev": prev.get("concepto") if prev else None})
            return out
        if "from scintela.caja" in s and "order by fecha, id_caja" in s:
            todas = list(self.filas)
            if len(params) >= 2:
                fecha_a, id_a = params[0], params[1]
                todas = [f for f in todas
                         if (f["fecha"], f["id_caja"]) >= (fecha_a, id_a)]
            elif len(params) == 1 and isinstance(params[0], date):
                todas = [f for f in todas if f["fecha"] >= params[0]]
            return sorted(todas, key=lambda f: (f["fecha"], f["id_caja"]))
        return []

    def execute(self, sql: str, params: Any = None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        s = " ".join((sql or "").split()).lower()
        # TMT 2026-08-14: el fake ignoraba el UPDATE del walk-forward, así que
        # las filas quedaban con el saldo viejo y el re-encadenado se
        # "probaba" sin que nada se moviera. Ahora escribe.
        if "update scintela.caja" in s and "set saldo" in s:
            saldo, cid = params[0], params[1]
            for f in self.filas:
                if f["id_caja"] == cid:
                    f["saldo"] = saldo
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


# ── El código de cliente que se auto-extrae del concepto ──────────────
#
# TMT 2026-08-13. La dueña vio en la conciliación un chip "EQUE" al lado de
# un movimiento cuyo concepto era "Cheque": *"este cheque tiene 4 letras el
# código? eso no existe"* (los códigos de cliente son de 3 letras).
#
# La regex de `insert_movimiento_bancario` tenía el punto OPCIONAL (`ch\.?`)
# y aceptaba de 3 a 5 letras, así que enganchaba el "ch" de adentro de una
# palabra y guardaba la cola como si fuera un cliente. En producción quedaron
# 25 filas del banco 10 así:
#     "CC CHALAN EMPAQUES" → ALAN     "Cheque"        → EQUE
#     "CC CHALNA VARIOS"   → ALNA     "SU LIQ CHAMBA" → AMBA
# Ahora hace falta un separador después del marcador, el código tiene que
# medir EXACTAMENTE 3 letras, y además tiene que existir en scintela.cliente.

class _FakeBankDBConClientes(_FakeBankDB):
    """El fake de siempre, más el padrón de clientes que la nueva
    validación consulta."""

    CLIENTES = {"LTM", "SJG", "MSS"}

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.cliente" in s:
            cod = (params or ("",))[0]
            return {"ok": 1} if cod in self.CLIENTES else None
        return super().fetch_one(sql, params, conn)


def _insertar_con_concepto(monkeypatch, concepto: str):
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDBConClientes(filas_pre=[])
    fake.apply_to(monkeypatch, db_mod)
    bh.insert_movimiento_bancario(
        conn=object(), no_banco=10, no_cta=None,
        fecha=date(2026, 7, 16), documento="CH", importe=505.50,
        concepto=concepto, usuario="andres",
    )
    return fake.filas[-1]["prov"]


@pytest.mark.parametrize("concepto", [
    "Cheque",                       # → EQUE
    "CC CHALAN EMPAQUES",           # → ALAN
    "CC CHALNA VARIOS ACCESORIOS",  # → ALNA
    "SU LIQ CHAMBA",                # → AMBA
    "PROTESTO CHEQUE",
])
def test_no_inventa_codigo_comiendose_el_ch_de_una_palabra(monkeypatch, concepto):
    """Las 4 basuras reales de producción, más una prima."""
    assert _insertar_con_concepto(monkeypatch, concepto) is None, (
        f"{concepto!r} no nombra a ningún cliente — no se guarda código"
    )


@pytest.mark.parametrize(("concepto", "esperado"), [
    ("1 ch.LTM", "LTM"),
    ("3 ch. SJG", "SJG"),
    ("dep. ch.LTM", "LTM"),
    ("tr.MSS", "MSS"),
])
def test_sigue_sacando_el_codigo_cuando_el_concepto_lo_nombra(
        monkeypatch, concepto, esperado):
    """Lo que la extracción vino a resolver en 2026-05-23 sigue andando."""
    assert _insertar_con_concepto(monkeypatch, concepto) == esperado


def test_tres_letras_que_no_son_un_cliente_no_se_guardan(monkeypatch):
    """La regex sola no alcanza: "1 ch. por deposito" da POR, que es una
    palabra, no un cliente. Por eso se confirma contra el padrón."""
    assert _insertar_con_concepto(monkeypatch, "1 ch. por deposito") is None


class _FakeBankDBTodoEsCliente(_FakeBankDB):
    """Padrón que acepta CUALQUIER código: deja a la regex sola frente al
    concepto. Sin esto, la validación contra `cliente` tapa el agujero y el
    test pasa igual con la regex vieja puesta — o sea, no prueba nada."""

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join((sql or "").split()).lower()
        if "from scintela.cliente" in s:
            return {"ok": 1}
        return super().fetch_one(sql, params, conn)


@pytest.mark.parametrize("concepto", [
    "Cheque",                       # la vieja sacaba EQUE
    "CC CHALAN EMPAQUES",           # ALAN
    "CC CHALNA VARIOS ACCESORIOS",  # ALNA
    "SU LIQ CHAMBA",                # AMBA
])
def test_la_regex_sola_ya_no_muerde_adentro_de_una_palabra(monkeypatch, concepto):
    """El candado sobre la REGEX, con el padrón desactivado a propósito."""
    import bank_helpers as bh
    import db as db_mod

    fake = _FakeBankDBTodoEsCliente(filas_pre=[])
    fake.apply_to(monkeypatch, db_mod)
    bh.insert_movimiento_bancario(
        conn=object(), no_banco=10, no_cta=None,
        fecha=date(2026, 7, 16), documento="CH", importe=505.50,
        concepto=concepto, usuario="andres",
    )
    assert fake.filas[-1]["prov"] is None, (
        f"{concepto!r}: la regex volvió a comerse el 'ch' de adentro de una palabra"
    )
