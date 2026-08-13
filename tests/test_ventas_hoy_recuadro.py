"""El recuadro "Hoy" de la pantalla de inicio: facturado + despachado.

TMT 2026-08-13 (dueña): *"cómo podríamos ir teniendo cantidad kg vendidos en el
día y que se vaya acumulando"* → el número a la vista en `/operaciones`, sin
depender de que llegue el aviso de la campanita. Y enseguida: *"podemos poner
otro número que sea el despachado, a veces se despacha más que facturado"*.

Lo que estos tests protegen:

· El despachado sale de Asinfo, así que **nunca puede tumbar la pantalla**: si
  el ERP no contesta se muestra la ÚLTIMA lectura buena con su hora, y si no
  hubo ninguna, un guión — jamás un cero, que se leería como "no salió nada"
  (decisión de la dueña, 13/08).
· ⭐ Un fracaso de Metabase **no se guarda en el caché**. Es la lección del
  29/07 (cachear el error dejó la utilidad rota por horas).
"""
from __future__ import annotations

import calendar
from datetime import date
from unittest.mock import patch

import pytest

from modules._lib import metabase_client
from modules.asinfo import service
from modules.facturas import views as fviews

DIA = date(2026, 8, 13)
CLAVE = (DIA.isoformat(), 53)


@pytest.fixture(autouse=True)
def _limpiar_cache():
    service.reset_flujo_caches()
    yield
    service.reset_flujo_caches()


def _envejecer(segundos: int = 10_000) -> float:
    """Correr para atrás el reloj de la entrada cacheada (vence el TTL)."""
    ts, kg = service._DESPACHO_HOY_CACHE[CLAVE]
    service._DESPACHO_HOY_CACHE[CLAVE] = (ts - segundos, kg)
    return ts - segundos


# ── despacho_fisico_dia_info ────────────────────────────────────────────────

def test_devuelve_los_kg_del_dia_y_los_marca_frescos():
    with patch.object(metabase_client, "fetch_dataset",
                      return_value=[{"kg": 11890.75}]) as m:
        info = service.despacho_fisico_dia_info(DIA)
    assert info["kg"] == 11890.75 and info["fresco"] is True
    sql = m.call_args[0][1]
    # El día va como literal (el GETDATE() de Asinfo está en UTC y correría el
    # día 5 horas), y el WHERE es el mismo que el del mes.
    assert "'2026-08-13'" in sql
    assert "fecha_anulacion IS NULL" in sql
    assert "id_bodega = 53" in sql


def test_la_segunda_lectura_seguida_sale_del_cache():
    with patch.object(metabase_client, "fetch_dataset",
                      return_value=[{"kg": 100.0}]) as m:
        service.despacho_fisico_dia_info(DIA)
        service.despacho_fisico_dia_info(DIA)
    assert m.call_count == 1


def test_si_asinfo_no_contesta_devuelve_la_ULTIMA_lectura_buena():
    """Y avisa que no es fresca, para que la pantalla ponga la hora."""
    with patch.object(metabase_client, "fetch_dataset",
                      return_value=[{"kg": 11890.75}]):
        service.despacho_fisico_dia_info(DIA)
    viejo = _envejecer()
    with patch.object(metabase_client, "fetch_dataset",
                      side_effect=RuntimeError("metabase 500")):
        info = service.despacho_fisico_dia_info(DIA)
    assert info["kg"] == 11890.75
    assert info["fresco"] is False
    assert info["medido_ts"] == viejo


def test_un_fracaso_NO_se_guarda_como_valor():
    """Lección del 29/07: cachear el fracaso deja el número roto por horas."""
    with patch.object(metabase_client, "fetch_dataset",
                      side_effect=RuntimeError("metabase 500")):
        info = service.despacho_fisico_dia_info(DIA)
    assert info["kg"] is None
    assert CLAVE not in service._DESPACHO_HOY_CACHE
    # …y la lectura siguiente, con Asinfo de vuelta, trae el número de verdad.
    with patch.object(metabase_client, "fetch_dataset",
                      return_value=[{"kg": 500.0}]):
        assert service.despacho_fisico_dia_info(DIA)["kg"] == 500.0


def test_una_respuesta_vacia_tampoco_pisa_el_ultimo_bueno():
    with patch.object(metabase_client, "fetch_dataset",
                      return_value=[{"kg": 300.0}]):
        service.despacho_fisico_dia_info(DIA)
    _envejecer()
    with patch.object(metabase_client, "fetch_dataset", return_value=[]):
        info = service.despacho_fisico_dia_info(DIA)
    assert info["kg"] == 300.0 and info["fresco"] is False


def test_despacho_fisico_dia_devuelve_solo_el_numero():
    with patch.object(metabase_client, "fetch_dataset",
                      return_value=[{"kg": 11890.75}]):
        assert service.despacho_fisico_dia(DIA) == 11890.75
    service.reset_flujo_caches()
    with patch.object(metabase_client, "fetch_dataset",
                      side_effect=RuntimeError("boom")):
        assert service.despacho_fisico_dia(DIA) == 0.0


# ── El envoltorio de la vista ───────────────────────────────────────────────

def test_la_vista_traduce_el_ts_a_hora_de_ECUADOR():
    """El server corre en UTC; la hora que ve la dueña es la de Ecuador."""
    ts = calendar.timegm((2026, 8, 13, 16, 24, 0, 0, 0, 0))  # 16:24 UTC
    with patch.object(service, "despacho_fisico_dia_info",
                      return_value={"kg": 11890.75, "medido_ts": ts,
                                    "fresco": False}):
        out = fviews._despachado_hoy(DIA)
    assert out == {"kg": 11890.75, "hora": "11:24", "fresco": False}


def test_si_nunca_hubo_lectura_la_vista_no_inventa_un_cero():
    with patch.object(service, "despacho_fisico_dia_info",
                      return_value={"kg": None, "medido_ts": None,
                                    "fresco": False}):
        out = fviews._despachado_hoy(DIA)
    assert out["kg"] is None and out["hora"] is None


def test_la_vista_es_fail_soft_si_el_modulo_de_asinfo_explota():
    with patch.object(service, "despacho_fisico_dia_info",
                      side_effect=RuntimeError("boom")):
        assert fviews._despachado_hoy(DIA)["kg"] is None
