"""El buscador de la pantalla "Deshacer conciliados" (2026-08-27).

TMT 2026-08-27 (Tamara): habia que desconciliar el cheque 246 CG3 (match del
19/08) y la pantalla no lo mostraba — lista los ultimos 500 matches y lo
viejo queda afuera del corte. El buscador filtra EN LA BASE, antes del
LIMIT, asi que lo conciliado viejo se alcanza sin agrandar la lista.

Reglas que estos tests cuidan:
- Sin texto no cambia nada (fragmento vacio, cero params).
- El filtro va por GRUPO entero (confirm_batch_id): si UN item pega, el
  grupo aparece COMPLETO — "Deshacer grupo" libera todos sus items y
  mostrarlo a medias mentiria.
- La vista usa el helper y el template manda el form por GET.
"""
from __future__ import annotations

import inspect
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.conciliacion.banco_v2_view import (  # noqa: E402
    _deshacer_filtro_buscar,
    banco_deshacer_v2,
)

_TPL = os.path.join(
    _REPO_ROOT, "modules", "conciliacion", "templates", "conciliacion",
    "banco_v2_deshacer.html",
)


def test_sin_texto_no_filtra():
    frag, params = _deshacer_filtro_buscar("")
    assert frag == "" and params == []
    frag, params = _deshacer_filtro_buscar("   ")
    assert frag == "" and params == []
    frag, params = _deshacer_filtro_buscar(None)
    assert frag == "" and params == []


def test_con_texto_arma_ilike_y_params():
    frag, params = _deshacer_filtro_buscar("CG3")
    assert params == ["%CG3%"] * 10
    # params y placeholders tienen que ir a la par
    assert frag.count("%s") == len(params)
    # busca en los dos lados: banco (real_*) y programa (t2.*)
    assert "real_concepto" in frag and "real_documento" in frag
    assert "t2.concepto" in frag and "t2.prov" in frag
    # y por nombre de cliente
    assert "c2.nombre ILIKE" in frag


def test_filtra_por_grupo_entero():
    """Si un item del grupo pega, entra el confirm_batch_id completo."""
    frag, _ = _deshacer_filtro_buscar("x")
    assert "m.confirm_batch_id IN (" in frag
    # y los sueltos (sin batch) por su propio id
    assert "m.confirm_batch_id IS NULL" in frag


def test_la_vista_usa_el_filtro_antes_del_limit():
    src = inspect.getsource(banco_deshacer_v2)
    assert "_deshacer_filtro_buscar" in src
    # el fragmento se inyecta antes del ORDER BY ... LIMIT
    i_frag = src.index("_frag_buscar + ")
    i_limit = src.index("LIMIT 500")
    assert i_frag < i_limit
    # y la vista le pasa el texto al template para que el input lo conserve
    assert "buscar=buscar" in src


def test_el_template_manda_el_form_por_get():
    with open(_TPL, encoding="utf-8") as fh:
        tpl = fh.read()
    assert 'name="buscar"' in tpl
    assert 'method="GET"' in tpl
    # el input conserva lo buscado
    assert "{{ buscar or '' }}" in tpl
