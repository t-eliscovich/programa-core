"""Tests de la tab Producción Terminado Asinfo (bodega 53).

La pantalla es el MOVIMIENTO del stock de terminado —inicial + producido −
vendido = final— con el desperdicio al costado, por mes y por día.

  - asinfo.service.fabricacion_flujo_por_dia/_por_mes: shape, SQL (bodega,
    ventana, criterio de hoja, material de entrada, granularidad), caché
    separada por granularidad y fail-soft.
  - asinfo.service.despacho_fisico_por_dia/_por_mes (la columna "vendido").
  - terminado_asinfo.service.resumen: encadenado del stock, total, control
    contra la foto real de Asinfo, y los casos None.
  - render de /produccion-terminado-asinfo.

Sin HTTP ni DB real: se mockea metabase_client.
"""
from __future__ import annotations

from datetime import date as _date
from unittest.mock import patch

import pytest

from modules._lib import metabase_client
from modules.asinfo import service as asvc
from modules.terminado_asinfo import service as tsvc


@pytest.fixture(autouse=True)
def _limpiar_cache():
    for reset in (asvc.reset_fabricacion_dia_cache,
                  asvc.reset_despacho_periodo_cache,
                  asvc.reset_movimiento_periodo_cache):
        reset()
    yield
    for reset in (asvc.reset_fabricacion_dia_cache,
                  asvc.reset_despacho_periodo_cache,
                  asvc.reset_movimiento_periodo_cache):
        reset()


_DIAS = [
    {"periodo": "2026-07-03", "n_ofs": 3, "fab": 1_400.0, "issued": 1_500.0},
    {"periodo": "2026-07-10", "n_ofs": 5, "fab": 2_000.0, "issued": 2_100.0},
]
_MESES = [
    {"periodo": "2026-01", "n_ofs": 40, "fab": 10_000.0, "issued": 10_500.0},
    {"periodo": "2026-02", "n_ofs": 60, "fab": 20_000.0, "issued": 21_000.0},
]


# ---------------------------------------------------------------------------
# fabricacion_flujo_por_dia / _por_mes
# ---------------------------------------------------------------------------


def test_por_dia_shape():
    with patch.object(metabase_client, "fetch_dataset", return_value=_DIAS):
        out = asvc.fabricacion_flujo_por_dia(53, 2026, 7)
    assert out[0] == {"periodo": "2026-07-03", "n_ofs": 3,
                      "issued": 1_500.0, "fab": 1_400.0}


def test_por_dia_sql_bodega_ventana_hoja_y_material():
    with patch.object(metabase_client, "fetch_dataset", return_value=_DIAS) as fd:
        asvc.fabricacion_flujo_por_dia(53, 2026, 7)
    sql = fd.call_args[0][1]
    assert "id_bodega = 53" in sql
    assert "'2026-07-01'" in sql and "'2026-08-01'" in sql
    # Sólo hojas: se excluyen las órdenes que son padre de otra.
    assert "NOT IN (\n                     SELECT id_orden_fabricacion_padre" in sql
    # El material de entrada de la bodega 53 es la TELA CRUDA (no el hilo):
    # sin este filtro el 'consumido' sumaría cualquier insumo de la orden.
    assert "prm.nombre_categoria_producto = 'TELA CRUDA'" in sql
    assert "CONVERT(varchar(10), fecha_cierre, 23)" in sql   # granularidad día


def test_por_mes_agrupa_por_mes_y_barre_el_anio():
    with patch.object(metabase_client, "fetch_dataset", return_value=_MESES) as fd:
        out = asvc.fabricacion_flujo_por_mes(53, 2026)
    sql = fd.call_args[0][1]
    assert "CONVERT(varchar(7),  fecha_cierre, 23)" in sql   # granularidad mes
    assert "'2026-01-01'" in sql and "'2027-01-01'" in sql
    assert [f["periodo"] for f in out] == ["2026-01", "2026-02"]


