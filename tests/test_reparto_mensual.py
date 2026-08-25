"""El reparto de un monto mensual entre los días del mes (reparto_mensual.py).

Pedido de Tamara 25/08/2026: mismo monto mensual, repartido entre los días
REALES del mes, contando sábados y domingos. Corte: 01/09/2026.
"""
from datetime import date, timedelta

import pytest

import reparto_mensual as rm

# ------------------------------------------------------------------ días

@pytest.mark.parametrize("f, esperado", [
    (date(2026, 1, 15), 31),
    (date(2026, 2, 15), 28),
    (date(2028, 2, 15), 29),   # bisiesto
    (date(2026, 4, 15), 30),
])
def test_dias_del_mes(f, esperado):
    assert rm.dias_del_mes(f) == esperado


# --------------------------------------------------------------- activos

def test_antes_del_corte_el_activo_sigue_dividiendo_por_30():
    # 25/08/2026 — el día que se pidió el cambio. No se toca agosto.
    assert rm.coef_activos(date(2026, 8, 25)) == pytest.approx(25 / 30)
    # el tope viejo: el 31 no movía nada porque el 30 ya había llegado a 1
    assert rm.coef_activos(date(2026, 8, 30)) == 1.0
    assert rm.coef_activos(date(2026, 8, 31)) == 1.0


def test_desde_el_corte_el_activo_divide_por_los_dias_del_mes():
    assert rm.coef_activos(date(2026, 10, 1)) == pytest.approx(1 / 31)
    assert rm.coef_activos(date(2026, 10, 25)) == pytest.approx(25 / 31)


def test_septiembre_entra_sin_escalon():
    """El corte cae en un mes de 30: las dos fórmulas dan lo MISMO.

    Es la razón por la que el cambio no hace saltar la utilidad.
    """
    for dia in range(1, 31):
        f = date(2026, 9, dia)
        assert rm.coef_activos(f) == pytest.approx(min(dia, 30) / 30)


@pytest.mark.parametrize("f", [
    date(2026, 9, 30), date(2026, 10, 31), date(2027, 2, 28), date(2028, 2, 29),
])
def test_el_ultimo_dia_del_mes_cierra_en_uno_exacto(f):
    """Lo que hoy no pasa en febrero (cerraba en 28/30 = 93,3%)."""
    assert rm.coef_activos(f) == 1.0


def test_el_activo_sube_todos_los_dias_y_en_pedazos_iguales():
    """Ningún día del mes queda plano, y todos mueven lo mismo."""
    saltos = [
        rm.coef_activos(date(2026, 10, d + 1)) - rm.coef_activos(date(2026, 10, d))
        for d in range(1, 31)
    ]
    assert all(s > 0 for s in saltos)
    assert max(saltos) == pytest.approx(min(saltos))


def test_la_suma_de_los_dias_da_la_cuota_del_mes():
    cuota = 48200.0          # el total real de Intela al 25/08/2026
    dias = [date(2026, 10, d) for d in range(1, 32)]
    assert sum(rm.deprec_del_dia(cuota, d) for d in dias) == pytest.approx(cuota)


def test_deprec_del_dia_antes_del_corte_sigue_dividiendo_por_30():
    assert rm.deprec_del_dia(48200, date(2026, 8, 25)) == pytest.approx(48200 / 30)


def test_deprec_del_dia_tolera_none():
    assert rm.deprec_del_dia(None, date(2026, 10, 5)) == 0.0


def test_coef_activos_sql_llama_a_la_funcion_de_la_base():
    assert rm.coef_activos_sql() == (
        "scintela.coef_amortizacion((CURRENT_TIMESTAMP - INTERVAL '5 hours')::date)")
    assert rm.coef_activos_sql("%s::date") == "scintela.coef_amortizacion(%s::date)"


# ----------------------------------------------------------- provisiones

def test_antes_del_corte_la_provision_no_corre_el_fin_de_semana():
    assert rm.provision_corre(date(2026, 8, 28)) is True    # viernes
    assert rm.provision_corre(date(2026, 8, 29)) is False   # sábado
    assert rm.provision_corre(date(2026, 8, 30)) is False   # domingo


def test_desde_el_corte_la_provision_corre_sabados_y_domingos():
    assert rm.provision_corre(date(2026, 9, 5)) is True     # sábado
    assert rm.provision_corre(date(2026, 9, 6)) is True     # domingo


def test_antes_del_corte_la_provision_paga_la_misma_cuota_diaria_de_siempre():
    """195.750/mes es SUELDOS… no: es A,E,C (9.000/día × 21,75)."""
    assert rm.cuota_del_dia(195750, date(2026, 8, 25)) == pytest.approx(9000.0)
    assert rm.cuota_del_dia(182700, date(2026, 8, 25)) == pytest.approx(8400.0)


def test_desde_el_corte_la_provision_reparte_el_mes_entre_sus_dias():
    assert rm.cuota_del_dia(195750, date(2026, 9, 10)) == pytest.approx(195750 / 30)
    assert rm.cuota_del_dia(195750, date(2026, 10, 10)) == pytest.approx(195750 / 31)


def test_el_mes_de_provisiones_suma_el_mensual_completo():
    mensual = 724275.0       # el total de las 12 provisiones
    f = date(2026, 9, 1)
    total = 0.0
    while f.month == 9:
        if rm.provision_corre(f):
            total += rm.cuota_del_dia(mensual, f)
        f += timedelta(days=1)
    assert total == pytest.approx(mensual)


def test_el_mes_de_provisiones_da_igual_en_un_mes_de_31():
    mensual = 724275.0
    f = date(2026, 10, 1)
    total = 0.0
    while f.month == 10:
        total += rm.cuota_del_dia(mensual, f) if rm.provision_corre(f) else 0.0
        f += timedelta(days=1)
    assert total == pytest.approx(mensual)


def test_cuota_del_dia_tolera_none():
    assert rm.cuota_del_dia(None, date(2026, 9, 5)) == 0.0
