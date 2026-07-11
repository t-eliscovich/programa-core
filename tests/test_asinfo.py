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
    service._EN_PROCESO_CACHE.clear()
    yield
    service._STOCK_LOTE_CACHE.clear()
    service._STOCK_LOTE_TOTALES_CACHE.clear()
    service._EN_PROCESO_CACHE.clear()


# ---------------------------------------------------------------------------
# stock_en_proceso (WIP entre pasos)
# ---------------------------------------------------------------------------


def test_stock_en_proceso_agrega_y_filtra():
    rows = [
        # Tejeduría: en_proceso > 0 → cuenta
        {"id_bodega": 52, "oft": "OFT-1", "producto": "TC A", "prod_codigo": "TCA",
         "planif": 100, "fab": 60, "issued": 100, "en_proceso": 40},
        # Tintorería: en_proceso > 0 → cuenta
        {"id_bodega": 53, "oft": "OFT-3", "producto": "PT C", "prod_codigo": "PTC",
         "planif": 30, "fab": 20, "issued": 30, "en_proceso": 10},
        # en_proceso <= 0 (produjo más de lo despachado) → se ignora
        {"id_bodega": 53, "oft": "OFT-2", "producto": "PT B", "prod_codigo": "PTB",
         "planif": 50, "fab": 60, "issued": 50, "en_proceso": -10},
        # fila corrupta → except, se saltea
        {"id_bodega": None, "oft": "X", "issued": 1, "fab": 0, "en_proceso": 1},
    ]
    with patch.object(metabase_client, "fetch_dataset", return_value=rows):
        out = service.stock_en_proceso()
    pasos = {p["id_bodega"]: p for p in out["pasos"]}
    assert pasos[52]["en_proceso"] == 40
    assert pasos[52]["n_ofts"] == 1
    assert pasos[53]["en_proceso"] == 10
    assert pasos[53]["n_ofts"] == 1
    # sólo las OFT con en_proceso>0 están en el detalle (no la negativa)
    assert [o["oft"] for o in out["ofts"]] == ["OFT-1", "OFT-3"]
    assert "Tejedur" in pasos[52]["paso"]


def test_stock_en_proceso_cachea():
    rows = [{"id_bodega": 52, "oft": "O", "producto": "p", "prod_codigo": "p",
             "planif": 1, "fab": 0, "issued": 1, "en_proceso": 1}]
    with patch.object(metabase_client, "fetch_dataset", return_value=rows) as m:
        service.stock_en_proceso()
        service.stock_en_proceso()  # 2ª vez sale del cache
    assert m.call_count == 1


def test_vista_en_proceso_filtro_y_csv(app, fake_db):
    c = _login_informes(app, fake_db)
    data = {
        "pasos": [{"id_bodega": 52, "paso": "Tejeduría (Hilo → Tela Cruda)", "issued": 100.0,
                   "producido": 60.0, "en_proceso": 40.0, "n_ofts": 1}],
        "ofts": [{"id_bodega": 52, "paso": "Tejeduría (Hilo → Tela Cruda)", "oft": "OFT-1",
                  "producto": "TC A", "prod_codigo": "TCA", "planif": 100.0, "fab": 60.0,
                  "issued": 100.0, "en_proceso": 40.0}],
    }
    with patch.object(service, "stock_en_proceso", return_value=data):
        r = c.get("/stock/en-proceso?paso=52")
        assert r.status_code == 200
        assert b"OFT-1" in r.data
        # paso inválido → no filtra, no rompe
        r2 = c.get("/stock/en-proceso?paso=abc")
        assert r2.status_code == 200
        rc = c.get("/stock/en-proceso?paso=52&export=csv")
        assert rc.status_code == 200
        assert "text/csv" in rc.headers["Content-Type"]


def test_vista_en_proceso_error_no_rompe(app, fake_db):
    c = _login_informes(app, fake_db)
    with patch.object(service, "stock_en_proceso", side_effect=RuntimeError("asinfo caído")):
        r = c.get("/stock/en-proceso")
    assert r.status_code == 200
    assert b"asinfo" in r.data.lower() or b"Error" in r.data


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


