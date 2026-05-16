"""Tests for the X-Request-Id + bitácora correlation (batch 6, 2026-04-17).

Invariants under test:
    - Cada response lleva header `X-Request-Id` con forma de UUID canónico.
    - `g.request_id` queda disponible desde el primer handler.
    - Si el cliente manda un `X-Request-Id` válido, se respeta; si manda
      basura, se genera uno nuevo.
    - El INSERT en `scintela.bitacora_acciones` incluye el request_id
      como último parámetro.
    - El visor /bitacora acepta `?request_id=...` (completo o prefijo).
"""
from __future__ import annotations

import re
import uuid

import pytest

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# --- header emission ------------------------------------------------------


def test_x_request_id_header_present_on_every_response(client):
    """Cada response tiene X-Request-Id, incluso la página pública de login."""
    resp = client.get("/login")
    rid = resp.headers.get("X-Request-Id")
    assert rid is not None, "Falta el header X-Request-Id en la respuesta."
    assert UUID_RE.match(rid), f"X-Request-Id no es un UUID canónico: {rid!r}"


def test_x_request_id_is_per_request(client):
    """Dos requests seguidas tienen request_ids distintos."""
    r1 = client.get("/login").headers.get("X-Request-Id")
    r2 = client.get("/login").headers.get("X-Request-Id")
    assert r1 and r2 and r1 != r2, (
        f"Los request_ids deben ser únicos por request, vi {r1!r} y {r2!r}."
    )


def test_incoming_x_request_id_respected_when_uuid_shaped(client):
    """Si viene un X-Request-Id válido, se respeta y ecoa tal cual."""
    incoming = str(uuid.uuid4())
    resp = client.get("/login", headers={"X-Request-Id": incoming})
    assert resp.headers.get("X-Request-Id") == incoming


def test_incoming_x_request_id_rejected_when_garbage(client):
    """Un X-Request-Id malformado se descarta y se genera uno nuevo."""
    resp = client.get("/login", headers={"X-Request-Id": "no-soy-un-uuid"})
    rid = resp.headers.get("X-Request-Id")
    assert rid is not None
    assert rid != "no-soy-un-uuid"
    assert UUID_RE.match(rid)


def test_incoming_x_request_id_rejected_when_wrong_length(client):
    """Un X-Request-Id con los 36 chars pero no UUID también se descarta."""
    resp = client.get(
        "/login",
        headers={"X-Request-Id": "a" * 36},  # 36 chars pero no hex-con-guiones
    )
    rid = resp.headers.get("X-Request-Id")
    assert rid != "a" * 36
    assert UUID_RE.match(rid)


# --- bitácora INSERT ------------------------------------------------------


def test_registrar_bitacora_incluye_request_id(app, monkeypatch):
    """`registrar_bitacora` escribe request_id como último parámetro."""
    import db
    from auth import registrar_bitacora

    captured: list[tuple] = []

    def _capture_execute(sql, params=None, conn=None):
        captured.append((sql, tuple(params or ())))
        return 1

    monkeypatch.setattr(db, "execute", _capture_execute)

    # Simulamos un request: request_id en g, request context activo.
    with app.test_request_context("/test-bitacora", method="POST"):
        from flask import g
        g.request_id = "11111111-2222-3333-4444-555555555555"
        g.user = {"username": "tmt", "nombre_rol": "Dueño"}
        registrar_bitacora(status_http=200, resumen="test")

    assert len(captured) == 1, "Debería haber exactamente un INSERT."
    sql, params = captured[0]

    # Column-list y placeholders incluyen request_id.
    sql_norm = " ".join(sql.split()).lower()
    assert "request_id" in sql_norm, "El SQL del INSERT no menciona request_id."

    # El último parámetro es el request_id.
    assert params[-1] == "11111111-2222-3333-4444-555555555555", (
        f"request_id no está al final de los parámetros: {params[-1]!r}"
    )


def test_registrar_bitacora_request_id_none_cuando_ausente(app, monkeypatch):
    """Si `g.request_id` no está, el INSERT manda NULL en esa columna."""
    import db
    from auth import registrar_bitacora

    captured: list[tuple] = []

    def _capture_execute(sql, params=None, conn=None):
        captured.append((sql, tuple(params or ())))
        return 1

    monkeypatch.setattr(db, "execute", _capture_execute)

    with app.test_request_context("/sin-rid", method="POST"):
        from flask import g
        # No ponemos g.request_id a propósito.
        g.user = {"username": "tmt", "nombre_rol": "Dueño"}
        registrar_bitacora(status_http=200)

    assert captured, "Debería haber INSERT."
    _sql, params = captured[0]
    assert params[-1] is None


# --- query listar() --------------------------------------------------------


def test_listar_acepta_request_id_prefijo():
    """queries.listar(request_id='abc') usa LIKE 'abc%'."""
    import db
    from modules.bitacora import queries

    captured: dict = {}

    def _fake_fetch_all(sql, params=None, conn=None):
        captured["sql"] = sql
        captured["params"] = params
        return []

    orig = db.fetch_all
    try:
        db.fetch_all = _fake_fetch_all
        queries.listar(request_id="abc123")
    finally:
        db.fetch_all = orig

    sql_norm = " ".join(captured["sql"].split()).lower()
    assert "request_id like" in sql_norm, (
        "La query debe filtrar por LIKE sobre request_id."
    )
    assert captured["params"]["rid"] == "abc123"
    assert captured["params"]["rid_prefix"] == "abc123%"


def test_listar_sin_request_id_no_restringe():
    """Sin request_id, la cláusula LIKE queda neutralizada por `rid IS NULL`."""
    import db
    from modules.bitacora import queries

    captured: dict = {}

    def _fake_fetch_all(sql, params=None, conn=None):
        captured["params"] = params
        return []

    orig = db.fetch_all
    try:
        db.fetch_all = _fake_fetch_all
        queries.listar()
    finally:
        db.fetch_all = orig

    assert captured["params"]["rid"] is None
    assert captured["params"]["rid_prefix"] is None


# --- sanity --------------------------------------------------------------


@pytest.mark.parametrize("path", ["/login", "/healthz"])
def test_header_no_rompe_rutas_sin_sesion(client, path):
    """El hook de request_id no depende de g.user; funciona con user anónimo."""
    resp = client.get(path)
    # /healthz puede no existir; sólo nos importa que el header se emita.
    if resp.status_code != 404:
        rid = resp.headers.get("X-Request-Id")
        assert rid and UUID_RE.match(rid)
