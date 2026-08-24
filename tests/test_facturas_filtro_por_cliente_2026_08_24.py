"""Filtrar por cliente tardaba 5 segundos. Era el cache, no la consulta.

TMT 2026-08-24 (dueña): *"¿qué más podemos hacer más rápido?"*. Medido en
producción sobre /facturas:

    ?cliente=AJO    primera vez 4.915 ms · después 190 ms
    ?cliente=BED    primera vez 5.242 ms
    ?cliente=AJO&desde=…&hasta=…   primera vez 5.806 ms

El patrón no era "el cliente X es lento": era **la primera vez de CADA
combinación**. La pantalla le pide a Asinfo las facturas del rango que abarcan
las filas visibles, y `facturas_periodo` cachea por (desde, hasta): como el
rango salía de las fechas EXACTAS de las facturas de ese cliente, cada filtro
estrenaba su propia clave y volvía a pagar los ~20 meses de Asinfo.

Ahora el rango se redondea a meses enteros, así que casi todos los filtros
caen en la misma clave —2025-01-01 → fin del mes en curso— que además el
warmup mantiene caliente.
"""
from __future__ import annotations

import ast
import inspect
import textwrap


def _llamadas_a(func, nombre):
    arbol = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return [n for n in ast.walk(arbol)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == nombre]


def test_el_rango_se_redondea_a_meses_enteros():
    """Con la fecha exacta, cada cliente estrena su propia clave de cache."""
    from modules.facturas import views

    src = inspect.getsource(views.lista)
    assert "mn = mn.replace(day=1)" in src, (
        "el desde volvió a ser la fecha exacta de la primera factura: cada "
        "filtro nuevo vuelve a pagar los 5 segundos de Asinfo"
    )
    assert "_cal.monthrange(mx.year, mx.month)[1]" in src, (
        "el hasta tiene que llegar al FIN del mes, si no el rango cambia "
        "todos los días y el cache no sirve"
    )


def test_el_calentador_deja_listo_el_rango_ancho():
    """El primero que filtre un cliente no tiene por qué pagarlo él."""
    from modules._lib import warmup

    src = inspect.getsource(warmup._warm_once)
    assert "facturas_rango_ancho" in src
    assert "date(2025, 1, 1)" in src


def test_le_sigue_preguntando_a_asinfo():
    """El redondeo es para el cache, no para dejar de traer las columnas."""
    from modules.facturas import views

    assert _llamadas_a(views.lista, "facturas_periodo"), (
        "sin esta llamada las columnas KG AI / USD AI quedan vacías"
    )


def test_el_piso_de_2025_sigue_puesto():
    """Pedirle a Asinfo la cartera legacy (2021-2024) son 20 segundos y no
    matchea con nada: el piso es lo que lo evita."""
    from modules.facturas import views

    src = inspect.getsource(views.lista)
    assert "ASINFO_DESDE_EFECTIVO = _date(2025, 1, 1)" in src
    assert "mn = max(min(fechas_2025_plus), ASINFO_DESDE_EFECTIVO)" in src
