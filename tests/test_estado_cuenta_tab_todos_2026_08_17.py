"""La pestaña "Todos" del estado de cuenta.

Pedido de la dueña 2026-08-17: *"estados de cuenta, podes dejar imprimir
todos, un ultimo que diga todos"*. Hasta acá sólo se podía imprimir el lote
DENTRO de una agrupación: un vendedor, una provincia, o los grupos de 2+
clientes (que deja afuera a los que no tienen grupo). No había forma de
imprimir a todos los que deben de una sola vez.

En el medio se probó traerle al renglón las cifras del encabezado de la ficha
(cheques por cobrar, total, cupo, % usado) y la dueña lo bajó: *"mejor solo
saldo. como todo el resto"*. Por eso hay tests que EXIGEN que el renglón de
"Todos" sea idéntico al de las otras pestañas — si alguien vuelve a agregar
columnas ahí, esto falla y obliga a preguntar de nuevo.

Tests de PLANTILLA (no tocan Postgres), en la misma línea que
test_estado_cuenta_impresiones.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "modules/informes/templates/informes"
GRUPOS = (TPL / "estado_cuenta_grupos.html").read_text(encoding="utf-8")
LANDING = (TPL / "estado_cuenta_landing.html").read_text(encoding="utf-8")
QUERIES = (ROOT / "modules/informes/queries.py").read_text(encoding="utf-8")
VIEWS = (ROOT / "modules/informes/views.py").read_text(encoding="utf-8")


def _sql_de(nombre: str) -> str:
    """El cuerpo de una función de queries.py, hasta el siguiente `def`."""
    i = QUERIES.index(f"def {nombre}(")
    j = QUERIES.find("\ndef ", i + 1)
    return QUERIES[i : j if j > 0 else len(QUERIES)]


# ---------------------------------------------------------------------------
# 1. La pestaña existe, va ÚLTIMA, y está en las dos barras
# ---------------------------------------------------------------------------


def test_la_pestana_todos_va_ultima():
    """"un ultimo que diga todos" — literal: última de la barra."""
    tabs = re.search(r"\{% set tabs = \[(.*?)\] %\}", GRUPOS, re.S).group(1)
    claves = re.findall(r"\('(\w+)',", tabs)
    assert claves[-1] == "todos", f"'todos' tiene que ir última, quedó {claves}"
    assert claves == ["cliente", "vendedor", "provincia", "grupo", "todos"]


def test_la_barra_del_landing_tiene_la_misma_pestana():
    """Las dos barras son la misma barra: si una tiene Todos, la otra también."""
    assert "('todos','Todos')" in LANDING


@pytest.mark.parametrize("por", ["vendedor", "grupo", "provincia", "todos"])
def test_las_dos_vistas_aceptan_todos_como_dimension(por):
    """`por=todos` no puede caer al default 'vendedor' en ninguna de las dos."""
    assert VIEWS.count(f'"{por}"') >= 2
    for fn in ("estado_cuenta_grupos", "estado_cuenta_lote_imprimir"):
        i = VIEWS.index(f"def {fn}(")
        cuerpo = VIEWS[i : VIEWS.index("\n@informes_bp.route", i + 1)]
        assert '("vendedor", "grupo", "provincia", "todos")' in cuerpo


# ---------------------------------------------------------------------------
# 2. Orden alfabético por código (decisión dueña, y ya vale para Alex)
# ---------------------------------------------------------------------------


def test_todos_ordena_alfabeticamente_por_codigo():
    """Mismo criterio que ya pidió Alex para imprimir por vendedor/provincia:
    la hoja sale ordenada igual sin importar por dónde se entró."""
    for fn in ("estado_cuenta_grupos", "estado_cuenta_lote_imprimir"):
        i = VIEWS.index(f"def {fn}(")
        cuerpo = VIEWS[i : VIEWS.index("\n@informes_bp.route", i + 1)]
        j = cuerpo.index('por == "todos"')
        tramo = cuerpo[j : j + 900]
        assert 'key=lambda r: (r.get("codigo_cli") or "").upper()' in tramo, (
            f"{fn} no ordena por código en la rama 'todos'"
        )


# ---------------------------------------------------------------------------
# 3. El renglón de "Todos" es el MISMO que el de las otras pestañas
# ---------------------------------------------------------------------------


def _bloque(nombre: str) -> str:
    ini = GRUPOS.index(f"MODE: {nombre}")
    fin = GRUPOS.find("MODE:", ini + 1)
    return GRUPOS[ini : fin if fin > 0 else len(GRUPOS)]


def test_todos_muestra_codigo_cliente_y_saldo():
    """Dueña: "mejor solo saldo. como todo el resto"."""
    bloque = _bloque("TODOS")
    for col in ("Código", "Cliente", "Saldo"):
        assert f">{col}<" in bloque, f"falta la columna {col}"
    assert "c.saldo | money_es" in bloque


@pytest.mark.parametrize(
    "col", ["Cheques", "Total", "Cupo", "% usado"]
)
def test_todos_no_trae_las_columnas_de_la_ficha(col):
    """Se probaron y la dueña las bajó. Si alguien las re-agrega sin preguntar,
    este test lo frena: el desglose vive en la ficha del cliente."""
    assert f">{col}<" not in _bloque("TODOS")


def test_las_tres_listas_tienen_el_mismo_encabezado():
    """"como todo el resto", literal: mismo renglón en las tres."""
    encabezados = {}
    for nombre in ("LIST", "GRUPO", "TODOS"):
        encabezados[nombre] = re.findall(r'<th[^>]*>\s*(.*?)\s*</th>', _bloque(nombre), re.S)
    assert encabezados["TODOS"] == encabezados["LIST"] == encabezados["GRUPO"], encabezados


def test_el_codigo_linkea_a_la_ficha_del_cliente():
    """El desglose que no está en el renglón se llega clickeando el código."""
    assert "url_for('informes.estado_cuenta', codigo_cli=c.codigo_cli)" in _bloque("TODOS")


# ---------------------------------------------------------------------------
# 4. Las otras pestañas NO se tocaron (dueña: "mejor mantené como está el resto")
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("modo", ["LIST", "GRUPO"])
def test_las_otras_listas_no_se_tocaron(modo):
    """Vendedor / Provincia / Grupos siguen mostrando código, cliente y saldo."""
    bloque = _bloque(modo)
    assert ">Cupo<" not in bloque and ">% usado<" not in bloque
    assert "c.saldo | money_es" in bloque
