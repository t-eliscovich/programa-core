"""Tests del puente formulas_app ↔ Programa Core (modules/costos_ot).

Cubre:
    - FakeAdapter: datos semilla, no-I/O, cliente desconocido, campos cálculo.
    - MetabaseAdapter: disponibilidad según env vars, fallback silencioso.
    - PostgresAdapter: decodificación de filas.
    - Factory build_adapter() según env var.
    - service.costos_por_cliente / fuente / disponible.
    - Panel en detalle factura no rompe si el adapter levanta.
"""
from __future__ import annotations

import os
from datetime import date
from unittest.mock import patch

import pytest

from modules.costos_ot import service
from modules.costos_ot.adapters import (
    FakeAdapter,
    MetabaseAdapter,
    OTCosto,
    PostgresAdapter,
    build_adapter,
)

# ---------------------------------------------------------------------------
# FakeAdapter
# ---------------------------------------------------------------------------

def test_fake_adapter_cliente_conocido_devuelve_ots():
    a = FakeAdapter()
    rows = a.costos_por_cliente("JTX")
    assert len(rows) >= 1
    assert all(isinstance(r, OTCosto) for r in rows)
    assert all(r.cliente_codigo == "JTX" for r in rows)
    assert all(r.fuente == "fake" for r in rows)


def test_fake_adapter_cliente_desconocido_devuelve_vacio():
    a = FakeAdapter()
    assert a.costos_por_cliente("NO-EXISTE") == []
    assert a.costos_por_cliente("") == []


def test_fake_adapter_costo_total_es_kg_por_costo_kg():
    a = FakeAdapter()
    rows = a.costos_por_cliente("JTX")
    for r in rows:
        assert r.costo_total == round(r.kg * r.costo_kg, 2)


def test_fake_adapter_disponible_true_sin_config():
    assert FakeAdapter().disponible() is True


def test_fake_adapter_costos_por_factura_siempre_vacio():
    # el fake no conoce el mapping factura→OT, por eso devuelve []
    assert FakeAdapter().costos_por_factura(1) == []


def test_fake_adapter_normaliza_codigo_cli_a_mayuscula():
    a = FakeAdapter()
    rows_upper = a.costos_por_cliente("JTX")
    rows_lower = a.costos_por_cliente("jtx")
    rows_mixed = a.costos_por_cliente("jTx")
    assert len(rows_upper) == len(rows_lower) == len(rows_mixed)


def test_fake_adapter_extra_override_permite_inyectar_datos_en_tests():
    a = FakeAdapter(_extra={"TEST": [{
        "n_orden": "99999",
        "fecha_cierre": date(2026, 4, 10),
        "descripcion": "Test OT",
        "kg": 100.0,
        "costo_kg": 2.0,
    }]})
    rows = a.costos_por_cliente("TEST")
    assert len(rows) == 1
    assert rows[0].n_orden == "99999"
    assert rows[0].costo_total == 200.0


def test_ot_costo_to_dict_serializa_fecha_iso():
    c = OTCosto(
        n_orden="1", fecha_cierre=date(2026, 4, 10), cliente_codigo="JTX",
        descripcion="x", kg=10, costo_total=20, costo_kg=2, fuente="fake",
    )
    d = c.to_dict()
    assert d["fecha_cierre"] == "2026-04-10"
    assert d["n_orden"] == "1"


def test_ot_costo_to_dict_fecha_none_no_rompe():
    c = OTCosto(
        n_orden="1", fecha_cierre=None, cliente_codigo="JTX",
        descripcion="x", kg=10, costo_total=20, costo_kg=2, fuente="fake",
    )
    d = c.to_dict()
    assert d["fecha_cierre"] is None


# ---------------------------------------------------------------------------
# MetabaseAdapter
# ---------------------------------------------------------------------------

def test_metabase_adapter_disponible_false_sin_env():
    # aseguramos entorno limpio
    with patch.dict(os.environ, {}, clear=False):
        for key in ("METABASE_URL", "METABASE_USERNAME", "METABASE_PASSWORD",
                    "METABASE_QUESTION_ID_COSTOS_OT"):
            os.environ.pop(key, None)
        a = MetabaseAdapter()
        assert a.disponible() is False


def test_metabase_adapter_disponible_true_con_env_completo():
    env = {
        "METABASE_URL": "https://metabase.test",
        "METABASE_USERNAME": "u",
        "METABASE_PASSWORD": "p",
        "METABASE_QUESTION_ID_COSTOS_OT": "42",
    }
    with patch.dict(os.environ, env, clear=False):
        assert MetabaseAdapter().disponible() is True


def test_metabase_adapter_sin_config_devuelve_lista_vacia_sin_error():
    # No env vars — no I/O. costos_por_cliente devuelve vacío y no levanta.
    with patch.dict(os.environ, {}, clear=False):
        for key in ("METABASE_URL", "METABASE_USERNAME", "METABASE_PASSWORD",
                    "METABASE_QUESTION_ID_COSTOS_OT"):
            os.environ.pop(key, None)
        assert MetabaseAdapter().costos_por_cliente("JTX") == []


def test_metabase_row_to_costo_normaliza_fecha_iso():
    row = {
        "n_orden": "123",
        "fecha_cierre": "2026-04-10T00:00:00",
        "cliente_codigo": "jtx",
        "descripcion": "tela",
        "kg": "50.5",
        "costo_kg": "1.78",
    }
    c = MetabaseAdapter._row_to_costo(row)
    assert c.fecha_cierre == date(2026, 4, 10)
    assert c.cliente_codigo == "JTX"
    assert c.kg == 50.5
    assert c.costo_total == round(50.5 * 1.78, 2)
    assert c.fuente == "metabase"


