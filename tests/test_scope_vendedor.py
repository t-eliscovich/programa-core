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

import scope_vendedor
from scope_vendedor import (
    HOME_VENDEDOR,
    PREFIJOS_HOME,
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
        # ⭐ La Competencia se les abrió el 25/08/2026: las tres pantallas que
        # cuelgan de ese prefijo, y NADA más de /analisis.
        ("/analisis/competencia", True),
        ("/analisis/competencia/telas", True),
        ("/analisis/competencia/mi-hoja", True),
        ("/analisis/competencia/mi-hoja.csv", True),
        # ⚠ Éstas tienen los clientes de TODOS y los puntos editables: si
        # alguna vez dan True, es un bug y no una mejora.
        ("/analisis/parado", False),
        ("/analisis/parado/clientes", False),
        ("/analisis/metas", False),
        ("/analisis", False),
        ("/analisis/competenciax", False),
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


def test_se_puede_volver_de_ver_como(app):
    """Sin esto, quien impersona a un vendedor queda encerrado en el portal.

    El banner "Volver a mi cuenta" postea a /stop-impersonate; si el allowlist
    lo 404ea, la única salida es borrar la cookie de sesión. Un candado no
    puede cerrar la puerta por la que se entró.
    """
    assert _con_user(app, "/stop-impersonate", {"vend": "PPR"}) is None


def test_pero_no_puede_impersonar_a_otro(app):
    """La puerta de SALIDA se abre; la de ENTRADA no."""
    resp = _con_user(app, "/impersonate/3", {"vend": "PPR"})
    assert resp is not None and resp[1] == 404


# --------------------------------------------------------------------------
# Las pantallas de ENTRADA se redirigen, no se 404ean
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/tablero", "/tablero/", "/operaciones"])
def test_las_entradas_llevan_al_portal_en_vez_de_404(app, path):
    """El bug que se comió el primer login de verdad (2026-08-03).

    `auth.login` manda a `dashboard.index` = `/tablero/`, que rebota a
    `/operaciones`. Ninguna estaba en el allowlist: el vendedor ponía su
    usuario y contraseña y **lo primero que veía del sistema era un 404**.
    Lo mismo con "Ver como" y con "Volver al inicio" de las páginas de error.
    """
    resp = _con_user(app, path, {"vend": "PPR"})
    assert not isinstance(resp, tuple), f"{path} 404ea en vez de redirigir"
    assert resp.status_code in (301, 302, 303)
    assert resp.headers["Location"].endswith(HOME_VENDEDOR)


def test_las_entradas_no_abren_lo_que_cuelga_de_ellas(app):
    """`/operaciones` redirige; `/operaciones-secretas` no existe para él."""
    resp = _con_user(app, "/operaciones-secretas", {"vend": "PPR"})
    assert isinstance(resp, tuple) and resp[1] == 404


def test_un_usuario_normal_no_ve_ningun_redirect_nuevo(app):
    """La invariante de siempre: para quien no es vendedor, no-op exacto."""
    for path in PREFIJOS_HOME:
        assert _con_user(app, path, {"username": "tamara", "vend": None}) is None


def test_las_pantallas_de_la_oficina_que_el_vendedor_tiene_con_otra_ruta_redirigen():
    """Dueña 26/08/2026, mirando la sección como Patricio: *"¿por qué no puedo
    ver saldos como patricio, o a quién ofrecerle qué?"*. Sí puede: son las
    MISMAS pantallas con sus clientes adentro, colgadas de
    /analisis/competencia. Lo que no funcionaba era llegar por la URL de la
    oficina —un bookmark, un link copiado, o ella previsualizando con «Ver
    como»—: el allowlist contestaba un 404 seco.

    ⚠ Se redirige SÓLO donde la pantalla existe del otro lado. Para el resto el
    404 se queda: el vendedor no tiene por qué enterarse de qué hay."""
    assert scope_vendedor.EQUIVALENTE_VENDEDOR == {
        "/analisis": "/analisis/competencia",
        "/analisis/parado": "/analisis/competencia/telas",
        "/analisis/parado/clientes": "/analisis/competencia/mi-hoja",
    }
    # y los tres destinos cuelgan del prefijo que el vendedor sí puede tocar
    for destino in scope_vendedor.EQUIVALENTE_VENDEDOR.values():
        assert scope_vendedor._path_permitido(
            destino, scope_vendedor.PREFIJOS_PERMITIDOS), (
            f"{destino} redirige a una ruta que el vendedor tampoco puede ver")


def test_lo_que_el_vendedor_no_tiene_sigue_dando_404():
    """El redirect es una excepción corta, no una puerta: una pantalla de la
    oficina sin equivalente no puede empezar a contestar algo distinto de 404."""
    for path in ("/facturas", "/cheques", "/informes/balance",
                 "/analisis/parado.csv", "/analisis/competencia/telas.csv"):
        assert path not in scope_vendedor.EQUIVALENTE_VENDEDOR
