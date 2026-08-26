"""El mes de las comisiones, preguntado como RANGO de fechas.

⭐ TMT 2026-08-26 (dueña): *"algo más que dure mucho tiempo y podamos bajar"*.

Las consultas de comisiones filtraban el mes con
`EXTRACT(YEAR FROM fechad) = … AND EXTRACT(MONTH FROM fechad) = …`. Una columna
envuelta en una función no puede usar ningún índice, y además deja ciego al
planificador. Ahora se pregunta por un rango: `>= el primero del mes` y
`< el primero del siguiente`.

Medido contra una base sembrada a escala de producción, las 113 llamadas que
hacen las pantallas de comisiones pasaron de 4.038 ms a 890 ms, devolviendo
exactamente las mismas 25.398 filas.

Lo que protegen estos tests es lo único que se puede romper en un cambio así:
**que el rango no sea el mismo mes**. Un día de más o de menos en el borde no
tira ningún error — mueve plata de un mes al otro, y la comisión de un vendedor
sale mal.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from modules.comisiones import queries as q

# ---------------------------------------------------------------------------
# El rango es EXACTAMENTE el mes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("anio", [2024, 2025, 2026])
@pytest.mark.parametrize("mes", list(range(1, 13)))
def test_el_rango_cubre_el_mes_entero_y_ni_un_dia_mas(anio, mes):
    """La prueba de fondo: para CADA día de CADA mes, el rango contesta lo
    mismo que contestaba `EXTRACT(YEAR)=… AND EXTRACT(MONTH)=…`.

    Incluye febrero de un año bisiesto (2024) y diciembre, que es donde un
    rango mal armado se lleva enero del año siguiente por delante.
    """
    desde, hasta = q._rango_mes(anio, mes)
    dia = date(anio, mes, 1)
    while dia < hasta:
        assert desde <= dia < hasta, dia
        assert (dia.year, dia.month) == (anio, mes), dia
        dia += timedelta(days=1)
    # El día anterior al mes y el primero del siguiente quedan AFUERA.
    assert not (desde <= desde - timedelta(days=1) < hasta)
    assert not (desde <= hasta < hasta)
    assert (hasta - timedelta(days=1)).month == mes


def test_diciembre_rueda_al_ano_siguiente():
    """El mes 13 no existe: sin esto, diciembre revienta al armar la fecha."""
    assert q._rango_mes(2025, 12) == (date(2025, 12, 1), date(2026, 1, 1))
    assert q._primero_del_siguiente(2025, 12) == date(2026, 1, 1)
    assert q._primero_del_siguiente(2025, 11) == date(2025, 12, 1)


def test_el_ano_hasta_un_mes_va_de_enero_a_ese_mes_INCLUSIVE():
    """La lista "mes a mes" del portal pide "hasta agosto" y espera agosto
    adentro."""
    assert q._rango_hasta_mes(2026, 1) == (date(2026, 1, 1), date(2026, 2, 1))
    assert q._rango_hasta_mes(2026, 8) == (date(2026, 1, 1), date(2026, 9, 1))
    assert q._rango_hasta_mes(2026, 12) == (date(2026, 1, 1), date(2027, 1, 1))


def test_los_meses_no_se_pisan_ni_dejan_huecos():
    """Doce meses seguidos tienen que cubrir el año entero sin superponerse:
    si dos rangos se pisan, un cheque cuenta dos veces."""
    fin_anterior = date(2026, 1, 1)
    for mes in range(1, 13):
        desde, hasta = q._rango_mes(2026, mes)
        assert desde == fin_anterior, f"hueco o pisada en el mes {mes}"
        fin_anterior = hasta
    assert fin_anterior == date(2027, 1, 1)


def test_acepta_los_numeros_como_vienen_de_la_pantalla():
    """`?anio=2026&mes=08` llega como texto."""
    assert q._rango_mes("2026", "08") == (date(2026, 8, 1), date(2026, 9, 1))
    assert q._rango_hasta_mes("2026", "12") == (date(2026, 1, 1), date(2027, 1, 1))


# ---------------------------------------------------------------------------
# Que no vuelva el EXTRACT, y que a ninguna consulta le falte un parámetro
# ---------------------------------------------------------------------------


def test_ninguna_consulta_filtra_el_mes_con_EXTRACT():
    """El guard: si alguien vuelve a escribir `EXTRACT(YEAR FROM x) = …` en un
    WHERE, la pantalla sigue andando —da lo mismo— y se pone lenta de nuevo sin
    que nadie lo note. Acá se nota.

    ⚠ En el SELECT sí se permite (`EXTRACT(MONTH FROM fechad) AS mes` es una
    columna de salida, no un filtro): lo que no puede volver es la comparación.
    """
    from pathlib import Path

    fuente = Path(q.__file__).read_text(encoding="utf-8")
    # Sólo el código, sin los comentarios que explican por qué se fue.
    sin_comentarios = "\n".join(
        x for x in fuente.split("\n") if not x.lstrip().startswith(("#", "--")))
    malos = re.findall(r"EXTRACT\([A-Z]+ FROM [\w.]+\)\s*(?:<=|>=|=|<|>)\s*%",
                       sin_comentarios)
    assert not malos, f"volvió el filtro por EXTRACT: {malos}"


def _consultas_de(fn, *a, **kw):
    """Corre una función de consulta con la base falsa y devuelve los (sql,
    params) que hubiera mandado."""
    import db

    vistas = []

    def _cap(sql, params=None, conn=None):
        vistas.append((sql, params))
        return []

    original = (db.fetch_all, db.fetch_one)
    db.fetch_all, db.fetch_one = _cap, _cap
    try:
        fn(*a, **kw)
    finally:
        db.fetch_all, db.fetch_one = original
    return vistas


@pytest.mark.parametrize("nombre,llamada", [
    ("lista", lambda: q.lista(anio=2026, mes=8)),
    ("lista sin mes", lambda: q.lista()),
    ("cobranzas_detalle", lambda: q.cobranzas_detalle("PPR", anio=2026, mes=12)),
    ("cobranza_periodo", lambda: q.cobranza_periodo(
        "PPR", date(2026, 8, 1), date(2026, 8, 31))),
    ("cobranzas_por_cliente_anio", lambda: q.cobranzas_por_cliente_anio(
        "PPR", anio=2026, hasta_mes=12)),
    ("ventas_detalle", lambda: q.ventas_detalle("PPR", anio=2026, mes=2)),
])
def test_a_ninguna_consulta_le_falta_un_parametro(nombre, llamada):
    """⭐ El error que este cambio podía dejar suelto y que NO se ve en
    pantalla hasta producción: cambiar el `WHERE` y olvidarse de agregar la
    fecha al diccionario de parámetros. Postgres tira ahí mismo
    (`ProgrammingError`), pero recién cuando alguien abre la pantalla.

    Acá se corre cada consulta contra una base falsa y se comprueba que todo
    `%(loquesea)s` del SQL tenga su valor.
    """
    for sql, params in _consultas_de(llamada):
        pedidos = set(re.findall(r"%\((\w+)\)s", sql))
        assert pedidos <= set(params or {}), (
            f"{nombre}: al SQL le faltan {pedidos - set(params or {})}")
        for clave in ("f_desde", "f_hasta"):
            if clave in pedidos:
                assert isinstance(params[clave], date), (
                    f"{nombre}: {clave} tiene que ser una fecha, no "
                    f"{type(params[clave]).__name__}")


def test_el_rango_que_viaja_es_el_del_mes_pedido():
    """Y que sea el mes que se pidió, no el de hoy."""
    for sql, params in _consultas_de(
            lambda: q.cobranzas_detalle("PPR", anio=2025, mes=12)):
        if "f_desde" in sql:
            assert params["f_desde"] == date(2025, 12, 1)
            assert params["f_hasta"] == date(2026, 1, 1)
