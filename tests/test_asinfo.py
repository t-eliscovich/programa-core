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
