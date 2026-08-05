"""Guardar la ficha de un usuario NO puede desactivarlo solo.

TMT 2026-08-05 (dueña): *"le cambié la clave a maribel y desapareció"*. Se le
cambió la contraseña por `/usuarios/<id>/editar`, se guardó, y maribel salió
del listado de usuarios activos — sin ningún error en pantalla. El flash decía
"Usuario maribel actualizado."

Causa
-----
`usuarios/form.html` manda DOS inputs con el mismo `name="activo"`:

    <input type="hidden"   name="activo" value="0">
    <input type="checkbox" name="activo" value="1" checked>

Es el patrón habitual para que un checkbox DESTILDADO viaje igual (si no, el
navegador simplemente no manda la clave). Pero cuando está TILDADO el browser
postea los dos: `activo=0&activo=1`. Y `request.form` es un MultiDict:
`.get("activo")` devuelve el PRIMER valor, o sea `"0"`.

=> `activo` daba False SIEMPRE, tildado o no. Cualquier guardada de esa
pantalla desactivaba al usuario.

Contrato que fijan estos tests:
  1. Con el checkbox TILDADO (hidden 0 + checkbox 1) el usuario queda ACTIVO.
  2. Con el checkbox DESTILDADO (solo el hidden 0) queda INACTIVO -- desactivar
     a mano tiene que seguir funcionando.
  3. El orden de los inputs en el POST no cambia el resultado.
"""
from __future__ import annotations

import os
import sys

import pytest
from werkzeug.datastructures import MultiDict

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.usuarios import views as usuarios_views  # noqa: E402
from tests.test_routes_smoke import ALL_PERMS, _make_fake_user, build_app  # noqa: E402

USUARIO = {
    "id_usuario": 19,
    "username": "maribel",
    "email": None,
    "id_rol": 12,
    "clave": "MAR",
    "vend": None,
    "activo": True,
}


class _FakeQueries:
    """Stub de modules.usuarios.queries: guarda con que se llamo a editar()."""

    def __init__(self):
        self.editar_kwargs = None

    def por_id(self, id_usuario):
        return dict(USUARIO) if id_usuario == USUARIO["id_usuario"] else None

    def roles_disponibles(self):
        return [{"id_rol": 12, "nombre_rol": "INT"}]

    def vendedores_disponibles(self):
        return []

    def editar(self, id_usuario, **kwargs):
        self.editar_kwargs = kwargs
        return 1


@pytest.fixture
def cliente_y_queries():
    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False

    # ⭐ `build_app()` pisa `auth.load_logged_in_user`, pero `app.py` ya hizo
    # `from auth import load_logged_in_user` al importarse: corriendo la suite
    # ENTERA el módulo ya está importado y el parche NO tiene efecto — el test
    # pasa solo y falla en conjunto (el POST se come un 404 del gate de
    # permisos y `queries.editar` no se llama nunca). Por eso registramos
    # nuestro propio hook DESPUÉS de `create_app()`: éste sí corre siempre, y
    # como se registra último, pisa lo que haya dejado el loader real.
    @app.before_request
    def _login_falso():  # pragma: no cover - infraestructura del test
        from flask import g
        g.user = _make_fake_user()
        g.permisos = set(ALL_PERMS)

    fake = _FakeQueries()
    previo = usuarios_views.queries
    usuarios_views.queries = fake
    try:
        yield app.test_client(), fake
    finally:
        usuarios_views.queries = previo
        deshacer()


def _post(cliente, campos):
    """POST con `campos` como lista de pares -- permite claves repetidas."""
    return cliente.post(
        "/usuarios/%d/editar" % USUARIO["id_usuario"],
        data=MultiDict(campos),
        follow_redirects=False,
    )


def test_checkbox_tildado_deja_al_usuario_activo(cliente_y_queries):
    """El caso que rompio: hidden 0 + checkbox 1 = TILDADO = activo."""
    cliente, fake = cliente_y_queries
    _post(cliente, [
        ("username", "maribel"),
        ("id_rol", "12"),
        ("clave", "MAR"),
        ("vend", ""),
        ("activo", "0"),   # el hidden
        ("activo", "1"),   # el checkbox tildado
    ])
    assert fake.editar_kwargs is not None, "no se llamo a queries.editar"
    assert fake.editar_kwargs["activo"] is True


def test_checkbox_destildado_desactiva(cliente_y_queries):
    """Destildar tiene que seguir desactivando: solo viaja el hidden."""
    cliente, fake = cliente_y_queries
    _post(cliente, [
        ("username", "maribel"),
        ("id_rol", "12"),
        ("clave", "MAR"),
        ("vend", ""),
        ("activo", "0"),
    ])
    assert fake.editar_kwargs is not None
    assert fake.editar_kwargs["activo"] is False


def test_el_orden_de_los_inputs_no_cambia_el_resultado(cliente_y_queries):
    """Si manana alguien reordena el template, el contrato no se mueve."""
    cliente, fake = cliente_y_queries
    _post(cliente, [
        ("username", "maribel"),
        ("id_rol", "12"),
        ("clave", "MAR"),
        ("vend", ""),
        ("activo", "1"),   # checkbox primero
        ("activo", "0"),   # hidden despues
    ])
    assert fake.editar_kwargs is not None
    assert fake.editar_kwargs["activo"] is True


def test_cambiar_solo_la_password_no_toca_el_activo(cliente_y_queries):
    """El caso literal de la duena: entra, escribe la contrasena, guarda."""
    cliente, fake = cliente_y_queries
    _post(cliente, [
        ("username", "maribel"),
        ("id_rol", "12"),
        ("clave", "MAR"),
        ("vend", ""),
        ("activo", "0"),
        ("activo", "1"),
        ("password", "UnaClaveLarga9"),
        ("password_confirm", "UnaClaveLarga9"),
    ])
    assert fake.editar_kwargs is not None
    assert fake.editar_kwargs["activo"] is True
    assert fake.editar_kwargs["password"] == "UnaClaveLarga9"
