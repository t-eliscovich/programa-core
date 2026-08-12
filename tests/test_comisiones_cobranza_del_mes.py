"""La cobranza que entra en la comisión — los dos bugs de julio 2026.

TMT 2026-08-03, dueña: "Las comisiones están siendo mal calculadas. Dbase
las tuvo diferentes para julio." Eran DOS cosas distintas:

  1. El estado 'C' (cheque cobrado en caja: NB=99 "EFT." → entrada a CAJA
     "CH."+CLIENTE, ALTAS.PRG PROCEDURE PASOCAJA L870-893). El dBase lo
     cuenta (`STAT $ "BWVCIK"`, MODIFICA.PRG L1830); PC lo tiraba. Julio:
     $109.854,13 de cobranza real fuera de la comisión.

  2. `scintela.cliente` tiene 25 códigos DUPLICADOS (dos empresas con el
     mismo código de 3 letras). El `JOIN scintela.cliente` crudo devolvía
     2 filas por cheque y contaba la cobranza DOS VECES. Julio: +$4.341,86
     de más para PPR. El dBase no sufre esto porque `CLIENTE $ GRUPO` es
     un test de substring y matchea una sola vez.

Los tests corren sin Postgres: capturan el SQL que cada query genera y lo
inspeccionan. Lo que se protege es la FORMA de la query, que es donde
vivían los dos bugs.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.cheques import queries as chq  # noqa: E402
from modules.comisiones import queries as q  # noqa: E402


@pytest.fixture()
def sql_de(monkeypatch):
    """Devuelve una función `sql_de(callable)` → el SQL que ejecutó."""

    def _run(fn, *args, **kwargs):
        capturado = {}

        def fake_fetch_all(sql, params=None, **kw):
            capturado["sql"] = sql
            return []

        monkeypatch.setattr(q.db, "fetch_all", fake_fetch_all)
        fn(*args, **kwargs)
        return capturado["sql"]

    return _run


# ── Bug 1: el estado 'C' ────────────────────────────────────────────────

def test_stats_cobrado_incluye_C():
    """'C' = cobrado en caja. Es cobranza real: la plata entró."""
    assert "C" in q.STATS_COBRADO


def test_stats_cobrado_cubre_todo_lo_que_el_dbase_considera_cobrado():
    """Paridad MODIFICA.PRG: STAT $ "BWVCIK" — menos los estados muertos.

    TMT 2026-08-11: `W` y `K` son códigos de bancos viejos del DBF con CERO
    cheques en producción (censo sobre 3.753). La dueña decidió sacarlos, así
    que la paridad se mide contra el dBase descontando lo que está declarado
    como no usado en `modules/cheques/estados.py` — si alguien los resucita sin
    declararlo, este test cae igual.
    """
    from modules.cheques import estados

    del_dbase = set("BWVCIK")
    esperado = del_dbase - set(estados.NO_USADOS)
    assert esperado <= set(q.STATS_COBRADO), (
        f"falta {sorted(esperado - set(q.STATS_COBRADO))} en STATS_COBRADO"
    )
    # Y lo que sacamos, sacado a propósito y con motivo escrito.
    for muerto in del_dbase - esperado:
        assert estados.NO_USADOS[muerto], f"{muerto} sin motivo declarado"


def test_stats_cobrado_no_agrega_nada_que_el_dbase_excluya():
    """No inventamos estados: sólo BWVCIK + los legacy de PC (J, A).

    'X' (anulado), 'Z' (en cartera), 'P' (postergado), 'D', 'E' y los
    rebotes 1/2/3/R NO son cobranza y no pueden colarse acá.
    """
    prohibidos = {"X", "Z", "P", "D", "E", "1", "2", "3", "R", "T"}
    assert prohibidos.isdisjoint(q.STATS_COBRADO)


def test_cheques_STATS_DEPOSITADO_sigue_sin_C():
    """Guardarraíl: 'C' NO va en la lista de cheques.

    Allá `STATS_DEPOSITADO` significa "el cheque está en el banco" y
    lockea los campos duros de la ficha. Un cheque cobrado en caja NO
    está depositado: meter 'C' ahí rompe la cobranza. Son dos preguntas
    distintas sobre el mismo campo.
    """
    assert "C" not in chq.STATS_DEPOSITADO


@pytest.mark.parametrize(
    "caso",
    [
        ("lista", lambda f: f(anio=2026, mes=7)),
        ("cobranzas_detalle", lambda f: f("PPR", anio=2026, mes=7)),
        # Agregada el 2026-08-04 para que el mes a mes del portal no pida el
        # detalle 8 veces. Si contara los cheques con otra lista de estados,
        # la columna de meses no cerraría con la pantalla del mes.
        ("cobranzas_por_cliente_anio", lambda f: f("PPR", anio=2026, hasta_mes=8)),
    ],
)
def test_toda_query_de_cobranza_filtra_por_STATS_COBRADO(sql_de, caso):
    """Las tres queries usan LA MISMA lista, sea cual sea.

    TMT 2026-08-11: el fragmento esperado se ARMA desde `q.STATS_COBRADO` en
    vez de estar tipeado. Antes decía `('B','V','W','I','J','K','A','C')` a
    mano, así que sacar dos estados muertos rompía un test que no habla de esos
    estados: habla de que las tres queries filtren por la misma definición.
    """
    nombre, invocar = caso
    sql = invocar(lambda *a, **k: sql_de(getattr(q, nombre), *a, **k))
    esperado = "ch.stat IN (" + ", ".join(f"'{s}'" for s in q.STATS_COBRADO) + ")"
    assert esperado in sql, f"{nombre} no filtra por STATS_COBRADO ({esperado})"


def test_ninguna_lista_de_stats_quedo_hardcodeada_en_el_fuente():
    """TODA aparición de `stat IN (...)` sale de `_IN_COBRADO`.

    Si alguien vuelve a escribir la lista a mano, se pueden desincronizar
    otra vez — que es exactamente como nació este bug.

    ⚠ Antes esto contaba las apariciones (`== 3`) y se rompía cada vez que se
    agregaba una query legítima — le pasó a `cobranzas_por_cliente_anio` el
    2026-08-04. Un test que hay que "arreglar" para agregar código correcto
    enseña a editarlo sin leerlo, y el día que detecte algo de verdad lo van
    a editar igual. Lo que importa no es cuántas son: es que ninguna tenga la
    lista escrita a mano.
    """
    import inspect
    import re

    fuente = inspect.getsource(q)
    cuerpo = fuente.split("_IN_COBRADO = ", 1)[1].split("\n", 1)[1]
    # Sin los comentarios SQL: ahí la lista se MENCIONA a propósito (la
    # explicación de la paridad con el PRG cita `stat IN (B, A)`), y no es
    # código que pueda desincronizarse.
    cuerpo = "\n".join(
        ln for ln in cuerpo.splitlines() if not ln.lstrip().startswith("--")
    )

    apariciones = re.findall(r"stat\s+IN\s+\(([^)]*)\)", cuerpo, re.IGNORECASE)
    assert apariciones, "desapareció el filtro de estados de cobranza"
    for a in apariciones:
        assert a.strip() == "{_IN_COBRADO}", f"lista de stats escrita a mano: {a}"


# ── Bug 2: los códigos de cliente duplicados ────────────────────────────

@pytest.mark.parametrize(
    "caso",
    [
        ("lista", lambda f: f(anio=2026, mes=7)),
        ("cobranzas_detalle", lambda f: f("PPR", anio=2026, mes=7)),
        ("ventas_detalle", lambda f: f("PPR", anio=2026, mes=7)),
    ],
)
def test_toda_query_deduplica_los_clientes(sql_de, caso):
    """Un código repetido en scintela.cliente NO puede duplicar la plata."""
    nombre, invocar = caso
    sql = invocar(lambda *a, **k: sql_de(getattr(q, nombre), *a, **k))
    assert "DISTINCT ON (codigo_cli)" in sql
    # y nadie joinea la tabla cruda por atrás
    assert "JOIN scintela.cliente" not in sql


def test_el_desempate_del_cte_es_determinista(sql_de):
    """Sin ORDER BY completo, DISTINCT ON elige una fila al azar.

    Prioriza la fila QUE TIENE vendedor (la vacía no sirve para comisión)
    y desempata por nombre.
    """
    sql = sql_de(q.lista, anio=2026, mes=7)
    cte = sql.split("cli AS (", 1)[1].split(")", 1)[0]
    assert "ORDER BY codigo_cli" in sql
    assert "TRIM(vend), '') = ''" in q._CTE_CLI
    assert q._CTE_CLI.rstrip().endswith("nombre\n        )")
    assert "codigo_cli" in cte


def test_ventas_tambien_deduplica(sql_de):
    """El duplicado inflaba ventas igual que cobranza ($23.795,07 en julio)."""
    sql = sql_de(q.lista, anio=2026, mes=7)
    ventas = sql.split("ventas_mes AS (", 1)[1]
    assert "JOIN cli c" in ventas
    assert "JOIN scintela.cliente" not in ventas


def test_n_clientes_cuenta_clientes_no_filas(sql_de):
    """`COUNT(DISTINCT codigo_cli)` + CTE deduplicado."""
    sql = sql_de(q.lista, anio=2026, mes=7)
    assert "COUNT(DISTINCT codigo_cli)" in sql
