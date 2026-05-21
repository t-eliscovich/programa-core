"""Tests del cliente Metabase compartido (modules._lib.metabase_client).

Cubre:
    - disponible() según presencia de las env vars METABASE_URL/USER/PASS.
    - fetch_card() sin card_id devuelve [].
    - fetch_card() con login exitoso devuelve los rows.
    - fetch_card() con 401 inicial: re-login y reintento (refresh on expired token).
    - fetch_card() con login fallido devuelve [] sin levantar.
    - fetch_card() ante excepción de red devuelve [] (fail-soft).
    - reset_session() fuerza re-login en la próxima llamada.

Diseño: nunca hacemos HTTP real. Mockeamos `requests` con un fake module
que entrega responses controladas.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from modules._lib import metabase_client


@pytest.fixture(autouse=True)
def _reset_token():
    """Cada test arranca sin token guardado."""
    metabase_client._session_token = None
    yield
    metabase_client._session_token = None


@pytest.fixture
def env_configured(monkeypatch):
    """Setea las env vars que el cliente espera."""
    monkeypatch.setenv("METABASE_URL", "http://localhost:3000")
    monkeypatch.setenv("METABASE_USERNAME", "u@test.com")
    monkeypatch.setenv("METABASE_PASSWORD", "secret")


# ---------------------------------------------------------------------------
# disponible()
# ---------------------------------------------------------------------------


def test_disponible_false_sin_env(monkeypatch):
    monkeypatch.delenv("METABASE_URL", raising=False)
    monkeypatch.delenv("METABASE_USERNAME", raising=False)
    monkeypatch.delenv("METABASE_PASSWORD", raising=False)
    assert metabase_client.disponible() is False


def test_disponible_false_con_solo_url(monkeypatch):
    monkeypatch.setenv("METABASE_URL", "http://localhost:3000")
    monkeypatch.delenv("METABASE_USERNAME", raising=False)
    monkeypatch.delenv("METABASE_PASSWORD", raising=False)
    assert metabase_client.disponible() is False


def test_disponible_true_con_las_tres_vars(env_configured):
    assert metabase_client.disponible() is True


# ---------------------------------------------------------------------------
# fetch_card()
# ---------------------------------------------------------------------------


def _fake_requests(login_status=200, login_body=None, query_responses=None):
    """Construye un MagicMock que parodia el módulo `requests`.

    - POST /api/session: devuelve `login_status` + `login_body or {"id": "tok1"}`.
    - POST /api/card/.../query/json: devuelve los responses de `query_responses`
      en orden (lista de tuplas (status_code, json_body)).
    """
    fake = MagicMock()
    login_resp = MagicMock()
    login_resp.status_code = login_status
    login_resp.raise_for_status.side_effect = (
        Exception("login failed") if login_status >= 400 else None
    )
    login_resp.json.return_value = login_body or {"id": "tok1"}

    query_responses = list(query_responses or [(200, [])])
    query_resps = []
    for status, body in query_responses:
        r = MagicMock()
        r.status_code = status
        r.raise_for_status.side_effect = (
            Exception(f"http {status}") if status >= 400 and status != 401 else None
        )
        r.json.return_value = body
        query_resps.append(r)

    def post(url, **kwargs):
        if url.endswith("/api/session"):
            return login_resp
        return query_resps.pop(0) if query_resps else MagicMock(
            status_code=500, json=lambda: {}, raise_for_status=MagicMock(side_effect=Exception("no más responses"))
        )

    fake.post.side_effect = post
    return fake


def test_fetch_card_sin_card_id_devuelve_vacio(env_configured):
    assert metabase_client.fetch_card(None) == []
    assert metabase_client.fetch_card("") == []


def test_fetch_card_sin_env_devuelve_vacio(monkeypatch):
    monkeypatch.delenv("METABASE_URL", raising=False)
    assert metabase_client.fetch_card(116) == []


def test_fetch_card_ok_devuelve_rows(env_configured):
    rows = [{"cliente": "JTX", "kg": 100}, {"cliente": "TEX", "kg": 200}]
    fake = _fake_requests(query_responses=[(200, rows)])
    with patch.dict("sys.modules", {"requests": fake}):
        result = metabase_client.fetch_card(116)
    assert result == rows
    # Debió haber hecho un login + un POST a /api/card/116/query/json
    assert fake.post.call_count == 2


def test_fetch_card_401_dispara_refresh_y_reintento(env_configured):
    """Token vencido: la primera query da 401, se re-loguea y reintenta una vez."""
    rows = [{"x": 1}]
    fake = _fake_requests(query_responses=[(401, None), (200, rows)])
    # Pre-cargamos un token "vencido" para que primero use ese.
    metabase_client._session_token = "expired-token"
    with patch.dict("sys.modules", {"requests": fake}):
        result = metabase_client.fetch_card(116)
    assert result == rows
    # 1 login (refresh) + 2 queries (la 401 + la retry exitosa)
    assert fake.post.call_count == 3


def test_fetch_card_login_falla_devuelve_vacio(env_configured):
    fake = _fake_requests(login_status=403)
    with patch.dict("sys.modules", {"requests": fake}):
        result = metabase_client.fetch_card(116)
    assert result == []


def test_fetch_card_excepcion_red_devuelve_vacio(env_configured):
    fake = MagicMock()
    fake.post.side_effect = Exception("network unreachable")
    with patch.dict("sys.modules", {"requests": fake}):
        result = metabase_client.fetch_card(116)
    assert result == []


def test_fetch_card_response_no_lista_devuelve_vacio(env_configured):
    """Si Metabase devuelve un dict (ej. un error JSON), devolvemos []."""
    fake = _fake_requests(query_responses=[(200, {"error": "bad query"})])
    with patch.dict("sys.modules", {"requests": fake}):
        result = metabase_client.fetch_card(116)
    assert result == []


def test_reset_session_fuerza_relogin(env_configured):
    metabase_client._session_token = "old-token"
    metabase_client.reset_session()
    assert metabase_client._session_token is None


def test_fetch_card_pasa_parameters_si_se_proveen(env_configured):
    fake = _fake_requests(query_responses=[(200, [])])
    with patch.dict("sys.modules", {"requests": fake}):
        metabase_client.fetch_card(
            116,
            params=[{"type": "category", "target": ["variable", ["template-tag", "v"]], "value": "JTX"}],
        )
    # La segunda llamada a post (la query) debió incluir parameters en el JSON body
    query_call = fake.post.call_args_list[1]
    body = query_call.kwargs.get("json", {})
    assert "parameters" in body
    assert body["parameters"][0]["value"] == "JTX"
