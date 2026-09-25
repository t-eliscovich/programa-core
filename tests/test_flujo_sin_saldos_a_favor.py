"""Flujo: los saldos a favor de clientes (espejo negativo, no_banco=98) no
entran como egreso (Tamara 2026-09-25). El cheque entero ya suma; el espejo
se consume contra facturas futuras y no es plata que sale."""
from modules.informes import queries


def test_query_de_cheques_excluye_espejos_negativos(monkeypatch):
    vistos = []

    def fake_fetch_all(sql, params=None):
        vistos.append(sql)
        return []

    monkeypatch.setattr(queries.db, "fetch_all", fake_fetch_all)
    queries.flujo_items_dbase()
    sql_cheques = next(s for s in vistos if "scintela.cheque" in s)
    assert "NOT (COALESCE(no_banco, 0) = 98 AND importe < 0)" in sql_cheques
