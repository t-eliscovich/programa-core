"""/compras/desde-formulas — lo que el puente va a DESHACER se ve en pantalla.

09/09/2026: una compra del puente cuya factura desapareció de formulas
(huérfana) tiene que aparecer con su importe y su link, y la que sólo cambió de
número tiene que decir "cambió el N°" y no "pendiente".
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.compras import formulas_bridge as fb  # noqa: E402
from tests.test_routes_smoke import build_app  # noqa: E402


@pytest.fixture
def app_duena():
    app, deshacer = build_app()

    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "test", "id_rol": 1,
                  "nombre_rol": "Dueño", "activo": True}
        g.permisos = {"*"}

    try:
        yield app
    finally:
        deshacer()


def _estado(monkeypatch, grupos, pc):
    monkeypatch.setattr(fb.formulas_db, "disponible", lambda: True)
    monkeypatch.setattr(fb.formulas_db, "fetch_all", lambda *a, **k: grupos)
    monkeypatch.setattr(fb.formulas_db, "fetch_one", lambda *a, **k: {"n": 1})
    monkeypatch.setattr(fb.db, "fetch_all", lambda *a, **k: pc)


_PC = {"id_compra": 678, "numero": 10323, "fecha": date(2026, 9, 9),
       "codigo_prov": "AQ", "importe": 273565.79, "concepto": "007-2026      9",
       "usuario_crea": "formulas-auto", "usuario_modifica": None,
       "id_transaccion": None, "cuenta_pagada": None}


def test_pantalla_muestra_la_compra_que_ya_no_esta_en_formulas(app_duena, monkeypatch):
    _estado(monkeypatch, [], [_PC])
    body = app_duena.test_client().get(
        "/compras/desde-formulas?mes=2026-09").get_data(as_text=True)
    assert "ya no" in body and "en formulas" in body
    assert "273.565,79" in body
    assert "/compras/678" in body and "N° 10323" in body
    assert "Anular 1 compra que ya no está en formulas" in body


def test_pantalla_marca_el_cambio_de_numero(app_duena, monkeypatch):
    _estado(monkeypatch, [{"proveedor": "AVQ", "factura": "26-27/414",
                           "fecha": "2026-09-09", "kg": 22850,
                           "importe_siva": 237883.30}], [_PC])
    body = app_duena.test_client().get(
        "/compras/desde-formulas?mes=2026-09").get_data(as_text=True)
    assert "cambió el N°: estaba como 007-2026" in body
    assert "pendiente" not in body.split("<tbody>")[1]
    assert "Corregir el N° de 1 compra" in body
