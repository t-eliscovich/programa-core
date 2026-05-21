"""Tests del endpoint /healthz/integraciones.

Cubre:
    - Sin nada configurado: ambos bridges aparecen como configured=False,
      reachable=None. HTTP 200.
    - formulas_db configured + healthy: reachable=True con latency.
    - formulas_db configured pero rds caído: reachable=False.
    - metabase configured + login OK: reachable=True.
    - metabase configured pero login falla: reachable=False.
    - El endpoint nunca devuelve 5xx (es informativo, no gate).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from modules._lib import formulas_db, metabase_client


def test_integraciones_sin_nada_configurado_devuelve_200_con_nones(app):
    with patch.object(formulas_db, "disponible", return_value=False), \
         patch.object(metabase_client, "disponible", return_value=False):
        r = app.test_client().get("/healthz/integraciones")
    assert r.status_code == 200
    body = r.get_json()
    assert body["formulas_app"]["configured"] is False
    assert body["formulas_app"]["reachable"] is None
    assert body["metabase"]["configured"] is False
    assert body["metabase"]["reachable"] is None
    assert "ts" in body


def test_integraciones_formulas_db_configured_y_healthy(app):
    with patch.object(formulas_db, "disponible", return_value=True), \
         patch.object(formulas_db, "healthcheck", return_value=True), \
         patch.object(metabase_client, "disponible", return_value=False):
        r = app.test_client().get("/healthz/integraciones")
    body = r.get_json()
    assert body["formulas_app"]["configured"] is True
    assert body["formulas_app"]["reachable"] is True
    assert isinstance(body["formulas_app"]["latency_ms"], float)


def test_integraciones_formulas_db_caida(app):
    """RDS down → reachable=False, igual 200 (es informativo)."""
    with patch.object(formulas_db, "disponible", return_value=True), \
         patch.object(formulas_db, "healthcheck", return_value=False), \
         patch.object(metabase_client, "disponible", return_value=False):
        r = app.test_client().get("/healthz/integraciones")
    assert r.status_code == 200
    body = r.get_json()
    assert body["formulas_app"]["reachable"] is False


def test_integraciones_formulas_db_healthcheck_levanta_se_absorbe(app):
    """Si healthcheck levanta excepción, el endpoint lo trata como reachable=False."""
    def _boom():
        raise RuntimeError("network glitch")

    with patch.object(formulas_db, "disponible", return_value=True), \
         patch.object(formulas_db, "healthcheck", side_effect=_boom), \
         patch.object(metabase_client, "disponible", return_value=False):
        r = app.test_client().get("/healthz/integraciones")
    assert r.status_code == 200
    body = r.get_json()
    assert body["formulas_app"]["reachable"] is False


def test_integraciones_metabase_configured_login_ok(app):
    with patch.object(formulas_db, "disponible", return_value=False), \
         patch.object(metabase_client, "disponible", return_value=True), \
         patch.object(metabase_client, "_login", return_value="fake-token"):
        r = app.test_client().get("/healthz/integraciones")
    body = r.get_json()
    assert body["metabase"]["configured"] is True
    assert body["metabase"]["reachable"] is True


def test_integraciones_metabase_login_falla(app):
    with patch.object(formulas_db, "disponible", return_value=False), \
         patch.object(metabase_client, "disponible", return_value=True), \
         patch.object(metabase_client, "_login", return_value=None):
        r = app.test_client().get("/healthz/integraciones")
    body = r.get_json()
    assert body["metabase"]["reachable"] is False


def test_integraciones_no_expone_credenciales(app):
    """La respuesta no debería incluir URLs / usernames / passwords."""
    with patch.object(formulas_db, "disponible", return_value=True), \
         patch.object(formulas_db, "healthcheck", return_value=True), \
         patch.object(metabase_client, "disponible", return_value=True), \
         patch.object(metabase_client, "_login", return_value="tok"):
        r = app.test_client().get("/healthz/integraciones")
    body_str = r.get_data(as_text=True).lower()
    for forbidden in ("password", "secret", "metabase_url", "database_url", "@"):
        assert forbidden not in body_str, f"{forbidden!r} aparece en la respuesta"
