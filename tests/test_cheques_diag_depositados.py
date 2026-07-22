"""Test de /cheques/diag/depositados-sin-movimiento — ruta de diagnóstico que
lista cheques en estado DEPOSITADO sin movimiento bancario 'DE' ligado.

No toca Postgres: monkeypatchea db.fetch_all para devolver filas fake para la
query de diagnóstico y delega el resto (login/permisos) al FakeDB.
"""
from __future__ import annotations

from datetime import date

import bcrypt

import db as _db


def _fake_rows():
    return [
        {
            "id_cheque": 100410, "no_cheque": "H1234", "codigo_cli": "LTM",
            "importe": 879.96, "stat": "B", "no_banco": 10, "banco": "PICHINCHA",
            "fecha": date(2026, 7, 20), "usuario_crea": "alex",
        },
    ]


def _patch(monkeypatch, fake_db, rows):
    anterior = fake_db.fetch_all

    def fake_fetch_all(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.cheque c" in s and "not exists" in s:
            return list(rows)
        return anterior(sql, params, conn=conn)

    monkeypatch.setattr(_db, "fetch_all", fake_fetch_all)


def _login(client, fake_db):
    rid = fake_db.add_role("Dueño", ["*"])
    pw = bcrypt.hashpw(b"secret123", bcrypt.gensalt())
    fake_db.add_user("tamara", pw, rid)
    client.post("/login", data={"username": "tamara", "password": "secret123"})


def test_diag_render_html(client, fake_db, monkeypatch):
    _patch(monkeypatch, fake_db, _fake_rows())
    _login(client, fake_db)

    resp = client.get("/cheques/diag/depositados-sin-movimiento")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "100410" in html
    assert "LTM" in html
    assert "879.96" in html


def test_diag_json(client, fake_db, monkeypatch):
    _patch(monkeypatch, fake_db, _fake_rows())
    _login(client, fake_db)

    resp = client.get("/cheques/diag/depositados-sin-movimiento?formato=json")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["n"] == 1
    ch = data["cheques"][0]
    assert ch["id_cheque"] == 100410
    assert ch["codigo_cli"] == "LTM"
    assert ch["fecha"] == "2026-07-20"


def test_diag_vacio(client, fake_db, monkeypatch):
    _patch(monkeypatch, fake_db, [])
    _login(client, fake_db)

    resp = client.get("/cheques/diag/depositados-sin-movimiento")
    assert resp.status_code == 200
    assert "sin cheques depositados sin movimiento" in resp.get_data(as_text=True)
