"""Un token de CSRF vencido en el login no puede ser un callejon sin salida.

TMT 2026-08-05 (duena, por patricio): *"con usuario patricio no puedo
ingresar"* -> la pantalla decia **"Bad Request. The CSRF tokens do not
match."**

Que pasa
--------
`{{ csrf_token() }}` se genera cuando se RENDERIZA el login y se guarda en la
sesion. Si la pestana quedo abierta desde antes (o el navegador sirvio la
pagina de su cache), el token del HTML ya no matchea el de la sesion y
Flask-WTF aborta el POST.

Por que era un callejon sin salida
----------------------------------
Sin errorhandler, Flask-WTF devuelve la pagina cruda de Werkzeug: fondo
blanco, ingles, sin un solo link. Y el remedio real -- pedir el login de nuevo
para que te den un token fresco -- es justo lo que nadie adivina, porque el
boton de ATRAS devuelve la misma pagina vieja con el mismo token muerto. El
usuario queda convencido de que le erro a la contrasena.

Contrato que fijan estos tests:
  1. Un POST al login con token invalido devuelve 400 pero RE-RENDERIZA el
     login (con token fresco), no la pagina de Werkzeug.
  2. El mensaje esta en castellano y dice que reintente.
  3. La pagina de login se manda con `Cache-Control: no-store` -- que era la
     causa de raiz: una copia cacheada trae un token ya muerto.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tests.test_routes_smoke import build_app  # noqa: E402


@pytest.fixture
def cliente_csrf_on():
    """App con CSRF ACTIVO -- el default de la suite lo apaga."""
    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        yield app.test_client()
    finally:
        deshacer()


def test_token_invalido_re_renderiza_el_login(cliente_csrf_on):
    r = cliente_csrf_on.post(
        "/login",
        data={"username": "patricio", "password": "loquesea",
              "csrf_token": "un-token-viejo-que-ya-no-vale"},
    )
    assert r.status_code == 400
    cuerpo = r.get_data(as_text=True)
    # Volvio el formulario de login, no la pagina de Werkzeug.
    assert 'name="csrf_token"' in cuerpo
    assert 'name="password"' in cuerpo
    assert "The CSRF tokens do not match" not in cuerpo


def test_el_mensaje_esta_en_castellano_y_pide_reintentar(cliente_csrf_on):
    r = cliente_csrf_on.post(
        "/login",
        data={"username": "patricio", "password": "loquesea",
              "csrf_token": "viejo"},
    )
    cuerpo = r.get_data(as_text=True).lower()
    assert "de nuevo" in cuerpo or "venci" in cuerpo


def test_el_login_no_se_cachea(cliente_csrf_on):
    """Causa de raiz: una copia cacheada del login trae un token muerto."""
    r = cliente_csrf_on.get("/login")
    assert "no-store" in (r.headers.get("Cache-Control") or "")
