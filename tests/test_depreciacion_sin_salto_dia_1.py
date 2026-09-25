"""La depreciación no salta la madrugada del día 1 (Tamara 2026-09-25).

A la medianoche del día 1 el coeficiente pasa a 1/31 pero la cuota del mes que
terminó entra a `amortizac` recién con la tarea de las 06:00. El valor en
libros suma esa cuota PENDIENTE con la misma condición que la tarea, así baja
sólo un día a la medianoche y no se mueve a las 06:00.

Simulado contra Postgres real (25/09) con `coef_amortizacion` de la mig 0221:
30/09 → 01/10 baja cuota/31 exacto; antes/después de la tarea de las 06:00, 0,00.
"""
import inspect

from modules.activos import queries as aq


def test_la_condicion_es_la_de_la_tarea_de_las_06():
    sql = aq.cuota_pendiente_sql("a", "DATE '2026-10-01'")
    assert "COALESCE(a.cuota, 0) > 0" in sql
    assert "COALESCE(a.inicial, 0) - COALESCE(a.amortizac, 0) > 0.01" in sql
    assert "a.ult_mes_amortizado IS DISTINCT FROM" in sql
    # un activo sin marca cuenta sólo si se cargó ANTES de este mes (EC)
    assert "(a.ult_mes_amortizado IS NOT NULL OR a.fecha_crea < " in sql
    assert "a.fecha_crea < date_trunc('month', DATE '2026-10-01') + INTERVAL '5 hours'" in sql
    assert "THEN 1 ELSE 0 END" in sql


def test_sin_alias():
    sql = aq.cuota_pendiente_sql()
    assert "COALESCE(cuota, 0)" in sql and "a.cuota" not in sql
    assert aq.HOY_EC_SQL in sql


def test_todos_los_valores_en_libros_la_usan():
    from modules.informes import foto
    from modules.informes import queries as iq
    assert "cuota_pendiente_sql" in inspect.getsource(iq)
    assert "_pend()" in inspect.getsource(iq)
    assert "{pend}" in inspect.getsource(foto)
    src = inspect.getsource(aq)
    assert src.count("+ {pend_a}) * COALESCE(a.cuota, 0)") == 2     # /activos
    assert src.count("+ {pend}) * COALESCE(cuota, 0)") == 2         # resumen


def test_el_gasto_del_mes_no_la_suma():
    """La depreciación del MES (gastos) es la del mes en curso: la cuota
    pendiente del anterior no es gasto de este mes."""
    from modules.informes import queries as iq
    assert "cuota_pendiente" not in inspect.getsource(iq.amortizaciones_mensuales)


def test_los_activos_nuevos_nacen_marcados_con_su_mes():
    src = inspect.getsource(aq)
    assert src.count("ult_mes_amortizado)") >= 2
    assert src.count('""" + MES_EC_SQL + """') == 2
