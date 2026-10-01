"""Un mes SIN datos todavía también se guarda (TMT 2026-10-01).

El día 1 /produccion-tejeduria-asinfo tardaba 3 s en CADA visita: Asinfo
contestaba "no hay nada" y, como un [] no se cacheaba, se volvía a preguntar
cuatro veces por visita. Un vacío BUENO se guarda; un fracaso no.
"""
from __future__ import annotations

from unittest.mock import patch

from modules._lib import metabase_client as mc
from modules.asinfo import service


def _reset():
    service._INGRESO_DIA_CACHE.clear()
    service._PROD_TEJ_CACHE.clear()


def test_vacio_bueno_no_se_vuelve_a_preguntar():
    _reset()
    with patch.object(mc, "fetch_dataset_estado", return_value=([], True)) as q:
        service.ingreso_bodega_por_dia(52, "2026-10-01")
        service.ingreso_bodega_por_dia(52, "2026-10-01")
        service.produccion_tejeduria_mes(2026, 10)
        service.produccion_tejeduria_mes(2026, 10)
    assert q.call_count == 2


def test_si_asinfo_no_contesta_se_vuelve_a_preguntar():
    _reset()
    with patch.object(mc, "fetch_dataset_estado", return_value=([], False)) as q:
        service.ingreso_bodega_por_dia(52, "2026-10-01")
        service.ingreso_bodega_por_dia(52, "2026-10-01")
        service.produccion_tejeduria_mes(2026, 10)
        service.produccion_tejeduria_mes(2026, 10)
    assert q.call_count == 4


def test_produccion_terminado_por_dia_vacio_tambien_se_guarda():
    """La /informes/dia preguntaba dos veces por visita el día 1: la producción
    y el despacho de terminado por día venían vacíos y no se guardaban."""
    service._FABRICACION_DIA_CACHE.clear()
    service._DESPACHO_PERIODO_CACHE.clear()
    service._MOVIMIENTO_PERIODO_CACHE.clear()
    with patch.object(mc, "fetch_dataset_estado", return_value=([], True)) as q:
        for _ in range(2):
            service.fabricacion_flujo_por_dia(53, 2026, 10)
    assert q.call_count == 1
    service._FABRICACION_DIA_CACHE.clear()
    with patch.object(mc, "fetch_dataset_estado", return_value=([], False)) as q:
        for _ in range(2):
            service.fabricacion_flujo_por_dia(53, 2026, 10)
    assert q.call_count == 2
