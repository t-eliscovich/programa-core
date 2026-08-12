"""Ningún link de la app manda a una pantalla con un filtro que no existe.

Un `href` que apunta a una ruta inexistente da 404 y se ve — eso ya lo cubre
`test_drift_estatico.test_los_links_literales_apuntan_a_pantallas_que_existen`.

Lo que NO se veía es el otro modo de romperse: el link RESUELVE, la pantalla
abre, y muestra *"Sin movimientos en este filtro"* porque el valor del filtro
no existe en el vocabulario de esa pantalla. Es peor que un 404: parece que no
hay datos.

Dueña, 12/08/2026, clickeando el renglón del banco en la traza:
`/historial?tipo=banco_cargado&…` → vacío. `banco_cargado` es una etiqueta
interna de la traza, no un tipo de `mov_doble`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from modules.historial.queries import TIPOS_LABEL
from modules.informes import eventos
from modules.informes.traza import _url_del_grupo

RAIZ = Path(__file__).resolve().parent.parent
DIA = "2026-08-12"


def _fuentes():
    for patron in ("modules/**/*.py", "modules/**/templates/**/*.html", "templates/**/*.html"):
        yield from RAIZ.glob(patron)


def test_ningun_link_literal_filtra_el_historial_por_un_tipo_inventado():
    rotos = []
    for p in _fuentes():
        txt = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"/historial\?[^\"'\s]*?tipo=([A-Za-z_0-9]+)", txt):
            if m.group(1) not in TIPOS_LABEL:
                rotos.append(f"{p.relative_to(RAIZ)}: tipo={m.group(1)}")
    assert not rotos, (
        "Estos links abren el historial con un filtro que no existe — la "
        f"pantalla va a decir 'Sin movimientos': {rotos}"
    )


@pytest.mark.parametrize("tipo", ["banco_cargado", "banco_varios", "lo_que_sea"])
def test_la_traza_no_arma_un_link_con_un_tipo_que_el_historial_no_conoce(tipo):
    assert _url_del_grupo({}, {"tipo": tipo, "dia": DIA}) is None


def test_todos_los_tipos_que_inventa_la_traza_estan_contemplados():
    """⭐ El invariante, sin enumerar.

    `eventos.py` fabrica etiquetas propias para agrupar renglones. Cada una es
    un candidato a link roto. Para TODAS: o el historial la conoce, o el
    renglón no linkea por tipo. Si mañana aparece una etiqueta nueva y alguien
    la deja linkeando, este test la nombra.
    """
    fuente = Path(eventos.__file__).read_text(encoding="utf-8")
    propios = set(re.findall(r'"tipo":\s*"([a-z_]+)"', fuente))
    assert propios, "no se encontró ninguna etiqueta propia — ¿cambió el patrón?"
    culpables = []
    for tipo in sorted(propios):
        if tipo in TIPOS_LABEL:
            continue  # el historial la conoce: puede linkear tranquila
        if _url_del_grupo({}, {"tipo": tipo, "dia": DIA}) is not None:
            culpables.append(tipo)
    assert not culpables, (
        f"Etiquetas internas de la traza que arman un link a la nada: {culpables}"
    )


def test_el_renglon_del_banco_manda_el_tipo_que_el_historial_SI_conoce():
    """Sin esto el renglón queda sin link: correcto, pero peor que el bueno."""
    url = _url_del_grupo(
        {}, {"tipo": "banco_cargado", "tipo_historial": "banco_de_directo", "dia": DIA}
    )
    assert url == f"/historial?tipo=banco_de_directo&desde={DIA}&hasta={DIA}"
    # y el nombre que arma eventos.py tiene que existir de verdad
    for doc in ("de", "ch", "tr", "nd", "nc", "ac"):
        assert f"banco_{doc}_directo" in TIPOS_LABEL, (
            f"eventos.py arma 'banco_{doc}_directo' y el historial no lo conoce"
        )
