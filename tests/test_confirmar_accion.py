"""Tests del pattern 'pending action' / confirmación en 2 pasos.

Para cada módulo (facturas, cheques, retenciones, provisiones, posdat),
verifica:
  1) Existe el endpoint GET de confirmación y renderiza 200.
  2) El endpoint POST sin motivo redirige a confirmación (no ejecuta
     la acción).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# Helper: login como Dueño (todos los permisos)
# ---------------------------------------------------------------------------

def _login_as_dueno(app, fake_db):
    """Session like Dueño with every perm used by these routes."""
    perms = [
        "*",
        "facturas.anular",
        "cheques.anular",
        "retenciones.anular",
        "provisiones.editar",
        "posdat.anular",
    ]
    rid = fake_db.add_role("Dueño", perms)
    uid = fake_db.add_user("tmt", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


# ---------------------------------------------------------------------------
# Facturas
# ---------------------------------------------------------------------------

def test_facturas_confirmar_anulacion_get_200(app, fake_db, monkeypatch):
    from modules.facturas import queries as fq
    monkeypatch.setattr(fq, "por_id", lambda _id: {
        "id_factura": 1, "numf": 123, "numf_completo": "001-001-000000123",
        "fecha": None, "codigo_cli": "JTX", "cliente": "Jimenez",
        "importe": 1234.56, "saldo": 500.0, "stat": "Z",
    })
    c = _login_as_dueno(app, fake_db)
    r = c.get("/facturas/1/confirmar-anulacion")
    assert r.status_code == 200
    assert b"001-001-000000123" in r.data
    assert b"Confirmar anulaci" in r.data
    # Motivo input must appear
    assert b'name="motivo"' in r.data


def test_facturas_anular_sin_motivo_redirige_a_confirmacion(app, fake_db, monkeypatch):
    from modules.facturas import queries as fq
    monkeypatch.setattr(fq, "por_id", lambda _id: {
        "id_factura": 1, "numf": 123, "numf_completo": "001-001-000000123",
        "fecha": None, "codigo_cli": "JTX", "cliente": "J",
        "importe": 10, "saldo": 10, "stat": "Z",
    })
    monkeypatch.setattr(fq, "anular", lambda *a, **k: 1)

    c = _login_as_dueno(app, fake_db)
    r = c.post("/facturas/1/anular", data={})  # sin motivo
    # Redirige a la confirmación, NO ejecuta anular.
    assert r.status_code in (302, 303)
    assert "/confirmar-anulacion" in r.headers["Location"]


# ---------------------------------------------------------------------------
# Cheques
# ---------------------------------------------------------------------------

def test_cheques_confirmar_reverso_get_200(app, fake_db, monkeypatch):
    from modules.cheques import queries as cq
    monkeypatch.setattr(cq, "por_id", lambda _id: {
        "id_cheque": 7, "no_cheque": "C-42", "fecha": None,
        "codigo_cli": "JTX", "importe": 1000, "stat": "D",
    })
    c = _login_as_dueno(app, fake_db)
    r = c.get("/cheques/7/confirmar-reverso")
    assert r.status_code == 200
    # Es rebote real (stat D) → menciona STOP
    assert b"STOP" in r.data


def test_cheques_reversar_sin_motivo_redirige(app, fake_db, monkeypatch):
    from modules.cheques import queries as cq
    monkeypatch.setattr(cq, "por_id", lambda _id: {
        "id_cheque": 7, "no_cheque": "C-42", "fecha": None,
        "codigo_cli": "JTX", "importe": 1, "stat": "Z",
    })
    monkeypatch.setattr(cq, "reversar", lambda **kw: {"reversadas": 0})
    c = _login_as_dueno(app, fake_db)
    r = c.post("/cheques/7/reversar", data={})
    assert r.status_code in (302, 303)
    assert "/confirmar-reverso" in r.headers["Location"]


# ---------------------------------------------------------------------------
# Retenciones
# ---------------------------------------------------------------------------

def test_retenciones_confirmar_anulacion_get_200(app, fake_db, monkeypatch):
    from modules.retenciones import queries as rq
    monkeypatch.setattr(rq, "por_id", lambda _id: {
        "id_retencion": 5, "codigo_cli": "JTX", "numf": 123, "rete": 50,
        "fecha": None, "cliente": "Jimenez", "importe_factura": 1000,
        "numf_completo": "001-001-000000123",
    })
    c = _login_as_dueno(app, fake_db)
    r = c.get("/retenciones/5/confirmar-anulacion")
    assert r.status_code == 200
    assert b"Confirmar anulaci" in r.data


def test_retenciones_anular_sin_motivo_redirige(app, fake_db, monkeypatch):
    from modules.retenciones import queries as rq
    monkeypatch.setattr(rq, "por_id", lambda _id: {
        "id_retencion": 5, "codigo_cli": "JTX", "numf": 123, "rete": 50,
        "fecha": None, "cliente": "", "importe_factura": 0,
        "numf_completo": "",
    })
    monkeypatch.setattr(rq, "anular", lambda *a, **k: 1)
    c = _login_as_dueno(app, fake_db)
    r = c.post("/retenciones/5/anular", data={})
    assert r.status_code in (302, 303)
    assert "/confirmar-anulacion" in r.headers["Location"]


# ---------------------------------------------------------------------------
# Provisiones
# ---------------------------------------------------------------------------

def test_provisiones_confirmar_eliminacion_get_200(app, fake_db, monkeypatch):
    from modules.provisiones import queries as pq
    monkeypatch.setattr(pq, "por_id", lambda _id: {
        "id_provisiones": 9, "concepto": "Alquiler",
        "importe": 500, "periodo_aplica": "MENSUAL",
    })
    c = _login_as_dueno(app, fake_db)
    r = c.get("/provisiones/9/confirmar-eliminacion")
    assert r.status_code == 200
    assert b"Alquiler" in r.data


def test_provisiones_eliminar_sin_motivo_redirige(app, fake_db, monkeypatch):
    from modules.provisiones import queries as pq
    monkeypatch.setattr(pq, "por_id", lambda _id: {
        "id_provisiones": 9, "concepto": "X", "importe": 1,
        "periodo_aplica": "MENSUAL",
    })
    monkeypatch.setattr(pq, "eliminar", lambda *a, **k: 1)
    c = _login_as_dueno(app, fake_db)
    r = c.post("/provisiones/9/eliminar", data={})
    assert r.status_code in (302, 303)
    assert "/confirmar-eliminacion" in r.headers["Location"]


# ---------------------------------------------------------------------------
# Posdat
# ---------------------------------------------------------------------------

def test_posdat_confirmar_anulacion_get_200(app, fake_db, monkeypatch):
    from modules.posdat import queries as pq
    monkeypatch.setattr(pq, "por_id", lambda _id: {
        "id_posdat": 3, "num": 101, "prov": "PROV",
        "importe": 2500, "fechad": None, "banc": 0,
    })
    c = _login_as_dueno(app, fake_db)
    r = c.get("/posdat/3/confirmar-anulacion")
    assert r.status_code == 200
    assert b"PROV" in r.data


def test_posdat_anular_sin_motivo_redirige(app, fake_db, monkeypatch):
    from modules.posdat import queries as pq
    monkeypatch.setattr(pq, "por_id", lambda _id: {
        "id_posdat": 3, "num": 101, "prov": "PROV",
        "importe": 1, "fechad": None, "banc": 0,
    })
    monkeypatch.setattr(pq, "anular", lambda *a, **k: 1)
    c = _login_as_dueno(app, fake_db)
    r = c.post("/posdat/3/anular", data={})
    assert r.status_code in (302, 303)
    assert "/confirmar-anulacion" in r.headers["Location"]