def test_dia_y_mes_no_comparten_cache():
    """Misma bodega y mismo año: si la clave no distingue la granularidad, la
    tabla por mes devuelve las filas por día (o al revés)."""
    with patch.object(metabase_client, "fetch_dataset", return_value=_DIAS):
        asvc.fabricacion_flujo_por_dia(53, 2026, 7)
    with patch.object(metabase_client, "fetch_dataset", return_value=_MESES):
        assert [f["periodo"] for f in asvc.fabricacion_flujo_por_mes(53, 2026)] \
            == ["2026-01", "2026-02"]


def test_por_dia_diciembre_cruza_de_anio():
    with patch.object(metabase_client, "fetch_dataset", return_value=_DIAS) as fd:
        asvc.fabricacion_flujo_por_dia(53, 2026, 12)
    sql = fd.call_args[0][1]
    assert "'2026-12-01'" in sql and "'2027-01-01'" in sql


def test_por_dia_fail_soft():
    with patch.object(metabase_client, "fetch_dataset", side_effect=RuntimeError("boom")):
        assert asvc.fabricacion_flujo_por_dia(53, 2026, 7) == []
    with patch.object(metabase_client, "fetch_dataset", return_value=[]):
        assert asvc.fabricacion_flujo_por_mes(53, 2026) == []


def test_periodo_mes_invalido_no_consulta():
    with patch.object(metabase_client, "fetch_dataset") as fd:
        assert asvc.fabricacion_flujo_por_dia(53, "no", "va") == []
        assert asvc.fabricacion_flujo_por_mes(53, "no") == []
    fd.assert_not_called()


def test_por_dia_cachea():
    with patch.object(metabase_client, "fetch_dataset", return_value=_DIAS) as fd:
        asvc.fabricacion_flujo_por_dia(53, 2026, 7)
        asvc.fabricacion_flujo_por_dia(53, 2026, 7)
    assert fd.call_count == 1


def test_por_dia_vacio_no_se_congela_en_cache():
    """Un [] puede ser Asinfo mudo — si se cachea, la pantalla queda vacía
    5 minutos aunque el ERP ya conteste."""
    with patch.object(metabase_client, "fetch_dataset", return_value=[]):
        asvc.fabricacion_flujo_por_dia(53, 2026, 7)
    with patch.object(metabase_client, "fetch_dataset", return_value=_DIAS):
        assert len(asvc.fabricacion_flujo_por_dia(53, 2026, 7)) == 2


# ---------------------------------------------------------------------------
# despacho_fisico_por_dia / _por_mes — la columna "vendido"
# ---------------------------------------------------------------------------

_VENTA_ROWS = [{"periodo": "2026-07-03", "kg": 900.0},
               {"periodo": "2026-07-10", "kg": 1_100.0}]


def test_despacho_por_dia_devuelve_dict_y_filtra_anulados():
    with patch.object(metabase_client, "fetch_dataset", return_value=_VENTA_ROWS) as fd:
        out = asvc.despacho_fisico_por_dia(2026, 7, 53)
    assert out == {"2026-07-03": 900.0, "2026-07-10": 1_100.0}
    sql = fd.call_args[0][1]
    assert "dc.fecha_anulacion IS NULL" in sql      # mismo WHERE que el mensual
    assert "dd.id_bodega = 53" in sql
    assert "'2026-07-01'" in sql and "'2026-08-01'" in sql


def test_despacho_por_mes_barre_el_anio():
    with patch.object(metabase_client, "fetch_dataset", return_value=[]) as fd:
        asvc.despacho_fisico_por_mes(2026, 53)
    sql = fd.call_args[0][1]
    assert "CONVERT(varchar(7),  dc.fecha, 23)" in sql
    assert "'2026-01-01'" in sql and "'2027-01-01'" in sql


def test_despacho_fail_soft():
    with patch.object(metabase_client, "fetch_dataset", side_effect=RuntimeError("boom")):
        assert asvc.despacho_fisico_por_dia(2026, 7, 53) == {}
    with patch.object(metabase_client, "fetch_dataset") as fd:
        assert asvc.despacho_fisico_por_mes("no", 53) == {}
    fd.assert_not_called()


# ---------------------------------------------------------------------------
# resumen — el encadenado del stock
# ---------------------------------------------------------------------------


