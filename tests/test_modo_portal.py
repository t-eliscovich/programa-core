"""El proceso del PORTAL levanta SÓLO el portal: el ERP no existe ahí.

TMT 2026-08-24, `PLAN_PORTAL_CLIENTE_2026_08_24.md`. El portal del cliente va a
estar abierto a internet y el ERP no. De las tres formas de hacerlo —adentro
con un candado, una app aparte, o el mismo código en un proceso propio— se
eligió la tercera, y esta es la razón:

⭐ **Un candado hay que escribirlo bien. Que la ruta no exista no hay que
escribirlo.**

Por eso estos tests van ANTES que cualquier pantalla del portal: son el corazón
de la seguridad de todo el asunto. El que caza los errores futuros es
`test_en_modo_portal_no_queda_ni_una_ruta_del_erp`, que recorre el `url_map`
entero en vez de probar una lista de rutas escritas a mano — una lista a mano
sólo prueba lo que a alguien se le ocurrió el día que la escribió.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import modo  # noqa: E402
from tests.test_routes_smoke import build_app  # noqa: E402

#: Lo único que puede existir en el proceso del portal. Todo lo demás que
#: aparezca en el `url_map` es una pantalla del ERP que se coló.
PREFIJOS_PERMITIDOS = ("portal.", "static", "auth.", "auth_google.")

#: `healthz` (/_healthz) es la excepción, y a propósito: es el chequeo que
#: mira el monitoreo para saber si el proceso está vivo. No pide login
#: porque no muestra ni un dato — contesta 200 o 503 y nada más.
ENDPOINTS_PERMITIDOS = {"healthz"}


def _app_en_modo(valor):
    entorno = dict(os.environ)
    if valor is None:
        entorno.pop("MODO", None)
    else:
        entorno["MODO"] = valor
    with patch.dict(os.environ, entorno, clear=True):
        return build_app()


def _endpoints(app):
    return {r.endpoint for r in app.url_map.iter_rules()}


# ---------------------------------------------------------------------------
# La variable
# ---------------------------------------------------------------------------


def test_solo_el_valor_exacto_prende_el_portal():
    """Fail-safe hacia el lado seguro: si alguien escribe mal la variable, el
    que arranca es el programa de la oficina, que ya está detrás de login y
    permisos. Al revés sería peor."""
    for valor, esperado in (("portal", True), ("PORTAL", True), (" portal ", True),
                            ("portal-cliente", False), ("Portal del cliente", False),
                            ("1", False), ("", False)):
        with patch.dict(os.environ, {"MODO": valor}):
            assert modo.es_portal() is esperado, valor


def test_sin_la_variable_es_el_programa_de_siempre():
    entorno = {k: v for k, v in os.environ.items() if k != "MODO"}
    with patch.dict(os.environ, entorno, clear=True):
        assert modo.es_portal() is False
        assert modo.nombre() == "Programa Core"


# ---------------------------------------------------------------------------
# Modo normal: no se rompió nada
# ---------------------------------------------------------------------------


def test_el_modo_normal_registra_todo_lo_de_siempre():
    """Los ~80 blueprints se MOVIERON de `create_app` a `registro_erp`, sin
    tocar una línea. Si el movimiento hubiera perdido alguno, se ve acá."""
    app, deshacer = _app_en_modo(None)
    try:
        eps = _endpoints(app)
        assert len(eps) > 400, f"faltan pantallas: quedaron {len(eps)}"
        for esperado in ("informes.balance", "cheques.lista", "clientes.lista",
                         "posdat.lista", "compras.lista", "sql_console.consola",
                         "index"):
            assert esperado in eps, f"se perdió {esperado} al mover los blueprints"
    finally:
        deshacer()


# ---------------------------------------------------------------------------
# Modo portal: el ERP no existe
# ---------------------------------------------------------------------------


def test_en_modo_portal_no_queda_ni_una_ruta_del_erp():
    """⭐ El test que importa. Recorre el `url_map` ENTERO en vez de probar una
    lista escrita a mano: así caza también la pantalla que alguien agregue el
    año que viene sin acordarse de este archivo."""
    app, deshacer = _app_en_modo("portal")
    try:
        colados = sorted(
            ep for ep in _endpoints(app)
            if not ep.startswith(PREFIJOS_PERMITIDOS)
            and ep not in ENDPOINTS_PERMITIDOS
        )
        assert colados == [], (
            "en el proceso del portal quedaron pantallas que no son del "
            f"portal: {colados}")
    finally:
        deshacer()


def test_las_pantallas_de_la_plata_dan_404_en_el_portal():
    """La otra mitad: no alcanza con que el endpoint no esté en el mapa, el
    pedido de verdad tiene que morir. Estas son las que mueven plata."""
    app, deshacer = _app_en_modo("portal")
    try:
        c = app.test_client()
        for url in ("/informes/balance", "/posdat", "/cheques", "/clientes",
                    "/compras", "/admin/sql", "/informes/flujo",
                    "/conciliacion/banco", "/admin/health/all", "/mi-cartera"):
            r = c.get(url)
            assert r.status_code == 404, f"{url} contestó {r.status_code} en el portal"
    finally:
        deshacer()


def test_el_portal_si_contesta_en_su_puerta():
    app, deshacer = _app_en_modo("portal")
    try:
        r = app.test_client().get("/")
        assert r.status_code == 200
        assert r.get_json()["programa"] == "Portal Intela"
    finally:
        deshacer()


def test_el_programa_de_la_oficina_no_levanta_el_portal():
    """Al revés también: la puerta del portal no aparece en el ERP, donde `/`
    es el tablero."""
    app, deshacer = _app_en_modo(None)
    try:
        assert "portal.inicio" not in _endpoints(app)
    finally:
        deshacer()
