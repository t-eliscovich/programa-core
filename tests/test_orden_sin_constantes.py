"""Ningún buscador puede mandar una CONSTANTE en el ORDER BY.

TMT 2026-08-04 — este archivo existe porque el mismo error se deployó DOS
veces el mismo día, y las dos veces la pantalla no explotó: mintió.

    ORDER BY 0     → ERROR: ORDER BY position 0 is not in select list
                     (un entero suelto es una referencia POSICIONAL a la
                      columna N del SELECT)
    ORDER BY NULL  → ERROR: non-integer constant in ORDER BY

Las dos son válidas en SQLite (que es contra lo que corre el harness de
tests/test_clientes_buscar_ranking.py) y las dos revientan en Postgres. Y
como las vistas envuelven las consultas en `_safe()` / try-except, el 500
se convierte en una lista vacía:

  · el "¿quisiste decir?" del estado de cuenta no salía NUNCA;
  · `/clientes` SIN filtro decía "Mostrando 1-0 de 0 clientes".

Este test toma el SQL REAL que cada buscador le manda a la base (con la DB
mockeada), le saca la cláusula ORDER BY y exige que ningún término sea una
constante. Es la red que faltaba: no depende de que haya una DB.
"""
from __future__ import annotations

import re
from unittest.mock import patch

import pytest


# Cada caso: (módulo con `db`, cómo llamarlo). La lista tiene que cubrir
# TODOS los buscadores — si agregás uno, sumalo acá.
def _casos():
    from modules.activos import queries as activos_q
    from modules.cheques import queries as cheques_q
    from modules.clientes import queries as clientes_q
    from modules.facturas import queries as facturas_q
    from modules.gastos import queries as gastos_q
    from modules.informes import queries as informes_q
    from modules.posdat import queries as posdat_q
    from modules.proformas import queries as proformas_q
    from modules.proveedores import queries as proveedores_q
    from modules.retenciones import queries as retenciones_q

    casos = []
    for etiqueta, modulo, correr in [
        ("informes.buscar_clientes", informes_q,
         lambda m: m.buscar_clientes("irene condor")),
        ("clientes.buscar", clientes_q, lambda m: m.buscar("irene condor")),
        ("clientes.buscar SIN filtro", clientes_q, lambda m: m.buscar("")),
        ("clientes.contar", clientes_q, lambda m: m.contar("")),
        ("clientes.directorio", clientes_q, lambda m: m.directorio("")),
        ("proveedores.buscar", proveedores_q, lambda m: m.buscar("")),
        ("proveedores.contar", proveedores_q, lambda m: m.contar("")),
        ("facturas.buscar", facturas_q, lambda m: m.buscar("irene condor")),
        ("facturas.contar_filtrado", facturas_q,
         lambda m: m.contar_filtrado("irene condor")),
        ("cheques.buscar", cheques_q, lambda m: m.buscar("irene condor")),
        ("cheques.total_buscar", cheques_q,
         lambda m: m.total_buscar("irene condor")),
        ("activos.buscar", activos_q, lambda m: m.buscar("torno")),
        ("gastos.buscar", gastos_q, lambda m: m.buscar("flete")),
        ("posdat.buscar", posdat_q, lambda m: m.buscar(q="hilos")),
        ("posdat.resumen", posdat_q, lambda m: m.resumen(q="hilos")),
        ("proformas.buscar", proformas_q, lambda m: m.buscar("irene")),
        ("retenciones.buscar", retenciones_q, lambda m: m.buscar("irene")),
    ]:
        casos.append(pytest.param(modulo, correr, id=etiqueta))
    return casos


def _sqls_ejecutados(modulo, correr) -> list[str]:
    vistos: list[str] = []

    def espia(sql, params=None, conn=None):
        vistos.append(sql)
        return []

    def espia_one(sql, params=None, conn=None):
        vistos.append(sql)
        return {}

    with patch.object(modulo.db, "fetch_all", side_effect=espia), \
         patch.object(modulo.db, "fetch_one", side_effect=espia_one):
        correr(modulo)
    return vistos


def _terminos_order_by(sql: str) -> list[str]:
    """Los términos de cada ORDER BY del SQL, sin comentarios."""
    limpio = re.sub(r"--[^\n]*", "", sql)
    terminos: list[str] = []
    for m in re.finditer(r"\bORDER\s+BY\b(.*?)(?=\bLIMIT\b|\bOFFSET\b|\)|$)",
                         limpio, flags=re.S | re.I):
        # Coma de tope nivel (las de adentro de CASE/paréntesis no separan).
        cuerpo, nivel, actual = m.group(1), 0, ""
        for ch in cuerpo:
            if ch == "(":
                nivel += 1
            elif ch == ")":
                nivel -= 1
            if ch == "," and nivel == 0:
                terminos.append(actual)
                actual = ""
            else:
                actual += ch
        terminos.append(actual)
    return [t.strip() for t in terminos if t.strip()]


@pytest.mark.parametrize("modulo,correr", _casos())
def test_ningun_order_by_ordena_por_una_constante(modulo, correr):
    for sql in _sqls_ejecutados(modulo, correr):
        for termino in _terminos_order_by(sql):
            # Sacamos ASC/DESC/NULLS FIRST|LAST para mirar la expresión.
            expr = re.sub(r"\b(ASC|DESC|NULLS\s+(FIRST|LAST))\b", "",
                          termino, flags=re.I).strip()
            assert not re.fullmatch(r"\d+", expr), (
                f"{modulo.__name__}: `ORDER BY {expr}` — un entero suelto es "
                "una referencia POSICIONAL a la columna N (y 0 es un error)"
            )
            assert not re.fullmatch(r"NULL", expr, flags=re.I), (
                f"{modulo.__name__}: `ORDER BY NULL` es "
                "'non-integer constant in ORDER BY' en Postgres"
            )
            assert expr, f"{modulo.__name__}: término vacío en el ORDER BY"


def test_el_fallback_del_quisiste_decir_tambien_se_revisa():
    """La rama del typo NO se ejecuta con la DB vacía — hay que alimentarla.

    Justamente ahí vivía el bug: la primera versión ordenaba `ORDER BY 0` y
    la segunda `ORDER BY NULL`, y las dos morían tragadas por el `_safe()`
    de la vista. El caso parametrizado de arriba no la toca porque, con la
    base mockeada vacía, el padrón vuelve vacío y el fallback ni corre.
    """
    from modules.informes import queries as informes_q

    vistos: list[str] = []
    cola = [
        [],                                                  # búsqueda exacta
        [{"codigo_cli": "ICH", "nombre": "IRENE CHICAIZA CONDOR"}],  # padrón
        [{"codigo_cli": "ICH", "nombre": "IRENE CHICAIZA CONDOR",
          "saldo": 1}],                                      # filas aprox
    ]

    def espia(sql, params=None, conn=None):
        vistos.append(sql)
        return cola.pop(0) if cola else []

    with patch.object(informes_q.db, "fetch_all", side_effect=espia):
        filas = informes_q.buscar_clientes("chicaisa")

    assert filas and filas[0]["aprox"] is True, (
        "el '¿quisiste decir?' tiene que devolver filas marcadas aprox"
    )
    assert len(vistos) == 3, "esperaba exacta → padrón → filas aproximadas"
    for termino in _terminos_order_by(vistos[2]):
        expr = re.sub(r"\b(ASC|DESC|NULLS\s+(FIRST|LAST))\b", "",
                      termino, flags=re.I).strip()
        assert not re.fullmatch(r"\d+|NULL", expr, flags=re.I), (
            f"el fallback ordena por la constante `{expr}` → 500 en Postgres"
        )
