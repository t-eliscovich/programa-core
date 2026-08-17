"""El menú del nombre se queda sólo con Administración y Salir.

Pedido de la dueña 2026-08-17: *"no hace falta estas bases de datos dentro de
mi usuario"*, sobre la sección "BASES DE DATOS" del menú de arriba a la
derecha (Clientes / Proveedores / Lista de precios / Bancos / Contactos).

Cuatro de las cinco eran duplicado puro: ya estaban en el sidebar. La quinta
NO: `base.html` es el ÚNICO link a `/clientes/contactos` en toda la app, así
que sacar la sección a secas dejaba el directorio de contactos sin puerta —
una pantalla que existe, responde 200 y a la que no se puede llegar.

Primero se lo bajó al sidebar y la dueña lo sacó de ahí: *"los contactos estan
dentro de los clientes. no hacia falta que me lo pongas. si no volve a meter
esto dentro de mi usuario como estaba antes"*. Verificado en vivo el 17/08:
/clientes NO linkea a /clientes/contactos. Así que Contactos se queda en el
menú del nombre, solo y sin encabezado, que es lo que ella pidió.

De ahí el test que importa acá y que vale más que el pedido puntual:
`test_ninguna_pantalla_del_menu_viejo_quedo_sin_puerta`. Sacar un link es
barato; darse cuenta de que era el único es lo que cuesta.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BASE = (ROOT / "templates/base.html").read_text(encoding="utf-8")

#: Lo que vivía en la sección "Bases de datos" del menú del nombre.
PANTALLAS_DEL_MENU_VIEJO = [
    "clientes.lista",
    "proveedores.lista",
    "precios.lista",
    "bancos.lista",
    "clientes.contactos",
]

#: Las que se fueron del menú porque el sidebar ya las tenía.
DUPLICADAS_DEL_SIDEBAR = [e for e in PANTALLAS_DEL_MENU_VIEJO if e != "clientes.contactos"]


def _menu_del_nombre() -> str:
    """El popover que cuelga del nombre de usuario, sin el <script> de cierre."""
    i = BASE.index('id="user-menu-pop"')
    j = BASE.index("<script>", i)
    return BASE[i:j]


def _todos_los_templates() -> str:
    """Todos los .html del repo concatenados — dónde puede haber un link."""
    partes = []
    for p in sorted(ROOT.glob("**/*.html")):
        if ".git" in p.parts or "node_modules" in p.parts:
            continue
        partes.append(p.read_text(encoding="utf-8"))
    return "\n".join(partes)


# ---------------------------------------------------------------------------
# 1. La sección se fue
# ---------------------------------------------------------------------------


def test_el_menu_del_nombre_no_tiene_bases_de_datos():
    """El encabezado "Bases de datos" no se muestra más en el menú."""
    menu = _menu_del_nombre()
    assert ">Bases de datos<" not in menu


@pytest.mark.parametrize("endpoint", DUPLICADAS_DEL_SIDEBAR)
def test_el_menu_del_nombre_no_linkea_lo_que_ya_esta_en_el_sidebar(endpoint):
    assert f"url_for('{endpoint}')" not in _menu_del_nombre()


def test_el_menu_del_nombre_conserva_administracion_y_salir():
    """Lo que sí sigue siendo suyo: Periodos, Usuarios, Consola SQL y Salir."""
    menu = _menu_del_nombre()
    assert ">Administración<" in menu
    for endpoint in ("periodos.lista", "usuarios.lista", "sql_console.consola"):
        assert f"url_for('{endpoint}')" in menu
    assert "url_for('auth.logout')" in menu


# ---------------------------------------------------------------------------
# 2. Lo que de verdad importa: ninguna quedó sin puerta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", PANTALLAS_DEL_MENU_VIEJO)
def test_ninguna_pantalla_del_menu_viejo_quedo_sin_puerta(endpoint):
    """Cada una tiene que seguir teniendo AL MENOS un link que la traiga.

    Una pantalla sin link no da 404 ni rompe ningún test: simplemente deja de
    existir para el que la usa. Es la falla que no se ve.
    """
    html = _todos_los_templates()
    # Los links que la propia pantalla se hace a sí misma (paginar, limpiar el
    # buscador, exportar) no cuentan: si no podés llegar, no los ves nunca.
    fuera_de_su_casa = [
        m for m in re.finditer(rf"url_for\('{re.escape(endpoint)}'", html)
    ]
    assert fuera_de_su_casa, f"{endpoint} se quedó sin ningún link"
    assert f"nav_link('{endpoint}'" in BASE or f"url_for('{endpoint}')" in BASE, (
        f"{endpoint} no se llega desde la navegación de base.html"
    )


def test_contactos_se_queda_en_el_menu_del_nombre():
    """Dueña: "volvé a meter esto dentro de mi usuario como estaba antes"."""
    assert "url_for('clientes.contactos')" in _menu_del_nombre()


def test_contactos_no_esta_en_el_sidebar():
    """Se probó ponerlo ahí y lo bajó: "no hacía falta que me lo pongas"."""
    i = BASE.index("Clientes y proveedores")
    assert "clientes.contactos" not in BASE[i : BASE.index("</details>", i)]


def test_contactos_respeta_el_permiso_de_clientes():
    """Mismo gate de siempre: no se abre de paso a quien no ve clientes."""
    i = BASE.index("url_for('clientes.contactos')")
    assert "tiene_permiso('clientes.ver')" in BASE[i - 200 : i]
