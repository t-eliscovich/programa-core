"""La frase de químicos nombra al proveedor con el código de Programa Core.

TMT 2026-08-11: la traza mostraba dos renglones del MISMO hecho con dos
nombres distintos — "CP PO → DE 10149" y "se cargó una compra a PRO" — y
parecían dos compras distintas. PRO y PO son PROVITEX.
"""
from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.compras.formulas_bridge import PROV_MAP  # noqa: E402
from modules.informes.quimico_inv_formulas import (  # noqa: E402
    _frase_de_lo_cargado,
    _prov_pc,
)


def test_usa_el_diccionario_que_ya_existe():
    """No un mapa nuevo: el MISMO que usa el puente para crear la compra, así
    los dos lados no se pueden desincronizar."""
    assert _prov_pc("PRO") == PROV_MAP["PRO"] == "PO"
    assert _prov_pc("SEY") == "SY"
    assert _prov_pc("qsi") == "QI"


def test_un_proveedor_sin_mapear_sale_como_viene():
    """Mejor su código de formulas que un guión: se puede ir a buscarlo."""
    assert _prov_pc("ZZZ") == "ZZZ"
    assert _prov_pc("") == ""


def test_la_frase_nombra_al_proveedor_como_lo_llama_el_programa():
    frase = _frase_de_lo_cargado([{"k": "compra", "quien": "PRO", "n": 1,
                                   "q": 200}])
    assert frase == "se cargó una compra a PO (200 u.)"
    assert "PRO" not in frase


def test_dos_proveedores_se_nombran_los_dos_traducidos():
    frase = _frase_de_lo_cargado([{"k": "compra", "quien": "PRO", "n": 1, "q": 200},
                                  {"k": "compra", "quien": "SEY", "n": 1, "q": 50}])
    assert frase == "se cargaron 2 compras a PO y SY (250 u.)"
