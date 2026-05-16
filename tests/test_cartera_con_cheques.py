"""Test que la query de cartera incluye los cheques en cartera (paridad dBase).

Verifica el SHAPE del SQL emitido — el query real corre contra Postgres en
los tests `@pytest.mark.db`. Acá nos importa que el WITH esté presente y que
la query reste cheques_en_cartera del saldo total.
"""
from __future__ import annotations

from typing import Any


class _CapturingDB:
    """Captura la query y devuelve filas pre-cargadas."""

    def __init__(self, *, rows_for_all=None, row_for_one=None):
        self.rows_for_all = list(rows_for_all or [])
        self.row_for_one = row_for_one
        self.queries_seen: list[str] = []

    def fetch_all(self, sql: str, params: Any = None, conn=None):
        self.queries_seen.append(sql)
        return list(self.rows_for_all)

    def fetch_one(self, sql: str, params: Any = None, conn=None):
        self.queries_seen.append(sql)
        return dict(self.row_for_one) if self.row_for_one else None

    def execute(self, *a, **kw):
        return 0


def test_aging_buckets_query_incluye_cheques_en_cartera(monkeypatch):
    import db as db_mod
    from modules.cartera import queries

    fake = _CapturingDB(rows_for_all=[])
    monkeypatch.setattr(db_mod, "fetch_all", fake.fetch_all)
    monkeypatch.setattr(db_mod, "fetch_one", fake.fetch_one)

    queries.aging_buckets()

    assert len(fake.queries_seen) == 1
    sql = fake.queries_seen[0].lower()
    # CTE de cheques en cartera presente
    assert "with cheques_cli" in sql
    # Filtra los stats correctos
    for s in ("'z'", "'1'", "'2'", "'3'", "'p'", "'d'", "'a'"):
        assert s in sql
    # La cartera por cliente RESTA cheques
    assert "f.saldo), 0) - coalesce(max(cc.en_cartera)" in " ".join(sql.split())


def test_aging_totales_query_incluye_cheques_en_cartera(monkeypatch):
    import db as db_mod
    from modules.cartera import queries

    fake = _CapturingDB(row_for_one={
        "b0_30": 100, "b31_60": 200, "b61_90": 50, "b90_plus": 0,
        "saldo_facturas": 350, "cheques_en_cartera": 100, "total": 250,
        "n_facturas": 5, "n_clientes": 3,
    })
    monkeypatch.setattr(db_mod, "fetch_all", fake.fetch_all)
    monkeypatch.setattr(db_mod, "fetch_one", fake.fetch_one)

    res = queries.aging_totales()

    sql = fake.queries_seen[0].lower()
    assert "with cheques_total" in sql
    assert res["cheques_en_cartera"] == 100
    assert res["saldo_facturas"] == 350
    assert res["total"] == 250  # 350 - 100
    # Estructura completa
    for k in ("b0_30", "b31_60", "b61_90", "b90_plus",
              "n_facturas", "n_clientes"):
        assert k in res


def test_aging_totales_devuelve_estructura_estable_con_db_vacia(monkeypatch):
    import db as db_mod
    from modules.cartera import queries

    fake = _CapturingDB(row_for_one=None)
    monkeypatch.setattr(db_mod, "fetch_all", fake.fetch_all)
    monkeypatch.setattr(db_mod, "fetch_one", fake.fetch_one)

    res = queries.aging_totales()
    assert res["total"] == 0.0
    assert res["cheques_en_cartera"] == 0.0
    assert res["saldo_facturas"] == 0.0
    assert res["n_facturas"] == 0
