"""Deshacer la anulación de un cheque tiene que devolver TAMBIÉN su espejo.

El 19/08 se arregló la ida: anular un anticipo se lleva su espejo NB=98, para
que la utilidad no se mueva de una sola punta. La VUELTA quedaba coja.

`deshacer_anulacion_error_carga` busca la anulación del espejo pidiendo
`estado='activo'`. Pero una anulación por error de carga nace 'activo' o
'reverso' según haya encontrado el `cheque_creado` del cheque para linkearlo
(`id_original=… if md_orig_cheque else None`). En producción, al 24/08, hay
**34 'activo' y 47 'reverso'**: con ese filtro, deshacer la anulación del
padre revivía el anticipo y dejaba el espejo muerto en más de la mitad de los
casos — el mismo desbalance del 19/08 con el signo al revés. Y el `continue`
de la rama lo hacía **en silencio**.

Lo que importa no es con qué estado nació el reverso, sino que la anulación
del espejo **no esté ya deshecha**.
"""
from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
FUENTE = (RAIZ / "modules" / "cheques" / "queries.py").read_text(encoding="utf-8")


def _consulta_del_espejo() -> str:
    """El SELECT con el que se busca la anulación del espejo."""
    i = FUENTE.index("espejos_revividos: list[int] = []")
    bloque = FUENTE[i:i + 2500]
    j = bloque.index("SELECT id_mov_doble")
    return bloque[j:bloque.index('"""', j)]


def test_no_se_pide_el_estado_con_el_que_nacio():
    sql = _consulta_del_espejo()
    assert "estado='activo'" not in sql.replace(" ", ""), (
        "una anulación por error de carga nace 'activo' o 'reverso' según "
        "haya encontrado el cheque_creado para linkear: pedir 'activo' se "
        "saltea la mitad de los espejos y los deja muertos, sin avisar"
    )


def test_se_pide_que_no_este_ya_deshecha():
    sql = _consulta_del_espejo().replace(" ", "")
    assert "estado<>'reversado'" in sql, (
        "la condición correcta es que la anulación del espejo no esté ya "
        "deshecha — con cualquier otra se pierden espejos"
    )


def test_la_anulacion_del_espejo_puede_nacer_de_las_dos_formas():
    """El origen del problema, fijado para que no se 'simplifique' después.

    `anular_por_error_carga` linkea el reverso al `cheque_creado` sólo si lo
    encuentra. Ese `if … else None` es lo que hace que el estado no sea
    predecible, y es a propósito.
    """
    i = FUENTE.index('tipo="reverso_cheque_administrativo"')
    bloque = FUENTE[i:i + 3000]
    m = re.search(r"id_original=(.+?),\n", bloque)
    assert m, "no encontré con qué se linkea la anulación por error de carga"
    assert "else None" in m.group(1), (
        "si el reverso pasara a linkearse SIEMPRE, este test sobra — pero "
        "revisá antes las consultas que lo buscan por estado"
    )


def test_el_espejo_que_no_vuelve_no_puede_pasar_callado():
    """El `continue` que se come el caso tiene que dejar rastro.

    Un espejo que no vuelve deja al cliente con un saldo a favor que no
    existe. Que eso pase sin una línea en el log es cómo se pierden ocho días
    hasta que alguien mira el número.
    """
    i = FUENTE.index("espejos_revividos: list[int] = []")
    bloque = FUENTE[i:i + 3000]
    j = bloque.index("if not _mv_esp:")
    rama = bloque[j:j + 900]
    assert "_LOG" in rama or "avisar" in rama, (
        "si no se encuentra la anulación del espejo se hace `continue` en "
        "silencio: dejá al menos un warning con el id del espejo"
    )


def test_el_modulo_compila():
    ast.parse(FUENTE)


def test_el_logger_existe_de_verdad():
    """El warning nuevo usa `_LOG`, que este módulo NO tenía definido.

    Un test que sólo lee el fuente da por bueno un `_LOG.warning(...)` que en
    ejecución sería un NameError — y encima adentro del camino de deshacer una
    anulación, o sea justo cuando alguien está arreglando algo.
    """
    from modules.cheques import queries as q
    assert isinstance(getattr(q, "_LOG", None), logging.Logger)


@pytest.mark.parametrize("otro", ["'activo'", '"activo"'])
def test_ninguna_busqueda_de_un_reverso_pide_activo(otro):
    """Barrido: un `tipo='reverso_*'` no se busca por `estado='activo'`.

    Es la forma exacta de los dos bugs del 24/08 (este y
    `_reactivar_factura_anulada`). Si aparece un tercero, que se caiga acá.
    """
    for m in re.finditer(r"tipo\s*=\s*'reverso_[a-z_]+'", FUENTE):
        ventana = FUENTE[m.start():m.start() + 400]
        corte = ventana.find('"""')
        ventana = ventana[:corte] if corte > 0 else ventana
        assert f"estado={otro}" not in ventana.replace(" ", "").replace(
            "estado=", "estado="), (
            f"cerca de {m.group(0)} se busca estado={otro}: un reverso puede "
            "nacer 'reverso' y esa consulta no lo va a encontrar nunca"
        )
