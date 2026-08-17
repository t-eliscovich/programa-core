"""El menú del nombre se queda sólo con Administración y Salir.

Pedido de la dueña 2026-08-17: *"no hace falta estas bases de datos dentro de
mi usuario"*, sobre la sección "BASES DE DATOS" del menú de arriba a la
derecha (Clientes / Proveedores / Lista de precios / Bancos / Contactos).

Cuatro de las cinco eran duplicado puro: ya estaban en el sidebar. La quinta
NO: `base.html` era el ÚNICO link a `/clientes/contactos` en toda la app, así
que sacar la sección a secas dejaba el directorio de contactos sin puerta —
una pantalla que existe, responde 200 y a la que no se puede llegar. Por eso
Contactos bajó al sidebar, al lado de Clientes.

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


@pytest.mark.parametrize("endpoint", PANTALLAS_DEL_MENU_VIEJO)
def test_el_menu_del_nombre_no_linkea_esas_pantallas(endpoint):
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


def test_contactos_bajo_al_sidebar_al_lado_de_clientes():
    """Es el directorio de teléfonos y mails de los CLIENTES: va con Clientes."""
    i = BASE.index("Clientes y proveedores")
    seccion = BASE[i : BASE.index("</details>", i)]
    assert "nav_link('clientes.contactos', 'Contactos'" in seccion
    assert seccion.index("nav_link('clientes.lista'") < seccion.index(
        "nav_link('clientes.contactos'"
    ), "Contactos va DESPUÉS de Clientes"


def test_contactos_respeta_el_permiso_de_clientes():
    """Mismo gate que tenía en el menú del nombre: no se abre de paso."""
    i = BASE.index("nav_link('clientes.contactos'")
    assert "tiene_permiso('clientes.ver')" in BASE[i - 120 : i]
