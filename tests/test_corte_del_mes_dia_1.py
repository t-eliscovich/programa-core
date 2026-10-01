"""El día 1 es del mes (Tamara 2026-10-01).

El corte del stock inicial era el día 1 (`fecha <= 01`), y los movimientos del
mes arrancaban `fecha > 01`: lo producido el día 1 quedaba adentro del inicial
y nunca se veía como producción. El 01/10 tejeduría mostraba 0 kg con 5.570 kg
ingresados. Ahora el corte es el cierre del día anterior."""
from datetime import date
from unittest.mock import patch

from modules.asinfo import service


def test_el_corte_es_el_ultimo_dia_del_mes_anterior():
    assert service.corte_del_mes(2026, 10) == date(2026, 9, 30)
    assert service.corte_del_mes(2026, 1) == date(2025, 12, 31)
    assert service.corte_del_mes(2026, 3) == date(2026, 2, 28)


def test_tejeduria_pide_el_ingreso_desde_el_dia_anterior():
    from modules.tejeduria_asinfo import service as tej
    with patch.object(service, "ingreso_bodega_por_dia", return_value=[]) as f:
        tej._ingreso_por_dia(2026, 10)
    assert f.call_args.args[1] == date(2026, 9, 30)


def test_el_dolar_por_kilo_del_balance_no_cambia_de_corte():
    with patch.object(service, "inventario_por_etapa_a_fecha", return_value={}) as inic, \
         patch.object(service, "inventario_por_etapa", return_value={}):
        service.mov_hilado_valuacion(2026, 10, 3.0)
    # el balance NO cambia: su $/kg sigue cortando el 1° (Tamara 01/10)
    assert inic.call_args.args[0] == date(2026, 10, 1)
