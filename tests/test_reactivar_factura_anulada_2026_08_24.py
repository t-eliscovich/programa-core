"""La factura que vuelve de X a Z tiene que volver a contar como EMITIDA.

TMT 2026-08-24 (caso 182254 KJG / 182327 VGA). El vigía de anuladas las dio
de baja por un hueco momentáneo de Asinfo (las dos siguen vivas en el ERP,
`fc.estado = 4`). Al devolverlas a cartera con el desplegable de estado, la
factura volvió — pero su `factura_emitida` quedó en 'reversado': el
Historial y la traza la seguían mostrando como una emisión dada de baja.

La causa: `_reactivar_factura_anulada` buscaba el reverso con
`estado='activo'`, y en `mov_doble.reversar` un reverso nace SIEMPRE con
`estado='reverso'`. La condición no podía cumplirse ni una sola vez desde
que se escribió (21/07). Cuatro facturas quedaron a medio camino, una de
$41.024,53.

Este test ata las dos puntas: el estado que `_reactivar_factura_anulada`
BUSCA sale de leer el que `mov_doble` ESCRIBE. Si mañana uno de los dos
cambia y el otro no, el test se cae.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parents[1]


def _estado_de_un_reverso() -> str:
    """El literal que `mov_doble.reversar` le pone a un reverso.

    Se lee por AST (no por texto): el candado que busca la palabra suelta se
    escapa el día que alguien renombra la variable.
    """
    arbol = ast.parse((_RAIZ / "mov_doble.py").read_text(encoding="utf-8"))
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.Assign):
            continue
        destinos = [t.id for t in nodo.targets if isinstance(t, ast.Name)]
        if "estado" not in destinos:
            continue
        if isinstance(nodo.value, ast.IfExp):
            # estado = "reverso" if id_original else "activo"
            cuerpo = nodo.value.body
            if isinstance(cuerpo, ast.Constant) and isinstance(cuerpo.value, str):
                return cuerpo.value
    pytest.fail(
        "No encontré en mov_doble.py el `estado = ... if id_original else ...` "
        "que decide con qué estado nace un reverso. Si se movió, actualizá "
        "este test — pero NO lo borres: es el que ata la búsqueda del "
        "des-reverso al valor real."
    )


class _DBFalsa:
    """Mínimo `db` para correr la función sin Postgres.

    `fetch_one` sólo devuelve el reverso si la consulta lo busca con el
    estado con el que un reverso REALMENTE nace. Con el `estado='activo'`
    viejo devuelve None y no se actualiza nada — que es exactamente el bug.
    """

    def __init__(self, estado_real: str) -> None:
        self.estado_real = estado_real
        self.updates: list[tuple[str, tuple]] = []

    def fetch_one(self, sql, params=None, conn=None):
        if f"estado='{self.estado_real}'" not in sql.replace('"', "'"):
            return None
        return {"id_mov_doble": 27160, "id_original": 26722}

    def execute(self, sql, params=None, conn=None):
        self.updates.append((" ".join(sql.split()), params))
        return 1


def test_la_emision_vuelve_a_activo(monkeypatch):
    from modules.informes import queries

    falsa = _DBFalsa(_estado_de_un_reverso())
    monkeypatch.setattr(queries, "db", falsa)
    queries._reactivar_factura_anulada(282170, conn=object())

    assert falsa.updates, (
        "La factura volvió a cartera pero su emisión sigue reversada: "
        "_reactivar_factura_anulada no encontró el reverso. Revisá con qué "
        "`estado` lo busca — un reverso nace con "
        f"'{_estado_de_un_reverso()}', no con 'activo'."
    )
    sqls = [s for s, _ in falsa.updates]
    assert any("estado='reversado'" in s for s in sqls), (
        "Falta tachar el 'reverso_factura_anulada' que se está deshaciendo."
    )
    assert any("estado='activo'" in s and "id_reverso=NULL" in s for s in sqls), (
        "Falta devolver la 'factura_emitida' original a 'activo' (y soltarle "
        "el id_reverso), que es lo que la hace contar de nuevo."
    )
    # El original que se reactiva es el que el reverso apunta, no otro.
    assert any(p == (26722,) for _, p in falsa.updates)