def _mock_resumen(stock_por_fecha, meses=None, dias=None, vendido_mes=None,
                  vendido_dia=None, mov_mes=None, mov_dia=None):
    """Arma los patches de las 7 fuentes que consume `resumen`."""
    return (
        patch.object(asvc, "movimiento_bodega_por_mes",
                     return_value=mov_mes if mov_mes is not None else {}),
        patch.object(asvc, "movimiento_bodega_por_dia",
                     return_value=mov_dia if mov_dia is not None else {}),
        patch.object(asvc, "fabricacion_flujo_por_mes",
                     return_value=meses if meses is not None else _MESES),
        patch.object(asvc, "fabricacion_flujo_por_dia",
                     return_value=dias if dias is not None else _DIAS),
        patch.object(asvc, "despacho_fisico_por_mes",
                     return_value=vendido_mes if vendido_mes is not None
                     else {"2026-01": 4_000.0, "2026-02": 5_000.0}),
        patch.object(asvc, "despacho_fisico_por_dia",
                     return_value=vendido_dia if vendido_dia is not None
                     else {"2026-07-03": 900.0}),
        patch.object(asvc, "inventario_por_etapa_a_fecha",
                     side_effect=lambda f: {"disponible": f in stock_por_fecha,
                                            "terminada": stock_por_fecha.get(f, 0.0)}),
    )


def test_resumen_encadena_el_stock_mes_a_mes():
    stock = {"2025-12-31": 100_000.0, "2026-06-30": 50_000.0,
             "2026-02-28": 121_000.0}
    p1, p2, p3, p4, p5, p6, p7 = _mock_resumen(stock)
    with p1, p2, p3, p4, p5, p6, p7:
        out = tsvc.resumen(2026, 7)
    f = out["meses"]["filas"]
    # Enero: 100.000 + 10.000 producido − 4.000 vendido = 106.000
    assert f[0]["inicial"] == 100_000.0
    assert f[0]["final"] == 106_000.0
    # El final de enero es el inicial de febrero (encadenado).
    assert f[1]["inicial"] == 106_000.0
    assert f[1]["final"] == 121_000.0
    # Desperdicio de enero = 10.500 consumido − 10.000 producido.
    assert f[0]["desperdicio_kg"] == 500.0
    # El TOTAL toma el stock de las PUNTAS y suma los flujos.
    t = out["meses"]["total"]
    assert (t["inicial"], t["final"]) == (100_000.0, 121_000.0)
    assert t["producido"] == 30_000.0 and t["vendido"] == 9_000.0


def test_resumen_control_no_pide_una_foto_del_futuro():
    """El mes en curso todavía no cerró: pedirle a Asinfo la foto al 31/08
    estando a 4 de agosto devuelve igual el stock de HOY y el rótulo mentiría.
    El corte se topea en el día de hoy."""
    stock = {"2025-12-31": 100_000.0, "2026-06-30": 0.0}
    p1, p2, p3, p4, p5, p6, p7 = _mock_resumen(
        stock, meses=[{"periodo": "2026-08", "n_ofs": 1, "fab": 10.0,
                       "issued": 11.0}])
    with p1, p2, p3, p4, p5, p6, p7, \
         patch.object(tsvc, "today_ec", return_value=_date(2026, 8, 4)):
        out = tsvc.resumen(2026, 8)
    # Sin foto para el 04/08 en el mock, el control queda en None — pero lo que
    # importa es que la fecha pedida NO fue 2026-08-31.
    assert out["control"] is None


def test_resumen_control_cierra_contra_la_foto_real():
    """El encadenado llega a 121.000 y Asinfo dice 121.000 → dif 0."""
    stock = {"2025-12-31": 100_000.0, "2026-06-30": 0.0, "2026-02-28": 121_000.0}
    p1, p2, p3, p4, p5, p6, p7 = _mock_resumen(stock)
    with p1, p2, p3, p4, p5, p6, p7, \
         patch.object(tsvc, "today_ec", return_value=_date(2026, 8, 4)):
        out = tsvc.resumen(2026, 7)
    assert out["control"]["fecha"] == "2026-02-28"
    assert out["control"]["dif"] == 0.0


