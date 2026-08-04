"""La comisión cuenta el cobro que entró a CAJA sin fila de cheque.

TMT 2026-08-04. Agregar el estado `'C'` arregló los cheques de ventanilla que
PC tiene cargados. Pero la primera semana de julio PC **no tiene la fila de
cheque**: `dbf-import` trajo sólo el movimiento de caja
(`tipo='E'`, `concepto='CH.'+CLIENTE`). El 01 y el 02/07 son 37 movimientos
por $34.040,14 que la comisión no veía — $2.212,00 de EDG y $1.305,00 de SEP.

⚠️ El arreglo NO es cargar esos cheques a mano: **la plata ya está en PC**, en
`scintela.caja`. Cargar el cheque la contaría dos veces, que es el error de la
madrugada del 04/08 ("apliqué 5 conversiones habiendo verificado 3, la deuda
bajó dos veces"). Estos tests fijan las dos mitades: que la caja se cuente, y
que **no se cuente dos veces** cuando el cheque sí existe.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.comisiones import queries as q  # noqa: E402


def _sql_de(nombre_fn) -> str:
    """El SQL que la función manda a la base, capturado con un db falso."""
    capt = {}

    class _FakeDB:
        def fetch_all(self, sql, params=None, conn=None):
            capt["sql"] = sql; capt["params"] = params; return []

        def fetch_one(self, sql, params=None, conn=None):
            capt["sql"] = sql; capt["params"] = params; return {"total": 0}

    original = q.db
    q.db = _FakeDB()
    try:
        nombre_fn()
    finally:
        q.db = original
    return capt["sql"]


TODAS = {
    "lista": lambda: q.lista(anio=2026, mes=7),
    "cobranzas_detalle": lambda: q.cobranzas_detalle("EDG", anio=2026, mes=7),
    "cobranzas_por_cliente_anio": lambda: q.cobranzas_por_cliente_anio("EDG", anio=2026),
    "cobranza_periodo": lambda: q.cobranza_periodo("EDG", "2026-07-01", "2026-07-31"),
}


# ---------------------------------------------------------------------------
# La rama de caja está en TODAS las consultas de cobranza
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("nombre", sorted(TODAS))
def test_todas_las_consultas_de_cobranza_miran_la_caja(nombre):
    """Si una sola se olvida, el total y el desglose dejan de coincidir.

    Ya pasó: `mi_cartera.cobrado` tenía su copia sin la 'C' y el KPI del
    vendedor no cerraba con el detalle que la misma pantalla mostraba abajo.
    """
    sql = _sql_de(TODAS[nombre])
    assert "scintela.caja" in sql, f"{nombre} no mira la caja"
    # `%%` y no `%`: el SQL va por psycopg2, que consume el primero.
    assert "'CH.%%'" in sql, f"{nombre} no filtra el concepto CH.<cliente>"


@pytest.mark.parametrize("nombre", sorted(TODAS))
def test_solo_cuenta_los_movimientos_de_ENTRADA(nombre):
    """`tipo='E'`. Una salida de caja no es cobranza de nadie."""
    assert "k.tipo = 'E'" in _sql_de(TODAS[nombre])


# ---------------------------------------------------------------------------
# ⭐ El anti-join — la mitad que evita contar dos veces
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("nombre", sorted(TODAS))
def test_no_cuenta_la_caja_si_el_cheque_existe(nombre):
    sql = _sql_de(TODAS[nombre])
    assert "NOT EXISTS" in sql
    assert "scintela.cheque chx" in sql


@pytest.mark.parametrize("nombre", sorted(TODAS))
def test_el_anti_join_compara_por_cliente_fecha_e_importe(nombre):
    """⭐ Y NO por `caja.id_cheque`.

    Las filas que trajo `dbf-import` tienen `id_cheque` en NULL **aunque el
    cheque exista** — el 06, el 07 y el 10 de julio están así. Un
    `WHERE k.id_cheque IS NULL` parece el filtro obvio y contaría esos tres
    días DOS VECES.
    """
    sql = _sql_de(TODAS[nombre])
    bloque = sql[sql.index("NOT EXISTS"):]
    bloque = bloque[:bloque.index(")))") + 3]
    assert "chx.fechad" in bloque and "k.fecha" in bloque
    assert "chx.importe" in bloque and "k.importe" in bloque
    assert "chx.codigo_cli" in bloque
    assert "id_cheque" not in q._CAJA_COBRO_FROM, (
        "el descarte NO puede depender de caja.id_cheque: las filas del "
        "dbf-import lo tienen en NULL aunque el cheque exista"
    )


@pytest.mark.parametrize("nombre", sorted(TODAS))
def test_el_anti_join_mira_solo_los_cheques_de_ventanilla(nombre):
    """Banco 99 = EFT./caja. Un cheque de banco no es el par de una caja."""
    assert "chx.no_banco = '99'" in _sql_de(TODAS[nombre])


def test_el_cliente_sale_del_concepto_CH_punto():
    """`concepto = 'CH.' + CLIENTE` es la convención de PASOCAJA."""
    assert "SUBSTRING(k.concepto FROM 4)" in q._CAJA_COBRO_FROM


# ---------------------------------------------------------------------------
# Una sola definición
# ---------------------------------------------------------------------------

def test_mi_cartera_no_tiene_su_propia_copia_de_la_consulta():
    """⭐ El KPI del vendedor y la comisión de la oficina, un solo lugar.

    `mi_cartera.cobrado` tenía la lista de estados hardcodeada y **sin la
    'C'**, así que el vendedor veía menos cobranza de la que se le pagaba.
    """
    ruta = os.path.join(_REPO_ROOT, "modules", "mi_cartera", "queries.py")
    with open(ruta, encoding="utf-8") as fh:
        fuente = fh.read()
    cuerpo = fuente[fuente.index("def cobrado("):]
    cuerpo = cuerpo[:cuerpo.index("\ndef ", 1)]
    assert "cobranza_periodo" in cuerpo, "tiene que delegar, no copiar"
    assert "scintela.cheque" not in cuerpo, "volvió a copiarse la consulta"
    assert not re.search(r"stat IN \(", cuerpo), "volvió la lista de estados propia"


def test_cobranza_periodo_suma_las_tres_fuentes():
    sql = _sql_de(TODAS["cobranza_periodo"])
    assert "scintela.cheque" in sql
    assert "scintela.cobro" in sql
    assert "scintela.caja" in sql


def test_cobranza_periodo_usa_la_misma_lista_de_estados():
    """Con la 'C'. Es la constante, no una copia."""
    sql = _sql_de(TODAS["cobranza_periodo"])
    assert q._IN_COBRADO in sql
    assert "'C'" in q._IN_COBRADO


def test_cobranza_periodo_devuelve_el_total(monkeypatch):
    class _FakeDB:
        def fetch_one(self, sql, params=None, conn=None):
            return {"total": 1234.56}

    monkeypatch.setattr(q, "db", _FakeDB())
    assert q.cobranza_periodo("EDG", "2026-07-01", "2026-07-31") == 1234.56


def test_cobranza_periodo_sin_datos_devuelve_cero(monkeypatch):
    """`None` es "no cobró nada", no un crash."""
    class _FakeDB:
        def fetch_one(self, sql, params=None, conn=None):
            return None

    monkeypatch.setattr(q, "db", _FakeDB())
    assert q.cobranza_periodo("EDG", "2026-07-01", "2026-07-31") == 0.0


# ---------------------------------------------------------------------------
# La caja REVERSADA tampoco cuenta
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("nombre", sorted(TODAS))
def test_una_caja_reversada_no_cuenta(nombre):
    """Caso ICH: el cobro entró en efectivo el 30/06 y el 15/07 se pasó a
    depósito (`REVERSO id 299 — ES DEPOSITO` + cheque nuevo en el banco). Es
    la MISMA plata: si la caja de junio cuenta y el cheque de julio también,
    se paga comisión dos veces por un solo cobro.
    """
    sql = _sql_de(TODAS[nombre])
    assert "kr.tipo = 'S'" in sql
    assert "REVERSO id " in sql


def test_el_reverso_se_matchea_con_borde_y_no_con_LIKE():
    """⭐ `LIKE 'REVERSO id ' || id || '%'` pesca de más.

    Con LIKE, la caja `id 2` matchea el texto "REVERSO id 299" y se cae de
    la comisión un cobro ajeno — medido en producción: CH.VIL, $2.488,01,
    que no tiene nada que ver con el reverso de ICH. El borde `([^0-9]|$)`
    es lo único que separa `id 2` de `id 299`.
    """
    frag = q._CAJA_COBRO_FROM
    assert "([^0-9]|$)" in frag, "falta el borde del id en el match del reverso"
    assert "LIKE 'REVERSO" not in frag, "el reverso NO se matchea con LIKE"
