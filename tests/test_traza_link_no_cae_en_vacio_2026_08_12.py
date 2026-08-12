"""Un renglón de la traza nunca linkea a una pantalla vacía.

Dueña, 12/08/2026: clickea el renglón del banco y cae en
`/historial?tipo=banco_cargado&desde=…&hasta=…`, que muestra *"Sin movimientos
en este filtro"*.

`banco_cargado` es una etiqueta INTERNA de la traza, no un tipo de `mov_doble`.
El respaldo de `_url_del_grupo` (cuando el grupo no trae los `id_mov_doble`
exactos) filtra el historial POR TIPO, y el historial no conoce ese nombre.

El código ya tachaba `banco_varios` a mano por esta misma razón — y así se coló
`banco_cargado`. Ahora se le PREGUNTA al historial (`TIPOS_LABEL`) si conoce el
tipo antes de armar el link: enumerar los que fallan es lo que ya falló.
"""
from __future__ import annotations

import pytest

from modules.historial.queries import TIPOS_LABEL
from modules.informes.traza import _url_del_grupo

DIA = "2026-08-12"


def test_no_linkea_con_un_tipo_que_el_historial_no_conoce():
    for tipo in ("banco_cargado", "banco_varios", "inventado_ayer", ""):
        url = _url_del_grupo({}, {"tipo": tipo, "dia": DIA})
        assert url is None, f"{tipo!r} arma un link a una pantalla vacía: {url}"


def test_el_renglon_del_banco_linkea_por_su_documento():
    """El historial sí conoce el movimiento como `banco_<doc>_directo`."""
    url = _url_del_grupo(
        {}, {"tipo": "banco_cargado", "tipo_historial": "banco_de_directo", "dia": DIA}
    )
    assert url == f"/historial?tipo=banco_de_directo&desde={DIA}&hasta={DIA}"


def test_los_ids_exactos_ganan_siempre():
    """Con los `id_mov_doble` del grupo no hace falta filtrar por tipo."""
    url = _url_del_grupo({"hechos": [7, 3]}, {"tipo": "banco_cargado", "dia": DIA})
    assert url == "/historial?ids=3,7"


@pytest.mark.parametrize("tipo", sorted(TIPOS_LABEL)[:12])
def test_los_tipos_de_verdad_siguen_linkeando(tipo):
    assert _url_del_grupo({}, {"tipo": tipo, "dia": DIA}) is not None


def test_el_evento_de_banco_trae_su_tipo_para_el_historial():
    """`eventos.transacciones` tiene que poner `tipo_historial`.

    Sin él, el renglón del banco se queda sin link (que es mejor que uno roto,
    pero peor que el bueno).
    """
    import inspect

    from modules.informes import eventos

    src = inspect.getsource(eventos)
    assert "tipo_historial" in src
    assert 'f"banco_{_doc_banco}_directo"' in src