# ─── _resolver_cliente_asinfo (TMT 2026-06-10) ─────────────────────────────
# Carga desde Asinfo: alias-aware + auto-crear cliente faltante.

def _setup_resolver(monkeypatch, existentes, alias_map=None, facturas_pc=None):
    """Stubs: scintela.cliente contiene `existentes`; alias map dado;
    `facturas_pc` = filas {codigo_cli, importe} con el numf consultado."""
    import db as _db
    from modules.asinfo import aliases as _aliases
    from modules.facturas import views as _views

    inserts = []
    aliases_creados = []

    def fake_fetch_all(sql, params=None, *a, **kw):
        if "FROM scintela.factura" in sql:
            return list(facturas_pc or [])
        return []

    def fake_agregar(a, p, nota="", usuario="web"):
        aliases_creados.append((a, p))
        return True

    def fake_fetch_one(sql, params=None, *a, **kw):
        if "FROM scintela.cliente" in sql:
            return {"?column?": 1} if params[0] in existentes else None
        return None

    def fake_execute(sql, params=None, *a, **kw):
        if "INSERT INTO scintela.cliente" in sql:
            inserts.append(params)
        return 1

    monkeypatch.setattr(_db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(_db, "execute", fake_execute)
    monkeypatch.setattr(_db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(_views.db, "fetch_one", fake_fetch_one, raising=False)
    monkeypatch.setattr(_views.db, "execute", fake_execute, raising=False)
    monkeypatch.setattr(_views.db, "fetch_all", fake_fetch_all, raising=False)
    monkeypatch.setattr(_aliases, "to_pc",
                        lambda c: (alias_map or {}).get((c or "").strip().upper(),
                                                        (c or "").strip().upper()))
    monkeypatch.setattr(_aliases, "agregar", fake_agregar)
    return _views, inserts, aliases_creados


def test_resolver_cliente_existente_directo(monkeypatch):
    views, inserts, _ = _setup_resolver(monkeypatch, existentes={"AJO"})
    assert views._resolver_cliente_asinfo("AJO", "tam") == ("AJO", False)
    assert inserts == []


def test_resolver_cliente_via_alias(monkeypatch):
    # Asinfo manda AJ2, PC tiene AJO, alias AJ2->AJO: debe usar AJO sin crear.
    views, inserts, _ = _setup_resolver(monkeypatch, existentes={"AJO"},
                                     alias_map={"AJ2": "AJO"})
    assert views._resolver_cliente_asinfo("AJ2", "tam") == ("AJO", False)
    assert inserts == []


def test_resolver_cliente_codigo_crudo_fallback(monkeypatch):
    # Alias apunta a un canonical que NO esta, pero el crudo SI (facturas
    # viejas cargadas bajo el alias): usar el crudo, no crear.
    views, inserts, _ = _setup_resolver(monkeypatch, existentes={"AJ2"},
                                     alias_map={"AJ2": "AJO"})
    assert views._resolver_cliente_asinfo("AJ2", "tam") == ("AJ2", False)
    assert inserts == []


def test_resolver_cliente_faltante_auto_crea(monkeypatch):
    # Cliente nuevo del dBase que aun no paso por clientes-import: se crea.
    views, inserts, _ = _setup_resolver(monkeypatch, existentes=set())
    cod, creado = views._resolver_cliente_asinfo("NUEVO", "tam")
    assert (cod, creado) == ("NUEVO", True)
    assert len(inserts) == 1
    # params: (codigo, nombre, ruc, usuario, codigo_where) — nombre/ruc
    # vienen de Asinfo (None acá porque Metabase no está configurado)
    assert inserts[0][0] == "NUEVO"
    assert inserts[0][3] == "tam"
    assert inserts[0][4] == "NUEVO"  # param del WHERE NOT EXISTS


def test_resolver_cliente_normaliza_y_trunca(monkeypatch):
    views, inserts, _ = _setup_resolver(monkeypatch, existentes=set())
    cod, creado = views._resolver_cliente_asinfo("  abcdef ", "u" * 80)
    assert creado is True
    assert cod == "ABCDE"  # upper + varchar(5)
    assert len(inserts[0][3]) == 50  # usuario truncado a varchar(50)


def test_resolver_guard_factura_ya_cargada_otro_cli_auto_alias(monkeypatch):
    # numf ya existe bajo AJO con MISMO importe y cliente AJ3 no existe:
    # NO crear cliente duplicado, SI registrar alias AJ3->AJO, skip fila.
    import pytest
    views, inserts, aliases_creados = _setup_resolver(
        monkeypatch, existentes=set(),
        facturas_pc=[{"codigo_cli": "AJO", "importe": 1234.56}],
    )
    with pytest.raises(views._CargaAsinfoSkip, match="AJO"):
        views._resolver_cliente_asinfo(
            "AJ3", "tam", numero="001-099-000175661", importe=1234.56,
        )
    assert inserts == []                      # no se creo cliente
    assert aliases_creados == [("AJ3", "AJO")]  # se mapeo el de Asinfo


def test_resolver_guard_mismo_codigo_no_duplica(monkeypatch):
    # Factura ya cargada bajo el MISMO codigo (sin ficha de cliente):
    # skip sin crear nada.
    import pytest
    views, inserts, aliases_creados = _setup_resolver(
        monkeypatch, existentes=set(),
        facturas_pc=[{"codigo_cli": "AJ3", "importe": 100.0}],
    )
    with pytest.raises(views._CargaAsinfoSkip, match="no se duplicó"):
        views._resolver_cliente_asinfo("AJ3", "tam", numero="123", importe=100.0)
    assert inserts == [] and aliases_creados == []


def test_resolver_guard_importe_distinto_sugiere_sin_alias(monkeypatch):
    # numf colisiona pero el importe NO matchea: ambiguo -> skip con
    # sugerencia, SIN alias automatico y SIN cliente nuevo.
    import pytest
    views, inserts, aliases_creados = _setup_resolver(
        monkeypatch, existentes=set(),
        facturas_pc=[{"codigo_cli": "XXX", "importe": 999.0}],
    )
    with pytest.raises(views._CargaAsinfoSkip, match="alias"):
        views._resolver_cliente_asinfo("AJ3", "tam", numero="123", importe=50.0)
    assert inserts == [] and aliases_creados == []


def test_resolver_guard_numf_libre_crea_normal(monkeypatch):
    # numf no existe en PC: flujo normal de auto-create.
    views, inserts, aliases_creados = _setup_resolver(
        monkeypatch, existentes=set(), facturas_pc=[],
    )
    cod, creado = views._resolver_cliente_asinfo(
        "NUEVO", "tam", numero="001-099-000175662", importe=10.0,
    )
    assert (cod, creado) == ("NUEVO", True)
    assert len(inserts) == 1 and aliases_creados == []


# ─── cliente_ficha desde Asinfo (TMT 2026-06-10) ───────────────────────────

def test_cliente_ficha_sanitiza_codigos_y_mapea(monkeypatch):
    from modules._lib import metabase_client as mc
    from modules.asinfo import service as svc
    capturas = []

    def fake_fetch_dataset(db_id, sql, max_results=None):
        capturas.append((db_id, sql))
        return [{"codigo": "AQN", "nombre": "ANA QUISPE NUÑEZ", "ruc": "1712345678001"}]

    monkeypatch.setattr(mc, "disponible", lambda: True)
    monkeypatch.setattr(mc, "fetch_dataset", fake_fetch_dataset)
    out = svc.cliente_ficha(["aqn", "x'; DROP TABLE--", "", None, "MNM"])
    assert out == {"AQN": {"nombre": "ANA QUISPE NUÑEZ", "ruc": "1712345678001"}}
    db_id, sql = capturas[0]
    assert db_id == 2
    assert "'AQN'" in sql and "'MNM'" in sql
    assert "DROP" not in sql  # el código inválido fue descartado


def test_cliente_ficha_sin_metabase_devuelve_vacio(monkeypatch):
    from modules._lib import metabase_client as mc
    from modules.asinfo import service as svc
    monkeypatch.setattr(mc, "disponible", lambda: False)
    assert svc.cliente_ficha(["AQN"]) == {}
