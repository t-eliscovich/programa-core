"""Tests de la tab Producción Terminado Asinfo (bodega 53, día por día).

  - asinfo.service.fabricacion_flujo_por_dia: shape, SQL (bodega, ventana de
    cierre, criterio de hoja, material de entrada), caché y fail-soft.
  - terminado_asinfo.service.resumen_mes: merma por día, total = suma de los
    días, y el caso "sin consumo" (merma_pct None, no 0).
  - render de /produccion-terminado-asinfo.

Sin HTTP ni DB real: se mockea metabase_client.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from modules._lib import metabase_client
from modules.asinfo import service as asvc
from modules.terminado_asinfo import service as tsvc


@pytest.fixture(autouse=True)
def _limpiar_cache():
    asvc.reset_fabricacion_dia_cache()
    yield
    asvc.reset_fabricacion_dia_cache()


_ROWS = [
    {"dia": "2026-07-03", "n_ofs": 3, "fab": 1_400.0, "issued": 1_500.0},
    {"dia": "2026-07-10", "n_ofs": 5, "fab": 2_000.0, "issued": 2_100.0},
]


# ---------------------------------------------------------------------------
# fabricacion_flujo_por_dia
# ---------------------------------------------------------------------------


def test_por_dia_shape():
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS):
        out = asvc.fabricacion_flujo_por_dia(53, 2026, 7)
    assert [d["dia"] for d in out] == ["2026-07-03", "2026-07-10"]
    assert out[0] == {"dia": "2026-07-03", "n_ofs": 3,
                      "issued": 1_500.0, "fab": 1_400.0}


def test_por_dia_sql_bodega_ventana_hoja_y_material():
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS) as fd:
        asvc.fabricacion_flujo_por_dia(53, 2026, 7)
    sql = fd.call_args[0][1]
    assert "id_bodega = 53" in sql
    assert "'2026-07-01'" in sql and "'2026-08-01'" in sql
    # Sólo hojas: se excluyen las órdenes que son padre de otra.
    assert "NOT IN (\n                     SELECT id_orden_fabricacion_padre" in sql
    # El material de entrada de la bodega 53 es la TELA CRUDA (no el hilo):
    # sin este filtro el 'consumido' sumaría cualquier insumo de la orden.
    assert "prm.nombre_categoria_producto = 'TELA CRUDA'" in sql
    assert "GROUP BY o.dia" in sql


def test_por_dia_diciembre_cruza_de_anio():
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS) as fd:
        asvc.fabricacion_flujo_por_dia(53, 2026, 12)
    sql = fd.call_args[0][1]
    assert "'2026-12-01'" in sql and "'2027-01-01'" in sql


def test_por_dia_fail_soft():
    with patch.object(metabase_client, "fetch_dataset", side_effect=RuntimeError("boom")):
        assert asvc.fabricacion_flujo_por_dia(53, 2026, 7) == []
    with patch.object(metabase_client, "fetch_dataset", return_value=[]):
        assert asvc.fabricacion_flujo_por_dia(53, 2026, 7) == []


def test_por_dia_mes_invalido_no_consulta():
    with patch.object(metabase_client, "fetch_dataset") as fd:
        assert asvc.fabricacion_flujo_por_dia(53, "no", "va") == []
    fd.assert_not_called()


def test_por_dia_cachea():
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS) as fd:
        asvc.fabricacion_flujo_por_dia(53, 2026, 7)
        asvc.fabricacion_flujo_por_dia(53, 2026, 7)
    assert fd.call_count == 1


def test_por_dia_vacio_no_se_congela_en_cache():
    """Un [] puede ser Asinfo mudo — si se cachea, la pantalla queda vacía
    5 minutos aunque el ERP ya conteste."""
    with patch.object(metabase_client, "fetch_dataset", return_value=[]):
        asvc.fabricacion_flujo_por_dia(53, 2026, 7)
    with patch.object(metabase_client, "fetch_dataset", return_value=_ROWS):
        assert len(asvc.fabricacion_flujo_por_dia(53, 2026, 7)) == 2


# ---------------------------------------------------------------------------
# resumen_mes — la merma por día y el total
# ---------------------------------------------------------------------------


def test_resumen_mes_merma_por_dia_y_total():
    with patch.object(asvc, "fabricacion_flujo_por_dia", return_value=_ROWS) as fp:
        out = tsvc.resumen_mes(2026, 7)
    fp.assert_called_once_with(53, 2026, 7)
    assert out["disponible"] is True
    d0 = out["dias"][0]
    assert d0["merma_kg"] == 100.0
    assert d0["merma_pct"] == round(100.0 * 100.0 / 1_500.0, 2)  # 6,67 %
    # El total es la SUMA de los días, no una segunda query.
    assert out["total"]["n_ofs"] == 8
    assert out["total"]["consumido"] == 3_600.0
    assert out["total"]["producido"] == 3_400.0
    assert out["total"]["merma_kg"] == 200.0


def test_resumen_mes_sin_consumo_no_inventa_porcentaje():
    """issued=0 → merma_pct None. Un 0,00% se lee como "no hubo merma"."""
    with patch.object(asvc, "fabricacion_flujo_por_dia",
                      return_value=[{"dia": "2026-07-03", "n_ofs": 1,
                                     "fab": 10.0, "issued": 0.0}]):
        out = tsvc.resumen_mes(2026, 7)
    assert out["dias"][0]["merma_pct"] is None
    assert out["total"]["merma_pct"] is None


def test_resumen_mes_sin_datos_no_disponible():
    with patch.object(asvc, "fabricacion_flujo_por_dia", return_value=[]):
        out = tsvc.resumen_mes(2026, 7)
    assert out["disponible"] is False
    assert out["dias"] == []


# ---------------------------------------------------------------------------
# render de la ruta
# ---------------------------------------------------------------------------


def test_tab_renderiza_200(app, fake_db):
    rid = fake_db.add_role("Tester", ["stock.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    with patch.object(asvc, "fabricacion_flujo_por_dia", return_value=_ROWS):
        r = c.get("/produccion-terminado-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Día a día" in body
    assert "03/07/2026" in body        # la fecha del primer día, formato EU
    assert "Merma" in body
    assert "1.500,00" in body          # tela cruda consumida del 03/07


def test_tab_sin_asinfo_avisa_y_no_rompe(app, fake_db):
    rid = fake_db.add_role("Tester", ["stock.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    with patch.object(metabase_client, "fetch_dataset", side_effect=RuntimeError("boom")):
        r = c.get("/produccion-terminado-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    assert "Asinfo no está respondiendo" in r.get_data(as_text=True)
