"""Scope de datos de VENDEDORES — allowlist fail-closed.

TMT 2026-08-03. Estos tests son la red de seguridad del portal de vendedores:
si alguien afloja el allowlist, acá se rompe.

La invariante que protegen: un usuario CON `vend` cargado sólo puede tocar
/mi-cartera y la infraestructura mínima (login/logout/estáticos). Todo lo
demás —incluidas las 31 rutas que hoy no tienen `@requiere_permiso`— devuelve
404. Y para un usuario SIN `vend` el hook no cambia absolutamente nada.
"""
from __future__ import annotations

import pytest
from flask import g

from scope_vendedor import (
    HOME_VENDEDOR,
    PREFIJOS_INFRA,
    PREFIJOS_PERMITIDOS,
    _path_permitido,
    enforce_scope_vendedor,
    es_vendedor,
    vendedor_de,
)

# --------------------------------------------------------------------------
# vendedor_de / es_vendedor
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "esperado"),
    [
        (None, ""),
        ({}, ""),
        ({"vend": None}, ""),
        ({"vend": ""}, ""),
        ({"vend": "   "}, ""),
        ({"vend": "ppr"}, "PPR"),
        ({"vend": "  ppr  "}, "PPR"),
        ({"vend": "PPR"}, "PPR"),
    ],
)
def test_vendedor_de_normaliza(user, esperado):
    assert vendedor_de(user) == esperado


def test_es_vendedor():
    assert es_vendedor({"vend": "ppr"}) is True
    assert es_vendedor({"vend": " "}) is False
    assert es_vendedor(None) is False


# --------------------------------------------------------------------------
# _path_permitido — el matcheo es por SEGMENTO, no por substring
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("/mi-cartera", True),
        ("/mi-cartera/", True),
        ("/mi-cartera/clientes", True),
        ("/mi-cartera/cliente/TDV/imprimir", True),
        ("/mi-cartera?x=1", True),
        # El agujero clásico de un startswith() ingenuo: una ruta que
        # EMPIEZA igual pero es otra cosa.
        ("/mi-carteras-todas", False),
        ("/mi-cartera-admin", False),
        ("/facturas", False),
        ("/", False),
    ],
)
def test_path_permitido_matchea_por_segmento(path, ok):
    assert _path_permitido(path, PREFIJOS_PERMITIDOS) is ok


def test_infra_incluye_lo_minimo_para_poder_salir():
    # Sin estos, un vendedor no podría desloguearse ni cambiar su contraseña.
    for p in ("/logout", "/login", "/password/cambiar", "/static/app.css"):
        assert _path_permitido(p, PREFIJOS_INFRA), p


# --------------------------------------------------------------------------
# enforce_scope_vendedor — el hook
# --------------------------------------------------------------------------


def _con_user(app, path, user):
    with app.test_request_context(path):
        g.user = user
        return enforce_scope_vendedor()


def test_sin_usuario_es_noop(app):
    assert _con_user(app, "/facturas", None) is None


def test_usuario_normal_es_noop(app):
    """La invariante que protege a Tamara/Andrés/Alex: nada cambia para ellos."""
    for path in ("/facturas", "/cheques", "/informes/balance", "/", "/posdat"):
        assert _con_user(app, path, {"username": "tamara", "vend": None}) is None


def test_vendedor_entra_a_su_portal(app):
    assert _con_user(app, "/mi-cartera", {"vend": "PPR"}) is None
    assert _con_user(app, "/mi-cartera/clientes", {"vend": "PPR"}) is None


def test_vendedor_pasa_por_infra(app):
    assert _con_user(app, "/logout", {"vend": "PPR"}) is None
    assert _con_user(app, "/static/tailwind.css", {"vend": "PPR"}) is None


def test_vendedor_en_la_raiz_va_a_su_portal(app):
    resp = _con_user(app, "/", {"vend": "PPR"})
    assert resp.status_code in (301, 302, 303)
    assert resp.headers["Location"].endswith(HOME_VENDEDOR)


@pytest.mark.parametrize(
    "path",
    [
        # Las 4 de estado de cuenta: hoy sólo piden @requiere_login.
        "/informes/estado-cuenta",
        "/informes/estado-cuenta/grupos",
        "/informes/estado-cuenta/imprimir",
        "/informes/estado-cuenta/TDV",
        # Rutas de ESCRITURA sin @requiere_permiso (auditoría 2026-08-03).
        "/anticipos/nuevo",
        "/anticipos/7/cancelar",
        "/cheques/123/reversar",
        "/cheques/123/transicionar",
        "/historial/55/reverso-inline",
        # Y lo gateado por permiso, por las dudas.
        "/facturas",
        "/cheques",
        "/informes/balance",
        "/usuarios",
        "/admin/dbase-sync",
    ],
)
def test_vendedor_no_llega_a_ninguna_otra_pantalla(app, path):
    resp = _con_user(app, path, {"vend": "PPR"})
    assert resp is not None, f"{path} quedó ABIERTA para un vendedor"
    _html, status = resp
    assert status == 404


def test_una_ruta_nueva_nace_cerrada(app):
    """El punto del allowlist: lo que todavía no existe ya está prohibido."""
    resp = _con_user(app, "/pantalla-que-alguien-agrega-manana", {"vend": "PPR"})
    assert resp is not None
    assert resp[1] == 404


# --------------------------------------------------------------------------
# La caché de la columna `vend` — el SÍ es para siempre, el NO dura 60 s
# --------------------------------------------------------------------------


def test_columna_vend_no_cachea_el_negativo_para_siempre(monkeypatch):
    """Pasó de verdad el 2026-08-03: corrí la migración 0153 y la app siguió
    sin ver la columna, porque ya había preguntado ANTES de la migración y se
    había guardado el "no está" para toda la vida del proceso. Sin síntoma
    visible más que "no me deja guardar el vendedor".

    Misma familia que la caché del fracaso de Metabase (2026-07-29): un
    negativo no puede tener la misma vida que un positivo.
    """
    import auth

    auth._reset_cache_columna_vend()
    respuestas = [None, {"ok": 1}]
    llamadas = []

    def _fetch_one(sql, params=None, conn=None):
        llamadas.append(1)
        return respuestas.pop(0) if respuestas else {"ok": 1}

    monkeypatch.setattr(auth.db, "fetch_one", _fetch_one)

    # Antes de la migración: no está.
    assert auth._columna_vend_existe() is False
    # Dentro del TTL no vuelve a preguntar (no una query por request).
    assert auth._columna_vend_existe() is False
    assert len(llamadas) == 1

    # Pasa el TTL (corrieron la migración en el medio) → re-pregunta y la ve.
    monkeypatch.setattr(auth, "_COL_VEND_TTL_NEGATIVO", 0.0)
    assert auth._columna_vend_existe() is True
    assert len(llamadas) == 2

    # Y una vez que existe, no pregunta nunca más.
    assert auth._columna_vend_existe() is True
    assert len(llamadas) == 2
    auth._reset_cache_columna_vend()
