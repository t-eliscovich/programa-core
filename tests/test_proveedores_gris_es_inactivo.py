"""La fila gris de /proveedores es un proveedor DESACTIVADO — y sólo eso.

Tamara 2026-09-17: *"¿por qué algunos proveedores están en negrita y otros en
gris?"*. La plantilla atenuaba toda fila con `activo != 'S'`, pero 'S' es
sólo lo que trajo el dBase: los 76 proveedores creados o editados en el
programa quedan con '1' (el valor que escribe el formulario y `set_activo`),
así que los ACTIVOS de verdad se veían en gris y los legacy en negrita.
Medido el 17/09: 'S' 69 · '1' 76 · '' 2 · '0' ninguno — el gris no quería
decir nada.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.proveedores import views as prov_views  # noqa: E402
from tests.test_routes_smoke import ALL_PERMS, _make_fake_user, build_app  # noqa: E402


def _prov(cod, activo):
    return {
        "id_proveedor": hash(cod) % 1000, "codigo_prov": cod, "nombre": f"PROV {cod}",
        "ruc": None, "telefono": None, "representante": None, "tipo": "H",
        "plazo": None, "retbase": None, "retiva": None, "activo": activo,
    }


FILAS = [_prov("LEG", "S"), _prov("NUEVO", "1"), _prov("VACIO", ""),
         _prov("BAJA", "0"), _prov("NULO", None)]


class _FakeQueries:
    def buscar(self, q, tipo=None, limite=50, offset=0):
        return list(FILAS)

    def contar(self, q, tipo=None):
        return len(FILAS)


@pytest.fixture
def cliente(monkeypatch):
    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False

    @app.before_request
    def _login_falso():  # pragma: no cover - infraestructura del test
        from flask import g
        g.user = _make_fake_user()
        g.permisos = set(ALL_PERMS)

    monkeypatch.setattr(prov_views, "queries", _FakeQueries())
    try:
        yield app.test_client()
    finally:
        deshacer()


def _fila_html(html: str, cod: str) -> str:
    i = html.index(f">{cod}</td>")
    ini = html.rindex("<tr", 0, i)
    return html[ini:i]


def test_solo_el_desactivado_va_en_gris(cliente):
    html = cliente.get("/proveedores").get_data(as_text=True)
    assert "opacity-50" in _fila_html(html, "BAJA")
    assert "Proveedor desactivado" in _fila_html(html, "BAJA")
    for cod in ("LEG", "NUEVO", "VACIO", "NULO"):
        assert "opacity-50" not in _fila_html(html, cod), cod
