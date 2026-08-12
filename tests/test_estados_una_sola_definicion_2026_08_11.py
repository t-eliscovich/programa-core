"""Los estados del cheque se definen en UN lugar, y ese lugar imita al dBase.

Dueña, 11/08/2026: *"tiene que estar definida en un lugar. tiene que funcionar.
y tiene que imitar al dbase"* · *"la estructura y qué significa cada estado. no
agregar estados muertos"*.

`modules/cheques/estados.py` es esa tabla: letra, qué significa, y de qué lado
está. Las familias (`EN_CARTERA`, `EN_BANCO`, `EN_CAJA`), las etiquetas de los
menús y quién es "rebote real" se DERIVAN de ahí. Antes cada cosa se escribía
por separado, y se atrasaban entre ellas sin que nada avisara.

La estructura es la del dBase (`MODIFICA.PRG`): `CART='Z123PD'`,
`ENBANC='BVWIJK'`, más `'9XC'`. Lo que este archivo exige no es que copiemos al
dBase letra por letra —hay estados suyos que no usamos y estados nuestros que
él no tiene— sino que **toda diferencia esté declarada con su motivo**. Una
divergencia tiene que ser una decisión escrita, no un olvido.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from modules.cheques import estados, queries

RAIZ = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# La tabla contra el dBase
# ---------------------------------------------------------------------------

def _dbase_todos() -> set[str]:
    return set(estados.DBASE_CART) | set(estados.DBASE_ENBANC) | set(estados.DBASE_OTROS)


def test_lo_que_el_dbase_tiene_y_nosotros_no_esta_declarado():
    faltan = _dbase_todos() - set(estados.ESTADOS)
    sin_motivo = sorted(faltan - set(estados.NO_USADOS))
    assert not sin_motivo, (
        f"Estos estados del dBase desaparecieron sin decir por qué: {sin_motivo}. "
        "Si se sacan a propósito, van en NO_USADOS con el motivo."
    )


def test_lo_que_nosotros_tenemos_y_el_dbase_no_esta_declarado():
    nuestros = set(estados.ESTADOS) - _dbase_todos()
    sin_motivo = sorted(nuestros - set(estados.AGREGADOS))
    assert not sin_motivo, (
        f"Estos estados no existen en el dBase y nadie dijo por qué: {sin_motivo}."
    )


def test_la_cartera_es_exactamente_la_del_dbase():
    """CART es la familia que decide si el cheque sigue siendo deuda del cliente.

    Acá no hay licencia para divergir: si un estado se cae de cartera, el
    cliente deja de deber esa plata en el estado de cuenta.
    """
    assert set(estados.EN_CARTERA) == set(estados.DBASE_CART), (
        f"cartera = {sorted(estados.EN_CARTERA)} vs dBase {sorted(estados.DBASE_CART)}"
    )


def test_lo_que_esta_en_banco_es_un_subconjunto_del_dbase_mas_lo_declarado():
    permitido = set(estados.DBASE_ENBANC) | set(estados.AGREGADOS)
    sobra = set(estados.EN_BANCO) - permitido
    assert not sobra, f"Estados en banco que nadie declaró: {sorted(sobra)}"


def test_no_quedan_estados_muertos():
    """W, J y K: códigos de bancos viejos, 0 cheques en producción."""
    for muerto in ("W", "J", "K"):
        assert muerto not in estados.ESTADOS, f"{muerto} volvió a la tabla"
        assert muerto in estados.NO_USADOS, f"falta el motivo por el que {muerto} no está"


# ---------------------------------------------------------------------------
# Un lugar: nadie re-escribe las familias
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("archivo", [
    "modules/cheques/queries.py",
    "modules/cheques/views.py",
    "modules/comisiones/queries.py",
])
def test_nadie_vuelve_a_escribir_la_familia_de_depositados_a_mano(archivo):
    """La firma del bug: la tupla de depositados tipeada de nuevo.

    Estuvo escrita a mano en tres archivos con miembros distintos. Cuando se le
    agregó un estado a una, las otras siguieron con la lista vieja.
    """
    # Sin comentarios: la tupla citada dentro de una explicación no es código.
    txt = "\n".join(
        linea for linea in (RAIZ / archivo).read_text(encoding="utf8").splitlines()
        if not linea.lstrip().startswith("#")
    )
    # una tupla de letras sueltas que incluya B y V es la familia re-escrita
    sospechosas = [
        m.group(0) for m in re.finditer(r'\(\s*(?:"[A-Z0-9]"\s*,\s*){2,}"[A-Z0-9]"\s*,?\s*\)', txt)
        if '"B"' in m.group(0) and '"V"' in m.group(0)
    ]
    assert not sospechosas, (
        f"{archivo} re-escribe la familia de depositados: {sospechosas}. "
        "Tiene que importar EN_BANCO de modules/cheques/estados.py."
    )


def test_cada_estado_dice_que_significa():
    """La dueña pidió la estructura Y qué significa cada estado."""
    sin_texto = [
        e.letra for e in estados.ESTADOS.values()
        if not e.que_es or len(e.que_es) < 15 or not e.nombre
    ]
    assert not sin_texto, f"Estados sin explicación: {sin_texto}"


def test_las_familias_no_se_pisan():
    fams = [set(estados.EN_CARTERA), set(estados.EN_BANCO), set(estados.EN_CAJA)]
    for i, a in enumerate(fams):
        for b in fams[i + 1:]:
            assert not (a & b), f"estados en dos familias: {sorted(a & b)}"


def test_las_etiquetas_de_los_menus_salen_de_la_tabla():
    assert queries.LABEL_CORTO_ESTADO is estados.LABEL_CORTO_ESTADO
    for letra in estados.ESTADOS:
        assert letra in queries.LABEL_CORTO_ESTADO


def test_todo_estado_que_el_menu_ofrece_esta_en_la_tabla():
    ofrecidos = {
        t["stat_destino"]
        for ops in queries.transiciones_map().values()
        for t in ops
    }
    huerfanos = sorted(ofrecidos - set(estados.ESTADOS))
    assert not huerfanos, f"El menú ofrece estados que la tabla no conoce: {huerfanos}"
