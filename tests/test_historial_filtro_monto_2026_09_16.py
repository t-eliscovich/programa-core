"""El historial filtra por MONTO, con la regla de la casa.

TMT 2026-09-16 (dueña, parada en /historial?q=mtm): *"acá quiero filtrar por
monto también"*. Un solo campo, como en cheques, facturas, bancos y dólares:
entero "500" → el dólar entero (500,00–500,99); con centavos "500,51" →
exacto. El rango sale de `parsers.monto_rango` y viaja al SQL como
monto_min/monto_max.

Particularidad de esta pantalla: el importe del historial NUNCA trae signo
—las tres ramas del UNION lo guardan en positivo (ABS en caja y bancos)—, así
que un "-500" tipeado tiene que buscar lo mismo que "500" en vez de no traer
nada.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.historial import queries as hq  # noqa: E402

#: Lo mínimo que el encabezado de la pantalla lee del resumen.
_KPIS = {"n_activos": 0, "total_activos": 0, "por_tipo": []}


def _login(app, fake_db, perms):
    rid = fake_db.add_role("Tester", perms)
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


@pytest.mark.parametrize("monto,esperado", [
    ("500", (500.0, 500.999999)),          # entero → el dólar entero
    ("500,51", (500.505, 500.515)),        # centavos → exacto (±medio centavo)
    ("1.250,00", (1249.995, 1250.005)),    # miles con punto, centavos con coma
    ("-500", (500.0, 500.999999)),         # el importe no tiene signo acá
    ("", (None, None)),                    # vacío → no filtra
    ("abc", (None, None)),                 # basura → no filtra, no 500
])
def test_la_pantalla_manda_el_rango_al_sql(app, fake_db, monkeypatch, monto, esperado):
    cap = {}
    monkeypatch.setattr(hq, "listar", lambda **kw: cap.update(kw) or [])
    monkeypatch.setattr(hq, "conteos", lambda **kw: _KPIS)
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/historial", query_string={"monto": monto})
    assert r.status_code == 200
    if esperado[0] is None:
        assert (cap["monto_min"], cap["monto_max"]) == (None, None)
    else:
        assert (cap["monto_min"], cap["monto_max"]) == pytest.approx(esperado)


def test_el_campo_se_queda_con_lo_tipeado(app, fake_db, monkeypatch):
    monkeypatch.setattr(hq, "listar", lambda **kw: [])
    monkeypatch.setattr(hq, "conteos", lambda **kw: _KPIS)
    c = _login(app, fake_db, ["informes.ver"])
    html = c.get("/historial", query_string={"monto": "500,51"}).get_data(as_text=True)
    assert 'name="monto"' in html
    assert 'value="500,51"' in html


def test_el_rango_llega_al_sql(monkeypatch):
    cap = {}

    class _DB:
        def fetch_all(self, sql, params=None):
            cap["sql"], cap["params"] = sql, params
            return []

        def fetch_one(self, sql, params=None):
            return {"?column?": 1}

    monkeypatch.setattr(hq, "db", _DB())
    hq.listar(monto_min=500.0, monto_max=500.999999)
    assert cap["params"]["monto_min"] == 500.0
    assert cap["params"]["monto_max"] == 500.999999
    assert "u.importe >= %(monto_min)s" in cap["sql"]
    assert "u.importe <= %(monto_max)s" in cap["sql"]
