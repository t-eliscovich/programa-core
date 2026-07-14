"""Tests de despacho_fisico_mes y movimiento_bodega_mes (modules/asinfo/service).

Ambas son fuentes FÍSICAS del flujo de terminado (dueña 2026-07-10):
    - despacho_fisico_mes  → kg despachados a cliente (detalle_despacho_cliente),
      coincide con FACTURA+NTEN. Referencia de venta.
    - movimiento_bodega_mes → ingreso/egreso reales de la bodega (deltas del
      saldo por lote), MISMA fuente que el stock → la columna cierra exacto.

Sin HTTP real: se mockea metabase_client.fetch_dataset. Se validan (a) el
parseo/agregación del resultado, (b) el fail-soft, y (c) que el SQL apunte a
las tablas/filtros correctos (bodega, rango de fecha, corte exclusivo).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules._lib import metabase_client
from modules.asinfo import service


@pytest.fixture(autouse=True)
def _limpiar_cache_flujo():
    """Ambas funciones cachean por (bodega, mes/corte) con TTL 600s. Sin
    limpiar entre tests, un resultado mockeado se filtraría al test siguiente
    que usa la misma key con otro mock."""
    service.reset_flujo_caches()
    yield
    service.reset_flujo_caches()


# ---------------------------------------------------------------------------
# despacho_fisico_mes
# ---------------------------------------------------------------------------


def test_despacho_fisico_mes_suma_kg():
    with patch.object(
        metabase_client, "fetch_dataset", return_value=[{"kg": 107026.13}]
    ) as m:
        out = service.despacho_fisico_mes(2026, 7)
    assert out == 107026.13
    # db 2 (Asinfo) y el SQL toca las tablas de despacho, bodega 53 por default
    args, kwargs = m.call_args
    assert args[0] == 2
    sql = args[1]
    assert "despacho_cliente" in sql
    assert "detalle_despacho_cliente" in sql
    assert "id_bodega = 53" in sql
    assert "'2026-07-01'" in sql and "'2026-08-01'" in sql
    assert "fecha_anulacion IS NULL" in sql


def test_despacho_fisico_mes_diciembre_cruza_anio():
    with patch.object(
        metabase_client, "fetch_dataset", return_value=[{"kg": 10.0}]
    ) as m:
        service.despacho_fisico_mes(2026, 12)
    sql = m.call_args[0][1]
    assert "'2026-12-01'" in sql and "'2027-01-01'" in sql


def test_despacho_fisico_mes_bodega_parametrizable():
    with patch.object(
        metabase_client, "fetch_dataset", return_value=[{"kg": 1.0}]
    ) as m:
        service.despacho_fisico_mes(2026, 7, id_bodega=52)
    assert "id_bodega = 52" in m.call_args[0][1]


def test_despacho_fisico_mes_vacio_es_cero():
    with patch.object(metabase_client, "fetch_dataset", return_value=[]):
        assert service.despacho_fisico_mes(2026, 7) == 0.0
    with patch.object(metabase_client, "fetch_dataset", return_value=[{"kg": None}]):
        assert service.despacho_fisico_mes(2026, 7) == 0.0


def test_despacho_fisico_mes_mes_invalido_no_llama():
    with patch.object(metabase_client, "fetch_dataset") as m:
        assert service.despacho_fisico_mes("x", None) == 0.0
    m.assert_not_called()


def test_despacho_fisico_mes_error_es_cero():
    with patch.object(metabase_client, "fetch_dataset", side_effect=RuntimeError("boom")):
        assert service.despacho_fisico_mes(2026, 7) == 0.0


# ---------------------------------------------------------------------------
# movimiento_bodega_mes
# ---------------------------------------------------------------------------


def test_movimiento_bodega_mes_parsea_ingreso_egreso():
    from datetime import date

    with patch.object(
        metabase_client,
        "fetch_dataset",
        return_value=[{"ingreso": 89281.54, "egreso": 100019.13}],
    ) as m:
        out = service.movimiento_bodega_mes(53, date(2026, 7, 1))
    assert out == {"ingreso": 89281.54, "egreso": 100019.13}
    args, _ = m.call_args
    assert args[0] == 2
    sql = args[1]
    assert "saldo_producto_lote" in sql
    assert "id_bodega = 53" in sql
    # corte EXCLUSIVO (fecha > corte) para telescopar contra el stock inicial
    assert "fecha > '2026-07-01'" in sql
    assert "LAG(saldo)" in sql


def test_movimiento_bodega_mes_acepta_string_corte():
    with patch.object(
        metabase_client, "fetch_dataset", return_value=[{"ingreso": 1.0, "egreso": 2.0}]
    ) as m:
        out = service.movimiento_bodega_mes(53, "2026-07-01")
    assert out == {"ingreso": 1.0, "egreso": 2.0}
    assert "fecha > '2026-07-01'" in m.call_args[0][1]


def test_movimiento_bodega_mes_corte_invalido_no_llama():
    with patch.object(metabase_client, "fetch_dataset") as m:
        assert service.movimiento_bodega_mes(53, "no-es-fecha") == {
            "ingreso": 0.0,
            "egreso": 0.0,
        }
    m.assert_not_called()


def test_movimiento_bodega_mes_vacio_es_cero():
    with patch.object(metabase_client, "fetch_dataset", return_value=[{}]):
        assert service.movimiento_bodega_mes(53, "2026-07-01") == {
            "ingreso": 0.0,
            "egreso": 0.0,
        }


def test_movimiento_bodega_mes_error_es_cero():
    with patch.object(metabase_client, "fetch_dataset", side_effect=RuntimeError("x")):
        assert service.movimiento_bodega_mes(53, "2026-07-01") == {
            "ingreso": 0.0,
            "egreso": 0.0,
        }


# ---------------------------------------------------------------------------
# cache (perf): las 3 funciones de flujo cachean 600s por su key y se
# reusan sin volver a pegarle a Metabase — reduce ~6 llamadas por render.
# ---------------------------------------------------------------------------


def test_despacho_fisico_mes_cachea_segunda_llamada():
    with patch.object(
        metabase_client, "fetch_dataset", return_value=[{"kg": 5.0}]
    ) as m:
        a = service.despacho_fisico_mes(2026, 7)
        b = service.despacho_fisico_mes(2026, 7)  # cache hit → sin fetch
    assert a == b == 5.0
    assert m.call_count == 1


def test_despacho_fisico_mes_vacio_no_cachea():
    # [] = probable error de red → no congelar 0.0 por 10 min.
    with patch.object(metabase_client, "fetch_dataset", return_value=[]) as m:
        service.despacho_fisico_mes(2026, 7)
        service.despacho_fisico_mes(2026, 7)
    assert m.call_count == 2


def test_movimiento_bodega_mes_cachea_por_bodega_y_corte():
    with patch.object(
        metabase_client, "fetch_dataset", return_value=[{"ingreso": 1.0, "egreso": 2.0}]
    ) as m:
        service.movimiento_bodega_mes(53, "2026-07-01")  # miss
        service.movimiento_bodega_mes(53, "2026-07-01")  # hit
        service.movimiento_bodega_mes(52, "2026-07-01")  # otra bodega → miss
    assert m.call_count == 2


def test_fabricacion_flujo_mes_cachea():
    with patch.object(
        metabase_client, "fetch_dataset", return_value=[{"issued": 10.0, "fab": 8.0}]
    ) as m:
        service.fabricacion_flujo_mes(52, 2026, 7)
        service.fabricacion_flujo_mes(52, 2026, 7)  # hit
    assert m.call_count == 1


def test_reset_flujo_caches_fuerza_refetch():
    with patch.object(
        metabase_client, "fetch_dataset", return_value=[{"kg": 5.0}]
    ) as m:
        service.despacho_fisico_mes(2026, 7)
        service.reset_flujo_caches()
        service.despacho_fisico_mes(2026, 7)  # cache vaciado → refetch
    assert m.call_count == 2
