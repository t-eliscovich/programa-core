"""Ficha de compra — /compras/<id>.

TMT 2026-08-03 (dueña): "cuando clické el link de compra 473 me dice 404 no
encontrado". El link del Historial apuntaba a /compras/473 y la ruta no
existía. Estos tests fijan el contrato de la ficha nueva:

1. GET /compras/<id_interno> → 200 y renderea el template.
2. GET /compras/<numero> (cuando el id no existe) → 200, cae a por_numero.
3. GET /compras/<inexistente> → 404 (no 500).
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tests.test_routes_smoke import build_app  # noqa: E402

COMPRA = {
    "id_compra": 473, "fecha": date(2026, 8, 3), "fechad": date(2026, 9, 2),
    "codigo_prov": "AI", "tipo": "H", "comprobante": "0001-15", "numero": 15,
    "kg": 1200, "importe": 15527.85, "concepto": "HILADO", "clave": "",
    "no_banco": 0, "stat": "", "observacion": "",
    "fecha_crea": date(2026, 8, 3), "usuario_crea": "andres",
    "proveedor": "AGRO INDUSTRIAL", "banco": "",
}


@pytest.fixture
def app_con_compra(monkeypatch):
    app, deshacer = build_app()
    # `app.py` hace `from auth import load_logged_in_user` en el import, así
    # que pisar `auth.load_logged_in_user` sólo funciona si app.py todavía no
    # se importó (o sea: sólo si este archivo corre primero). En la suite
    # completa ya está importado. Por eso registramos NUESTRO before_request
    # después de create_app: corre siempre, sin depender del orden.
    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {
            "id_usuario": 1, "username": "test",
            "id_rol": 1, "nombre_rol": "Dueño", "activo": True,
        }
        g.permisos = {"*"}

    try:
        yield app
    finally:
        deshacer()


def _patch_queries(monkeypatch, *, por_id=None, por_numero=None):
    from modules.compras import queries as q
    monkeypatch.setattr(q, "por_id", lambda i: por_id)
    monkeypatch.setattr(q, "por_numero", lambda n: por_numero)
    monkeypatch.setattr(q, "movimientos", lambda i, limite=100: [])


def test_detalle_por_id_interno_renderea(app_con_compra, monkeypatch):
    _patch_queries(monkeypatch, por_id=COMPRA)
    rv = app_con_compra.test_client().get("/compras/473")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "Compra N° 15" in body
    assert "AGRO INDUSTRIAL" in body


def test_detalle_cae_a_numero_si_el_id_no_existe(app_con_compra, monkeypatch):
    _patch_queries(monkeypatch, por_id=None, por_numero=COMPRA)
    rv = app_con_compra.test_client().get("/compras/15")
    assert rv.status_code == 200
    assert "Compra N° 15" in rv.get_data(as_text=True)


def test_detalle_inexistente_da_404_no_500(app_con_compra, monkeypatch):
    _patch_queries(monkeypatch, por_id=None, por_numero=None)
    rv = app_con_compra.test_client().get("/compras/999999")
    assert rv.status_code == 404


def test_detalle_muestra_los_movimientos(app_con_compra, monkeypatch):
    from modules.compras import queries as q
    monkeypatch.setattr(q, "por_id", lambda i: COMPRA)
    monkeypatch.setattr(q, "movimientos", lambda i, limite=100: [{
        "id_mov_doble": 19860, "fecha_operacion": date(2026, 8, 3),
        "tipo": "anticipo_a_compra", "importe": 15527.85,
        "concepto": "AI 15 · 1 anticipo(s) → compra #473",
        "usuario": "andres", "estado": "activo",
        "origen_table": "dolares", "origen_id": 3209,
        "destino_table": "compra", "destino_id": 473,
    }])
    body = app_con_compra.test_client().get("/compras/473").get_data(as_text=True)
    assert "Anticipo A Compra" in body
    assert "15.527,85" in body
