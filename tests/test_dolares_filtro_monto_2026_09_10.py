"""/dolares filtra por MONTO con la misma regla que facturas.

TMT 2026-09-10 (dueña): *"podemos agregar filtro para monto en anticipos, como
el de facturas"*. Un solo campo: entero "500" → el dólar entero (500,00–500,99);
con centavos "500,51" → exacto. El rango lo arma `parsers.monto_rango` (el
mismo de facturas, cheques y bancos) y viaja al SQL como monto_min/monto_max.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.dolares import queries as dq  # noqa: E402


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
    ("", (None, None)),                    # vacío → no filtra
    ("abc", (None, None)),                 # basura → no filtra, no 500
])
def test_la_pantalla_manda_el_rango_al_sql(app, fake_db, monkeypatch, monto, esperado):
    cap = {}
    monkeypatch.setattr(dq, "lista", lambda **kw: cap.update(kw) or [])
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/dolares", query_string={"monto": monto})
    assert r.status_code == 200
    if esperado[0] is None:
        assert (cap["monto_min"], cap["monto_max"]) == (None, None)
    else:
        assert (cap["monto_min"], cap["monto_max"]) == pytest.approx(esperado)


def test_el_campo_se_queda_con_lo_tipeado(app, fake_db, monkeypatch):
    monkeypatch.setattr(dq, "lista", lambda **kw: [])
    c = _login(app, fake_db, ["informes.ver"])
    html = c.get("/dolares", query_string={"monto": "500,51"}).get_data(as_text=True)
    assert 'name="monto"' in html
    assert 'value="500,51"' in html
    assert "Limpiar" in html


def test_por_id_el_monto_se_ignora(app, fake_db, monkeypatch):
    """Pedir ?id= es pedir ESA fila: ningún otro filtro la esconde."""
    cap = {}
    monkeypatch.setattr(dq, "lista", lambda **kw: cap.update(kw) or [])
    c = _login(app, fake_db, ["informes.ver"])
    c.get("/dolares", query_string={"id": "7", "monto": "500"})
    assert cap["monto_min"] is None and cap["monto_max"] is None


def test_el_rango_llega_al_sql(monkeypatch):
    cap = {}

    class _DB:
        def fetch_all(self, sql, params=None):
            cap["sql"], cap["params"] = sql, params
            return []

    monkeypatch.setattr(dq, "db", _DB())
    dq.lista(monto_min=500.0, monto_max=500.999999)
    assert cap["params"]["monto_min"] == 500.0
    assert cap["params"]["monto_max"] == 500.999999
    assert "d.importe" in cap["sql"] and "monto_min" in cap["sql"]
