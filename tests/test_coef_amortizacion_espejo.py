"""La función de la base y la de Python tienen que dar el MISMO número.

El reparto de la cuota mensual vive en dos lados: `reparto_mensual.py` (para
lo que se calcula en Python) y `scintela.coef_amortizacion()` (para lo que se
calcula adentro de un SELECT — migración 0221). Si se separan, el valor en
libros de la pantalla de activos deja de coincidir con el del balance y nadie
se entera hasta que la utilidad no cierra.

Corre contra el Postgres de verdad (el mismo que usa el resto de los `-m db`):

    pytest tests/test_coef_amortizacion_espejo.py -q -m db
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import reparto_mensual as rm  # noqa: E402

pytestmark = pytest.mark.db


@pytest.fixture
def cur(migrated_db, real_db_conn):
    """Cursor sobre la DB con TODAS las migraciones aplicadas (incluida la 0221)."""
    with real_db_conn.cursor() as c:
        yield c
    real_db_conn.rollback()


def _dias_de_prueba():
    """Un año entero alrededor del corte, más los bordes que muerden."""
    f = date(2026, 7, 1)
    while f <= date(2027, 7, 1):
        yield f
        f += timedelta(days=1)
    for f in (date(2028, 2, 28), date(2028, 2, 29), date(2028, 3, 1)):
        yield f


def test_la_base_y_python_dan_el_mismo_coeficiente(cur):
    dias = list(_dias_de_prueba())
    cur.execute(
        "SELECT d, scintela.coef_amortizacion(d) FROM unnest(%s::date[]) AS d",
        (dias,),
    )
    filas = cur.fetchall()
    assert len(filas) == len(dias)
    for d, coef_sql in filas:
        assert float(coef_sql) == pytest.approx(rm.coef_activos(d)), d


def test_la_base_y_python_dividen_por_los_mismos_dias(cur):
    dias = list(_dias_de_prueba())
    cur.execute(
        "SELECT d, scintela.divisor_amortizacion(d) FROM unnest(%s::date[]) AS d",
        (dias,),
    )
    for d, div_sql in cur.fetchall():
        esperado = rm.dias_del_mes(d) if d >= rm.CORTE_DIAS_REALES else 30
        assert float(div_sql) == pytest.approx(esperado), d


def test_el_ultimo_dia_de_cada_mes_cierra_en_uno(cur):
    """Desde el corte, ningún mes puede quedar con cuota sin correr.

    Es justo lo que hoy NO pasa: febrero cierra en 28/30 = 93,3%.
    """
    cur.execute("""
        SELECT x.d::date, scintela.coef_amortizacion(x.d::date)
          FROM generate_series(DATE '2026-09-01', DATE '2027-12-01', INTERVAL '1 month') g(d0),
               LATERAL (SELECT g.d0 + INTERVAL '1 month' - INTERVAL '1 day' AS d) x
    """)
    filas = cur.fetchall()
    assert len(filas) == 16
    for d, coef in filas:
        assert float(coef) == 1.0, d


def test_ningun_dia_del_mes_queda_plano(cur):
    """En un mes de 31 la regla vieja no movía nada el día 31."""
    cur.execute("""
        SELECT scintela.coef_amortizacion(DATE '2026-10-30'),
               scintela.coef_amortizacion(DATE '2026-10-31')
    """)
    d30, d31 = cur.fetchone()
    assert float(d31) > float(d30)


def test_la_proc_mensual_usa_el_mismo_coeficiente(cur):
    """`actualizar_amortizacion()` no puede tener su propia cuenta."""
    hoy = date.today()
    cur.execute("DELETE FROM scintela.activos")
    cur.execute(
        "INSERT INTO scintela.activos (concepto, tipo, inicial, amortizac, cuota, "
        "ult_mes_amortizado) VALUES ('TELAR DE PRUEBA', 'M', 120000, 0, 1000, %s)",
        (hoy.year * 100 + hoy.month,),   # el mes ya está procesado: no suma cuota
    )
    cur.execute("SELECT scintela.actualizar_amortizacion()")
    cur.execute("SELECT amortimes, valor FROM scintela.activos")
    amortimes, valor = cur.fetchone()
    assert float(amortimes) == pytest.approx(1000 * rm.coef_activos(hoy), abs=0.01)
    assert float(valor) == pytest.approx(120000 - float(amortimes), abs=0.01)