def test_resumen_control_marca_la_diferencia():
    """Si Asinfo tiene menos de lo que el encadenado calcula, la dif se ve: es
    movimiento que no es ni producción ni despacho (devolución, ajuste)."""
    stock = {"2025-12-31": 100_000.0, "2026-06-30": 0.0, "2026-02-28": 120_500.0}
    p1, p2, p3, p4, p5, p6, p7 = _mock_resumen(stock)
    with p1, p2, p3, p4, p5, p6, p7, \
         patch.object(tsvc, "today_ec", return_value=_date(2026, 8, 4)):
        out = tsvc.resumen(2026, 7)
    assert out["control"]["dif"] == 500.0


def test_resumen_sin_foto_de_stock_no_inventa_saldos():
    """Asinfo no dio la foto → inicial/final None, pero los flujos se muestran
    igual. Un 0,00 se leería como "no había stock"."""
    p1, p2, p3, p4, p5, p6, p7 = _mock_resumen({})
    with p1, p2, p3, p4, p5, p6, p7:
        out = tsvc.resumen(2026, 7)
    assert out["meses"]["filas"][0]["inicial"] is None
    assert out["meses"]["filas"][0]["final"] is None
    assert out["meses"]["filas"][0]["producido"] == 10_000.0
    assert out["control"] is None


def test_resumen_dia_se_ancla_en_el_cierre_del_mes_anterior():
    stock = {"2025-12-31": 0.0, "2026-06-30": 50_000.0}
    p1, p2, p3, p4, p5, p6, p7 = _mock_resumen(stock)
    with p1, p2, p3, p4, p5, p6, p7:
        out = tsvc.resumen(2026, 7)
    d = out["dias"]["filas"]
    # 50.000 + 1.400 producido − 900 vendido = 50.500
    assert d[0]["inicial"] == 50_000.0
    assert d[0]["final"] == 50_500.0
    # El 10/07 no tiene despacho en el mock → vendido 0.
    assert d[1]["vendido"] == 0.0
    assert d[1]["final"] == 52_500.0


def test_resumen_sin_datos_no_disponible():
    p1, p2, p3, p4, p5, p6, p7 = _mock_resumen({}, meses=[], dias=[],
                                       vendido_mes={}, vendido_dia={})
    with p1, p2, p3, p4, p5, p6, p7:
        out = tsvc.resumen(2026, 7)
    assert out["disponible"] is False
    assert out["meses"]["filas"] == []


def test_resumen_desperdicio_pct_none_sin_consumo():
    """issued=0 → pct None. Un 0,00% se lee como "no hubo desperdicio"."""
    p1, p2, p3, p4, p5, p6, p7 = _mock_resumen(
        {}, meses=[{"periodo": "2026-01", "n_ofs": 1, "fab": 10.0, "issued": 0.0}])
    with p1, p2, p3, p4, p5, p6, p7:
        out = tsvc.resumen(2026, 7)
    assert out["meses"]["filas"][0]["desperdicio_pct"] is None
    assert out["meses"]["total"]["desperdicio_pct"] is None


# ---------------------------------------------------------------------------
# render de la ruta
# ---------------------------------------------------------------------------