def test_metabase_row_to_costo_fecha_invalida_queda_none():
    row = {
        "n_orden": "1", "fecha_cierre": "no-es-fecha", "cliente_codigo": "J",
        "descripcion": "", "kg": 0, "costo_kg": 0,
    }
    c = MetabaseAdapter._row_to_costo(row)
    assert c.fecha_cierre is None


# ---------------------------------------------------------------------------
# PostgresAdapter
# ---------------------------------------------------------------------------

def test_postgres_adapter_row_to_costo():
    row = {
        "n_orden": "24089",
        "fecha_cierre": date(2026, 4, 10),
        "cliente_codigo": "JTX",
        "descripcion": "Jersey",
        "kg": 100.0,
        "costo_kg": 2.0,
    }
    c = PostgresAdapter._row_to_costo(row)
    assert c.costo_total == 200.0
    assert c.fuente == "postgres"


def test_postgres_adapter_fetch_falla_devuelve_vacio(monkeypatch):
    """Si la vista no existe (migración no-aplicada), no reventar."""
    import db

    def _raise(*args, **kwargs):
        raise Exception("relation scintela.vw_costos_ordenes does not exist")

    monkeypatch.setattr(db, "fetch_all", _raise)
    assert PostgresAdapter().costos_por_cliente("JTX") == []


def test_postgres_adapter_codigo_cli_vacio():
    assert PostgresAdapter().costos_por_cliente("") == []


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def test_build_adapter_default_es_fake():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("COSTOS_OT_ADAPTER", None)
        a = build_adapter()
        assert isinstance(a, FakeAdapter)


def test_build_adapter_explicit_override():
    assert isinstance(build_adapter("fake"), FakeAdapter)
    assert isinstance(build_adapter("metabase"), MetabaseAdapter)
    assert isinstance(build_adapter("postgres"), PostgresAdapter)


def test_build_adapter_desconocido_fallback_a_fake():
    a = build_adapter("algo-que-no-existe")
    assert isinstance(a, FakeAdapter)


def test_build_adapter_lee_env(monkeypatch):
    monkeypatch.setenv("COSTOS_OT_ADAPTER", "postgres")
    a = build_adapter()
    assert isinstance(a, PostgresAdapter)


# ---------------------------------------------------------------------------
# service fachada
# ---------------------------------------------------------------------------

def test_service_reset_y_costos_con_fake():
    service.reset_adapter(FakeAdapter())
    try:
        rows = service.costos_por_cliente("JTX")
        assert len(rows) >= 1
        assert service.fuente() == "fake"
        assert service.disponible() is True
    finally:
        service.reset_adapter(None)


def test_service_absorbe_exceptions_del_adapter():
    class BoomAdapter:
        fuente = "boom"
        def costos_por_cliente(self, codigo_cli):
            raise RuntimeError("simulated backend failure")
        def costos_por_factura(self, id_factura):
            raise RuntimeError("simulated backend failure")
        def disponible(self):
            raise RuntimeError("simulated backend failure")

    service.reset_adapter(BoomAdapter())
    try:
        # nunca levanta, siempre degrada a vacío / False
        assert service.costos_por_cliente("JTX") == []
        assert service.costos_por_factura(1) == []
        assert service.disponible() is False
    finally:
        service.reset_adapter(None)


# ---------------------------------------------------------------------------
# Vista HTTP — smoke de las rutas con cliente real
# ---------------------------------------------------------------------------

def _login_cartera(app, fake_db):
    """Crea un usuario Dueño-ish con cartera.ver y devuelve el test client logueado."""
    rid = fake_db.add_role("Cartera", ["cartera.ver"])
    uid = fake_db.add_user("cobranzas", b"$2b$12$fakehashplaceholderfakehashplaceholderfakeh", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def test_ruta_fragment_renderiza_con_cliente_conocido(app, fake_db):
    """El partial HTMX que embebe el detalle de factura tiene que cargar."""
    service.reset_adapter(FakeAdapter())
    try:
        c = _login_cartera(app, fake_db)
        r = c.get("/costos-ot/cliente/JTX/fragment")
        assert r.status_code == 200, r.data[:400]
        assert b"JTX" in r.data
    finally:
        service.reset_adapter(None)


def test_ruta_fragment_cliente_desconocido_devuelve_200_con_empty(app, fake_db):
    """Cliente que no tiene OTs — vista vacía, no 500."""
    service.reset_adapter(FakeAdapter())
    try:
        c = _login_cartera(app, fake_db)
        r = c.get("/costos-ot/cliente/ZZZZ/fragment")
        assert r.status_code == 200
        # Empty state reusa la macro `empty_row` que imprime el mensaje.
        assert b"OTs cerradas" in r.data or b"No hay" in r.data or b"no tiene" in r.data
    finally:
        service.reset_adapter(None)


def test_service_reset_adapter_con_none_fuerza_rebuild_en_proximo_get():
    service.reset_adapter(FakeAdapter())
    assert service.fuente() == "fake"
    service.reset_adapter(None)
    # el próximo get_adapter reconstruye desde env (default = fake)
    assert service.fuente() == "fake"
