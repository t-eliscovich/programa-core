"""rango_ancho_desde — piso ANCHO compartido para el caché de facturas_periodo.

TMT 2026-09-14: antes, cada consumidor de un rango "de varios años" hacia
Asinfo hardcodeaba su propia fecha fija (`date(2025, 1, 1)`), una en
`modules/_lib/warmup.py` (el paso que mantiene el caché caliente) y otra,
independiente, en `modules/informes/queries.py::ventas_cliente_por_mes`.
Cuando alguien pedía un rango más ancho que esa fecha fija (ej. "Ver 24
meses" con HOY en 2026: el `desde` calculado caía en 2024), la clave de
`facturas_periodo`'s cache no coincidía con la que el warmup mantenía
caliente, y esa request pagaba un fetch en frío de 10-30s a Metabase.

`rango_ancho_desde()` es la única fuente de verdad para ese piso: siempre
1° de enero de hace `RANGO_ANCHO_ANIOS - 1` años. Warmup y consumidores
deben llamarla en vez de hardcodear la fecha.
"""
from __future__ import annotations

from datetime import date

from modules.asinfo import service as asvc


def test_tres_anios_por_defecto():
    assert asvc.RANGO_ANCHO_ANIOS == 3


def test_primero_de_enero_hace_dos_anios():
    assert asvc.rango_ancho_desde(date(2026, 9, 14)) == date(2024, 1, 1)
    assert asvc.rango_ancho_desde(date(2026, 1, 1)) == date(2024, 1, 1)
    assert asvc.rango_ancho_desde(date(2026, 12, 31)) == date(2024, 1, 1)


def test_sin_argumento_usa_hoy():
    hoy = date.today()
    assert asvc.rango_ancho_desde() == date(hoy.year - 2, 1, 1)


def test_warmup_y_ventas_cliente_usan_la_misma_funcion():
    """Que las dos NUNCA vuelvan a divergir con fechas hardcodeadas propias."""
    import inspect

    from modules._lib import warmup
    from modules.informes import queries

    assert "rango_ancho_desde" in inspect.getsource(warmup._warm_once)
    assert "rango_ancho_desde" in inspect.getsource(queries.ventas_cliente_por_mes)
