"""Regresión bug #2 — `salcaj()` calcula el saldo de caja algebraicamente,
no leyendo el `saldo` guardado de la última fila.

El bug (reporte 2026-06-04): salcaj() hacía `SELECT saldo ... ORDER BY fecha
DESC LIMIT 1`. El running `saldo` se mantiene en orden de INSERT (id_caja), no
de fecha; un mov back-dateado dejaba a salcaj leyendo un saldo viejo →
Resultados se desincronizaba de la caja real (episodio del "+$100 fantasma").

El fix reescribió salcaj() con la MISMA fórmula que `caja.saldo_actual()`:
opening + Σ(E−S). Este test fija la forma de la query (mismo enfoque que
test_cartera_con_cheques) para que nadie reintroduzca el patrón viejo.
"""

from __future__ import annotations

from typing import Any


class _CapturaSQL:
    def __init__(self):
        self.sqls: list[str] = []

    def fetch_one(self, sql: str, params: Any = None, conn=None):
        self.sqls.append(sql)
        return {"saldo": 0}


def _norm(sql: str) -> str:
    return " ".join((sql or "").lower().split())


def test_salcaj_usa_formula_algebraica(monkeypatch):
    import db
    from modules.informes import queries

    cap = _CapturaSQL()
    monkeypatch.setattr(db, "fetch_one", cap.fetch_one)
    queries.salcaj()

    sql = _norm(cap.sqls[0])
    # Suma firmada por tipo (E=+, S=−) — robusto contra desorden de fechas.
    assert "sum(case when tipo='e' then importe" in sql
    assert "when tipo='s' then -importe" in sql
    # El opening se toma de la PRIMERA fila (fecha ASC), no de la última.
    assert "order by fecha asc" in sql
    # El bug leía el saldo guardado de la última fila por fecha DESC.
    assert "order by fecha desc" not in sql


def test_salcaj_y_saldo_actual_misma_forma(monkeypatch):
    """salcaj() (Resultados) y caja.saldo_actual() deben usar la MISMA query —
    si divergen, Resultados vuelve a poder desincronizarse de la caja."""
    import db
    from modules.caja import queries as caja_q
    from modules.informes import queries as inf_q

    cap_inf = _CapturaSQL()
    monkeypatch.setattr(db, "fetch_one", cap_inf.fetch_one)
    inf_q.salcaj()
    sql_salcaj = _norm(cap_inf.sqls[0])

    cap_caja = _CapturaSQL()
    monkeypatch.setattr(db, "fetch_one", cap_caja.fetch_one)
    caja_q.saldo_actual()
    sql_saldo = _norm(cap_caja.sqls[0])

    assert sql_salcaj == sql_saldo
