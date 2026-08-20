"""Confirmar un TOTALIZAR pide permiso propio, no `clientes.ver`.

TMT 2026-08-20. La ruta atiende GET (pantalla de confirmación) y POST
(ejecuta) con el mismo gate. `clientes.ver` es de LECTURA: lo tienen ocho
roles, entre ellos Lectura y Ventas. El POST redistribuye todos los abonos del
cliente y borra sus vínculos cheque↔factura.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

PERMISO = "estado_cuenta.totalizar"


def _permisos_de(rol: str) -> set[str]:
    from config.roles import ROLES
    for nombre, permisos in ROLES:
        if nombre == rol:
            return set(permisos)
    raise AssertionError(f"rol {rol} no existe en config/roles.py")


@pytest.mark.parametrize("rol", ["Gerente", "Contabilidad", "Cobranzas", "INT"])
def test_lo_tienen_los_que_totalizan(rol):
    assert PERMISO in _permisos_de(rol)


@pytest.mark.parametrize("rol", ["Lectura", "Ventas"])
def test_no_lo_tienen_los_de_lectura(rol):
    """El punto del cambio: ver la cuenta no es poder reescribirla."""
    permisos = _permisos_de(rol)
    assert "clientes.ver" in permisos, "cambió el supuesto del test"
    assert PERMISO not in permisos


def test_alex_y_andres_siguen_pudiendo():
    """Los dos únicos que totalizaron alguna vez (medido en mov_doble)."""
    from config.roles import ROLES
    wildcards = {n for n, p in ROLES if "*" in p}
    assert "Accionista" in wildcards        # andres
    assert PERMISO in _permisos_de("INT")   # alex


def _views_src() -> str:
    ruta = os.path.join(_REPO_ROOT, "modules", "informes", "views.py")
    with open(ruta, encoding="utf-8") as fh:
        return fh.read()


def test_el_deshacer_va_por_el_mismo_permiso():
    """Deshacer un totalizar mueve la misma plata que hacerlo."""
    src = _views_src()
    i = src.index("def totalizar_reverso_deshacer")
    cabecera = src[max(0, i - 400):i]
    assert f'@requiere_permiso("{PERMISO}")' in cabecera
    assert 'methods=["POST"]' in cabecera      # POST-only: no hay GET que ver
    assert '@requiere_permiso("clientes.ver")' not in cabecera


def test_el_post_de_totalizar_chequea_el_permiso():
    ruta = os.path.join(_REPO_ROOT, "modules", "informes", "views.py")
    with open(ruta, encoding="utf-8") as fh:
        src = fh.read()
    i = src.index("def estado_cuenta_totalizar")
    j = src.index("def ", src.index('if request.method == "POST":', i))
    cuerpo = src[i:j]
    assert f'tiene_permiso("{PERMISO}")' in cuerpo
    # y el chequeo tiene que estar ANTES de ejecutar
    assert (cuerpo.index(f'tiene_permiso("{PERMISO}")')
            < cuerpo.index("totalizar_estado_cuenta_ejecutar"))


def test_la_migracion_existe_y_nombra_a_los_mismos_roles():
    ruta = os.path.join(_REPO_ROOT, "migrations",
                        "0208_permiso_totalizar_estado_cuenta.py")
    assert os.path.exists(ruta), "sin migración el permiso no existe en producción"
    with open(ruta, encoding="utf-8") as fh:
        src = fh.read()
    assert f'PERMISO = "{PERMISO}"' in src
    for rol in ("Gerente", "Contabilidad", "Cobranzas", "INT"):
        assert f'"{rol}"' in src


def test_el_boton_no_se_dibuja_sin_permiso():
    ruta = os.path.join(_REPO_ROOT, "modules", "informes", "templates",
                        "informes", "totalizar_preview.html")
    with open(ruta, encoding="utf-8") as fh:
        html = fh.read()
    assert f"tiene_permiso('{PERMISO}')" in html
    i = html.index(f"tiene_permiso('{PERMISO}')")
    assert html.index("Confirmar totalización") > i