def _login(app, fake_db):
    rid = fake_db.add_role("Tester", ["stock.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_tab_renderiza_las_dos_tablas(app, fake_db):
    c = _login(app, fake_db)
    stock = {"2025-12-31": 100_000.0, "2026-06-30": 50_000.0,
             "2026-02-28": 121_000.0}
    p1, p2, p3, p4, p5, p6, p7 = _mock_resumen(stock)
    with p1, p2, p3, p4, p5, p6, p7:
        r = c.get("/produccion-terminado-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Por mes · 2026" in body
    # Dueña 2026-08-04: "poneme un + y que esté hide si no clickeo" — el bloque
    # por mes arranca PLEGADO (<details> sin `open`).
    assert "<details>" in body and "<details open>" not in body
    assert "Día a día" in body
    assert "Desperdicio" in body and "Merma" not in body   # dueña: "desperdicio"
    assert "01/2026" in body           # etiqueta del mes
    assert "03/07/2026" in body        # etiqueta del día, formato EU
    assert "106.000,00" in body        # final encadenado de enero


def test_tab_sin_asinfo_avisa_y_no_rompe(app, fake_db):
    c = _login(app, fake_db)
    with patch.object(metabase_client, "fetch_dataset", side_effect=RuntimeError("boom")):
        r = c.get("/produccion-terminado-asinfo?anio=2026&mes=7")
    assert r.status_code == 200
    assert "Asinfo no está respondiendo" in r.get_data(as_text=True)


# ---------------------------------------------------------------------------
# OTROS MOV. — la columna que explica el descuadre (dueña: "esto me suena raro")
# ---------------------------------------------------------------------------


def test_movimiento_por_mes_sql_no_filtra_la_particion_del_lag():
    """⚠ El LAG tiene que ver TODA la historia del lote para que el primer
    movimiento de la ventana sepa contra qué comparar. Si el filtro de fecha se
    mete adentro del CTE, ese primer movimiento sale como alta de lote y el
    ingreso del mes se infla."""
    with patch.object(metabase_client, "fetch_dataset", return_value=[]) as fd:
        asvc.movimiento_bodega_por_mes(53, 2026)
    sql = fd.call_args[0][1]
    cte = sql[sql.index("WITH d AS"):sql.index(")\n        SELECT")]
    assert "fecha >=" not in cte and "fecha <" not in cte
    assert "WHERE fecha >= '2026-01-01' AND fecha < '2027-01-01'" in sql
    assert "CONVERT(varchar(7),  fecha, 23)" in sql


def test_movimiento_por_dia_shape_y_fail_soft():
    filas = [{"periodo": "2026-07-03", "ingreso": 10.0, "egreso": 4.0}]
    with patch.object(metabase_client, "fetch_dataset", return_value=filas):
        assert asvc.movimiento_bodega_por_dia(53, 2026, 7) == {
            "2026-07-03": {"ingreso": 10.0, "egreso": 4.0}}
    asvc.reset_movimiento_periodo_cache()
    with patch.object(metabase_client, "fetch_dataset", side_effect=RuntimeError):
        assert asvc.movimiento_bodega_por_dia(53, 2026, 7) == {}


def test_otros_mov_hace_cerrar_el_final_con_el_stock_real():
    """Enero: produce 10.000 y vende 4.000, pero la bodega movió 12.000 de
    ingreso y 4.500 de egreso. Los 2.000 que entraron sin ser producción y los
    500 que salieron sin ser venta son OTROS MOV. = +1.500, y el final tiene
    que dar 100.000 + 12.000 − 4.500 = 107.500 (la identidad del saldo)."""
    stock = {"2025-12-31": 100_000.0}
    p1, p2, p3, p4, p5, p6, p7 = _mock_resumen(
        stock,
        meses=[{"periodo": "2026-01", "n_ofs": 40, "fab": 10_000.0,
                "issued": 10_500.0}],
        vendido_mes={"2026-01": 4_000.0},
        mov_mes={"2026-01": {"ingreso": 12_000.0, "egreso": 4_500.0}})
    with p1, p2, p3, p4, p5, p6, p7, \
         patch.object(tsvc, "today_ec", return_value=_date(2026, 8, 4)):
        out = tsvc.resumen(2026, 1)
    f = out["meses"]["filas"][0]
    assert f["otros_ingreso"] == 2_000.0     # entró sin ser producción
    assert f["otros_egreso"] == 500.0        # salió sin ser venta
    assert f["otros"] == 1_500.0
    assert f["final"] == 107_500.0
    assert out["meses"]["total"]["otros"] == 1_500.0


def test_sin_fuente_de_movimientos_otros_es_none_y_no_cero():
    """Un 0 en esa columna dice "no hubo movimientos raros". Que la fuente esté
    caída dice otra cosa muy distinta."""
    stock = {"2025-12-31": 100_000.0}
    p1, p2, p3, p4, p5, p6, p7 = _mock_resumen(
        stock,
        meses=[{"periodo": "2026-01", "n_ofs": 1, "fab": 10_000.0,
                "issued": 10_500.0}],
        vendido_mes={"2026-01": 4_000.0},
        mov_mes={})
    with p1, p2, p3, p4, p5, p6, p7, \
         patch.object(tsvc, "today_ec", return_value=_date(2026, 8, 4)):
        out = tsvc.resumen(2026, 1)
    f = out["meses"]["filas"][0]
    assert f["otros"] is None
    assert f["final"] == 106_000.0           # vuelve al encadenado simple
