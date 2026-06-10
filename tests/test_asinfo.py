"""Tests del bridge a Asinfo (modules/asinfo/service.py).

Cubre:
    - fetch_card_from_env: env vacía → [], env seteada → llama metabase_client.
    - ventas_vendedor_usd / ventas_vendedor_kg / ventas_cliente_kg:
      cada uno consulta su env var, pasa filtros opcionales.
    - disponible(): combina metabase_client.disponible + al menos una card_id.
    - Sin HTTP real: mockeamos metabase_client.fetch_card.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from modules._lib import metabase_client
from modules.asinfo import service


@pytest.fixture(autouse=True)
def _reset_facturas_cache():
    """El cache TTL de facturas_periodo es global. Limpiarlo entre tests."""
    service.reset_facturas_cache()
    yield
    service.reset_facturas_cache()


# ---------------------------------------------------------------------------
# fetch_card_from_env
# ---------------------------------------------------------------------------


def test_fetch_card_from_env_vacia_devuelve_vacio(monkeypatch):
    monkeypatch.delenv("ASINFO_CARD_VENDEDOR_USD", raising=False)
    with patch.object(metabase_client, "fetch_card") as m:
        result = service.fetch_card_from_env("ASINFO_CARD_VENDEDOR_USD")
    assert result == []
    m.assert_not_called()  # ni siquiera intentamos pegarle a Metabase


def test_fetch_card_from_env_seteada_llama_metabase(monkeypatch):
    monkeypatch.setenv("ASINFO_CARD_VENDEDOR_USD", "116")
    rows = [{"vendedor": "JUAN", "usd": 10000}]
    with patch.object(metabase_client, "fetch_card", return_value=rows) as m:
        result = service.fetch_card_from_env("ASINFO_CARD_VENDEDOR_USD")
    assert result == rows
    m.assert_called_once_with("116", params=None)


def test_fetch_card_from_env_pasa_params(monkeypatch):
    monkeypatch.setenv("ASINFO_CARD_VENDEDOR_USD", "116")
    params = [{"type": "category", "target": ["x"], "value": "v"}]
    with patch.object(metabase_client, "fetch_card", return_value=[]) as m:
        service.fetch_card_from_env("ASINFO_CARD_VENDEDOR_USD", params=params)
    m.assert_called_once_with("116", params=params)


# ---------------------------------------------------------------------------
# wrappers nominales
# ---------------------------------------------------------------------------


def test_ventas_vendedor_usd_sin_filtro(monkeypatch):
    monkeypatch.setenv("ASINFO_CARD_VENDEDOR_USD", "116")
    with patch.object(metabase_client, "fetch_card", return_value=[{"x": 1}]) as m:
        result = service.ventas_vendedor_usd()
    assert result == [{"x": 1}]
    m.assert_called_once_with("116", params=None)


def test_ventas_vendedor_usd_con_filtro_pasa_template_tag(monkeypatch):
    monkeypatch.setenv("ASINFO_CARD_VENDEDOR_USD", "116")
    with patch.object(metabase_client, "fetch_card", return_value=[]) as m:
        service.ventas_vendedor_usd(vendedor="JUAN")
    args, kwargs = m.call_args
    assert args[0] == "116"
    params = kwargs.get("params")
    assert params is not None
    assert params[0]["target"] == ["variable", ["template-tag", "vendedor"]]
    assert params[0]["value"] == "JUAN"


def test_ventas_vendedor_kg_usa_su_card(monkeypatch):
    monkeypatch.setenv("ASINFO_CARD_VENDEDOR_KG", "163")
    with patch.object(metabase_client, "fetch_card", return_value=[]) as m:
        service.ventas_vendedor_kg()
    assert m.call_args[0][0] == "163"


def test_ventas_cliente_kg_usa_su_card(monkeypatch):
    monkeypatch.setenv("ASINFO_CARD_CLIENTE_KG", "164")
    with patch.object(metabase_client, "fetch_card", return_value=[]) as m:
        service.ventas_cliente_kg()
    assert m.call_args[0][0] == "164"


def test_wrappers_env_vacia_no_llama_metabase(monkeypatch):
    """Cada wrapper degrada a [] sin pegarle a Metabase si su env está vacía."""
    for env in (
        "ASINFO_CARD_VENDEDOR_USD",
        "ASINFO_CARD_VENDEDOR_KG",
        "ASINFO_CARD_CLIENTE_KG",
    ):
        monkeypatch.delenv(env, raising=False)
    with patch.object(metabase_client, "fetch_card") as m:
        assert service.ventas_vendedor_usd() == []
        assert service.ventas_vendedor_kg() == []
        assert service.ventas_cliente_kg() == []
    m.assert_not_called()


# ---------------------------------------------------------------------------
# disponible
# ---------------------------------------------------------------------------


def test_disponible_false_si_metabase_no_disponible(monkeypatch):
    monkeypatch.setenv("ASINFO_CARD_VENDEDOR_USD", "116")
    with patch.object(metabase_client, "disponible", return_value=False):
        assert service.disponible() is False


def test_disponible_false_si_ninguna_card_seteada(monkeypatch):
    for env in (
        "ASINFO_CARD_VENDEDOR_USD",
        "ASINFO_CARD_VENDEDOR_KG",
        "ASINFO_CARD_CLIENTE_KG",
    ):
        monkeypatch.delenv(env, raising=False)
    with patch.object(metabase_client, "disponible", return_value=True):
        assert service.disponible() is False


def test_disponible_true_con_metabase_y_al_menos_una_card(monkeypatch):
    for env in (
        "ASINFO_CARD_VENDEDOR_USD",
        "ASINFO_CARD_VENDEDOR_KG",
        "ASINFO_CARD_CLIENTE_KG",
        "ASINFO_CARD_FACTURAS",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("ASINFO_CARD_CLIENTE_KG", "164")
    with patch.object(metabase_client, "disponible", return_value=True):
        assert service.disponible() is True


# ---------------------------------------------------------------------------
# facturas_periodo  /  facturas_totales_por_tipo
# ---------------------------------------------------------------------------


def test_facturas_periodo_pasa_rango_como_template_tags(monkeypatch):
    from datetime import date

    monkeypatch.setenv("ASINFO_CARD_FACTURAS", "199")
    with patch.object(metabase_client, "fetch_card", return_value=[]) as m:
        service.facturas_periodo(date(2026, 5, 1), date(2026, 5, 31))
    args, kwargs = m.call_args
    assert args[0] == "199"
    params = kwargs.get("params")
    assert params is not None
    assert len(params) == 2
    # fecha_inicio
    assert params[0]["target"] == ["variable", ["template-tag", "fecha_inicio"]]
    assert params[0]["value"] == "2026-05-01"
    # fecha_fin
    assert params[1]["target"] == ["variable", ["template-tag", "fecha_fin"]]
    assert params[1]["value"] == "2026-05-31"


def test_facturas_periodo_acepta_strings_iso(monkeypatch):
    """Si paso strings 'YYYY-MM-DD' en lugar de date, también funciona."""
    monkeypatch.setenv("ASINFO_CARD_FACTURAS", "199")
    with patch.object(metabase_client, "fetch_card", return_value=[]) as m:
        service.facturas_periodo("2026-05-01", "2026-05-31")
    params = m.call_args.kwargs["params"]
    assert params[0]["value"] == "2026-05-01"
    assert params[1]["value"] == "2026-05-31"


def test_facturas_periodo_sin_env_devuelve_vacio(monkeypatch):
    monkeypatch.delenv("ASINFO_CARD_FACTURAS", raising=False)
    with patch.object(metabase_client, "fetch_card") as m:
        result = service.facturas_periodo("2026-05-01", "2026-05-31")
    assert result == []
    m.assert_not_called()


def test_facturas_totales_por_tipo_suma_correctamente(monkeypatch):
    """Replica el caso del 2026-05-20 — totales por tipo deben sumar bien."""
    rows = [
        {"tipo": "FACTURA", "kg": 100, "usd": 1000},
        {"tipo": "FACTURA", "kg": 200, "usd": 2000},
        {"tipo": "DEVOLUCION", "kg": -10, "usd": -100},
        {"tipo": "NC_FINANCIERA", "kg": 0, "usd": -50},
        {"tipo": "NC_FINANCIERA", "kg": 0, "usd": -30},
    ]
    monkeypatch.setenv("ASINFO_CARD_FACTURAS", "199")
    with patch.object(metabase_client, "fetch_card", return_value=rows):
        totales = service.facturas_totales_por_tipo("2026-05-01", "2026-05-31")
    assert totales["FACTURA"]["docs"] == 2
    assert totales["FACTURA"]["kg"] == 300.0
    assert totales["FACTURA"]["usd"] == 3000.0
    assert totales["DEVOLUCION"]["docs"] == 1
    assert totales["DEVOLUCION"]["kg"] == -10.0
    assert totales["NC_FINANCIERA"]["docs"] == 2
    assert totales["NC_FINANCIERA"]["kg"] == 0.0
    assert totales["NC_FINANCIERA"]["usd"] == -80.0


def test_facturas_totales_por_tipo_vacio_si_no_hay_data(monkeypatch):
    monkeypatch.setenv("ASINFO_CARD_FACTURAS", "199")
    with patch.object(metabase_client, "fetch_card", return_value=[]):
        assert service.facturas_totales_por_tipo("2026-05-01", "2026-05-31") == {}


def test_facturas_totales_robusto_con_nulls_y_strings(monkeypatch):
    """Si la card devuelve kg/usd como None o como string, no rompe."""
    rows = [
        {"tipo": "FACTURA", "kg": None, "usd": "1500.5"},
        {"tipo": None, "kg": 50, "usd": 500},  # tipo desconocido cae en '?'
    ]
    monkeypatch.setenv("ASINFO_CARD_FACTURAS", "199")
    with patch.object(metabase_client, "fetch_card", return_value=rows):
        totales = service.facturas_totales_por_tipo("2026-05-01", "2026-05-31")
    assert totales["FACTURA"]["kg"] == 0.0
    assert totales["FACTURA"]["usd"] == 1500.5
    assert "?" in totales


# ---------------------------------------------------------------------------
# stock_asinfo_lote / stock_asinfo_lote_totales
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_lote_caches():
    service._STOCK_LOTE_CACHE.clear()
    service._STOCK_LOTE_TOTALES_CACHE.clear()
    yield
    service._STOCK_LOTE_CACHE.clear()
    service._STOCK_LOTE_TOTALES_CACHE.clear()


def test_stock_lote_bodega_invalida_no_consulta():
    with patch.object(metabase_client, "fetch_dataset") as m:
        assert service.stock_asinfo_lote(None) == []
        assert service.stock_asinfo_lote("x") == []
    m.assert_not_called()


def test_stock_lote_mapea_y_pasa_bodega_al_sql():
    rows = [
        {"codigo": "TC-WAF", "producto": "TC WAFFER", "lote": "0003467002",
         "tejido": "TELA CRUDA", "calidad": "PRI", "color": "TELA CRUDA",
         "acabado": None, "estampado": None, "titulo_hilo": "HILO 22",
         "proveedor": "HYLTEXPOY 10", "unidad": "KG", "saldo": 22.75},
    ]
    with patch.object(metabase_client, "fetch_dataset", return_value=rows) as m:
        out = service.stock_asinfo_lote(52)
    # el id de bodega (int) viaja embebido en el SQL
    sql_arg = m.call_args.args[1]
    assert "id_bodega = 52" in sql_arg
    assert len(out) == 1
    r = out[0]
    assert r["codigo"] == "TC-WAF"
    assert r["titulo_hilo"] == "HILO 22"
    assert r["proveedor"] == "HYLTEXPOY 10"
    assert r["unidad"] == "KG"
    assert r["saldo"] == 22.75
    # nulls → string vacío, nunca None
    assert r["acabado"] == "" and r["estampado"] == ""


def test_stock_lote_descarta_saldo_no_positivo():
    rows = [
        {"codigo": "A", "producto": "A", "lote": "1", "saldo": 0},
        {"codigo": "B", "producto": "B", "lote": "2", "saldo": 5.0},
    ]
    with patch.object(metabase_client, "fetch_dataset", return_value=rows):
        out = service.stock_asinfo_lote(51)
    assert [r["codigo"] for r in out] == ["B"]


def test_stock_lote_q_va_al_sql_con_like():
    with patch.object(metabase_client, "fetch_dataset", return_value=[]) as m:
        service.stock_asinfo_lote(52, q="waf")
    sql_arg = m.call_args.args[1]
    assert "LIKE '%WAF%'" in sql_arg  # se normaliza a mayúsculas


def test_stock_lote_totales_mapea():
    rows = [
        {"id_bodega": 51, "bodega": "Bodega Hilo", "lotes": 61666, "total_kg": 1790694.44},
    ]
    with patch.object(metabase_client, "fetch_dataset", return_value=rows):
        out = service.stock_asinfo_lote_totales()
    assert out[0]["id_bodega"] == 51
    assert out[0]["lotes"] == 61666
    assert out[0]["total_kg"] == 1790694.44


# ---------------------------------------------------------------------------
# Vista /stock/asinfo-lote — render (landing + detalle)
# ---------------------------------------------------------------------------


def _login_informes(app, fake_db):
    rid = fake_db.add_role("Tester", ["stock.ver"])
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_vista_lote_landing_renderiza(app, fake_db):
    c = _login_informes(app, fake_db)
    totales = [{"id_bodega": 52, "bodega": "Bodega Tela Cruda", "lotes": 11971, "total_kg": 255795.25}]
    with patch.object(service, "stock_asinfo_lote_totales", return_value=totales):
        r = c.get("/stock/asinfo-lote")
    assert r.status_code == 200
    assert b"Tela Cruda" in r.data


def test_vista_lote_detalle_renderiza(app, fake_db):
    c = _login_informes(app, fake_db)
    totales = [{"id_bodega": 52, "bodega": "Bodega Tela Cruda", "lotes": 1, "total_kg": 22.75}]
    detalle = [{
        "codigo": "TC-WAF", "producto": "TC WAFFER", "lote": "0003467002",
        "tejido": "TELA CRUDA", "calidad": "PRI", "color": "TELA CRUDA",
        "acabado": "", "estampado": "", "titulo_hilo": "HILO 22",
        "proveedor": "HYLTEXPOY 10", "unidad": "KG", "saldo": 22.75,
        "_total_lotes": 1, "_total_kg": 22.75,
    }]
    with patch.object(service, "stock_asinfo_lote_totales", return_value=totales), \
         patch.object(service, "stock_asinfo_lote", return_value=detalle):
        r = c.get("/stock/asinfo-lote?bodega=52")
    assert r.status_code == 200
    assert b"TC-WAF" in r.data
    assert b"HILO 22" in r.data
    assert b"0003467002" in r.data


def test_vista_en_proceso_renderiza(app, fake_db):
    c = _login_informes(app, fake_db)
    data = {
        "pasos": [
            {"id_bodega": 52, "paso": "Tejeduría (Hilo → Tela Cruda)", "issued": 134714.67,
             "producido": 100284.58, "en_proceso": 34430.09, "n_ofts": 208},
        ],
        "ofts": [
            {"id_bodega": 52, "paso": "Tejeduría (Hilo → Tela Cruda)", "oft": "OFT-000035309",
             "producto": "TC Waffer", "prod_codigo": "TC-WAF", "planif": 4900.0,
             "fab": 4899.16, "issued": 4950.72, "en_proceso": 51.56},
        ],
    }
    with patch.object(service, "stock_en_proceso", return_value=data):
        r = c.get("/stock/en-proceso")
    assert r.status_code == 200
    assert b"Tejer" in r.data or b"Tejedur" in r.data
    assert b"OFT-000035309" in r.data
    assert b"En Proceso" in r.data or b"en Proceso" in r.data


def test_vista_lote_export_csv(app, fake_db):
    c = _login_informes(app, fake_db)
    detalle = [{
        "codigo": "TC-WAF", "producto": "TC WAFFER", "lote": "0003467002",
        "tejido": "TELA CRUDA", "calidad": "PRI", "color": "TELA CRUDA",
        "acabado": "", "estampado": "", "titulo_hilo": "HILO 22",
        "proveedor": "HYLTEXPOY 10", "unidad": "KG", "saldo": 22.75,
        "_total_lotes": 1, "_total_kg": 22.75,
    }]
    with patch.object(service, "stock_asinfo_lote_totales", return_value=[]), \
         patch.object(service, "stock_asinfo_lote", return_value=detalle):
        r = c.get("/stock/asinfo-lote?bodega=52&export=csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["Content-Type"]
    assert b"TC-WAF" in r.data
