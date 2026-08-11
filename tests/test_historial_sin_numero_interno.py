"""El historial no muestra el `id_mov_doble` (TMT 2026-08-11).

Dueña, señalando el "#23046" debajo de cada fecha: *"este numerito no hace
falta mostrar"*. Es el número interno de la fila y no se usa para nada de
afuera — los links entran por `?ids=`, el reverso tiene su propia URL y la
búsqueda es por concepto. Queda en el `title` por si hace falta para soporte.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_LISTA = Path("modules/historial/templates/historial/lista.html")


def test_el_numero_interno_no_se_pinta():
    """Se busca `#{{ r.id_mov_doble }}` FUERA de un atributo: dentro de un
    `title=` es un tooltip, y ése se conserva a propósito."""
    html = _LISTA.read_text()
    visibles = [ln for ln in html.splitlines()
                if "#{{ r.id_mov_doble }}" in ln
                and not re.search(r'title="#\{\{ r\.id_mov_doble \}\}"', ln)]
    assert visibles == [], visibles


def test_el_tooltip_sigue_estando_para_soporte():
    html = _LISTA.read_text()
    assert html.count('title="#{{ r.id_mov_doble }}"') == 2


def test_la_fila_hija_conserva_la_flechita():
    """Sin el número la celda quedaría vacía y se perdería que es hija."""
    assert '<div class="text-xs text-violet-700">↳</div>' in _LISTA.read_text()
