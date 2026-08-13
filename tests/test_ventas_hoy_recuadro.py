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


# ── resumen_hoy: UNA función para los tres lugares ──────────────────────────

def _resumen(kg_fact, desp, *, n=53, importe=95833.45):
    with patch("modules.facturas.aviso_ventas.totales_dia",
               return_value={"n": n, "importe": importe, "kg": kg_fact}), \
         patch.object(fviews, "_despachado_hoy", return_value=desp):
        return fviews.resumen_hoy(DIA)


def test_resumen_hoy_arma_el_porcentaje_facturado_sobre_despachado():
    out = _resumen(11508.65, {"kg": 11947.0, "hora": None, "fresco": True})
    assert out["kg"] == 11508.65
    assert out["despachado"]["kg"] == 11947.0
    assert out["pct"] == 96
    assert out["fecha"] == "2026-08-13"


def test_el_porcentaje_pasa_de_100_cuando_se_factura_lo_de_ayer():
    assert _resumen(11000.0, {"kg": 10000.0, "hora": None,
                              "fresco": True})["pct"] == 110


def test_sin_dato_de_despacho_no_hay_porcentaje_y_no_explota():
    out = _resumen(11508.65, {"kg": None, "hora": None, "fresco": False})
    assert out["pct"] is None and out["despachado"]["kg"] is None


def test_los_tres_lugares_llaman_a_la_MISMA_funcion():
    """El recuadro del inicio, el pin de la campanita y el JSON que los
    refresca salen de `resumen_hoy`. Si alguno se armara por su cuenta, dos
    pantallas de la misma app dirían cosas distintas el mismo minuto."""
    import inspect
    from pathlib import Path

    import app as app_mod
    from modules.historial import views as hviews

    assert "resumen_hoy" in inspect.getsource(hviews.operaciones)
    assert "resumen_hoy" in inspect.getsource(app_mod.create_app)
    assert "resumen_hoy()" in inspect.getsource(fviews.totales_hoy_json)
    # …y el pin está en la campanita, arriba de la lista de avisos.
    base = Path("templates/base.html").read_text()
    assert "ventas_hoy_pin()" in base
    assert base.index("ventas_hoy_pin()") < base.index("_ab['items']")


# ── Ninguna pantalla le pregunta a Asinfo mientras se dibuja ────────────────

def test_el_modo_render_no_toca_asinfo():
    """El pin va en base.html (TODAS las pantallas): si el render pidiera el
    despacho, un Metabase lento haría esperar a la app entera."""
    with patch("modules.facturas.aviso_ventas.totales_dia",
               return_value={"n": 3, "importe": 10.0, "kg": 5.0}), \
         patch.object(fviews, "_despachado_hoy") as desp:
        out = fviews.resumen_hoy(DIA, con_despacho=False)
    desp.assert_not_called()
    assert out["despachado"]["kg"] is None and out["pct"] is None
    assert out["kg"] == 5.0          # lo local sí sale en el render


def test_el_pin_y_el_recuadro_rinden_en_modo_render_y_completan_por_fetch():
    import inspect

    import app as app_mod
    from modules.historial import views as hviews

    assert "con_despacho=False" in inspect.getsource(hviews.operaciones)
    assert "con_despacho=False" in inspect.getsource(app_mod.create_app)
    # El JSON, en cambio, SÍ trae el despacho (es el que llama el fetch).
    assert "con_despacho" not in inspect.getsource(fviews.totales_hoy_json)


def test_el_despachado_muestra_su_unidad_junto_al_numero():
    """En producción salió "12.357,40" pelado: el "kg" estaba fuera del span
    que escribe el fetch, y el render (que no tiene el dato) no lo pintaba."""
    from pathlib import Path

    html = Path(
        "modules/historial/templates/historial/operaciones.html").read_text()
    assert "data-campo=\"kg_despachado\">{{ (ventas_hoy.despachado.kg | kg_es ~ ' kg')" in html
    assert "fmt(dsp.kg, 2) + ' kg'" in html
