"""Tests para el timeout de sesión por rol.

Invariantes:
  1. `timeout_for_role` devuelve el valor del mapa, o el default si el rol no
     está listado.
  2. `_parse_last_activity` tolera None, strings inválidas, y valores naive
     (los asume UTC).
  3. Si la sesión tiene last_activity más viejo que el timeout del rol,
     `load_logged_in_user` limpia la sesión y deja g.user=None.
  4. Un request dentro del timeout deja la sesión intacta y actualiza
     last_activity a `now`.
  5. Requests a paths de keep-alive (/static, /healthz) NO reinician el
     contador — si alguien se va a almorzar y el monitor pinguea /healthz
     cada minuto, igual lo deslogueamos a las 4h.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

UTC = timezone.utc  # noqa: UP017  # py3.10 compat — ver auth.py

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def test_timeout_por_rol_mapeado():
    from auth import timeout_for_role
    assert timeout_for_role("Contabilidad") == timedelta(hours=4)
    assert timeout_for_role("Gerente") == timedelta(hours=8)
    assert timeout_for_role("Lectura") == timedelta(hours=24)
    assert timeout_for_role("Bodega") == timedelta(hours=12)


def test_timeout_rol_desconocido_cae_al_default():
    from auth import SESSION_TIMEOUT_DEFAULT, timeout_for_role
    assert timeout_for_role("RolInexistente") == SESSION_TIMEOUT_DEFAULT
    assert timeout_for_role(None) == SESSION_TIMEOUT_DEFAULT
    assert timeout_for_role("") == SESSION_TIMEOUT_DEFAULT


def test_parse_last_activity_tolera_basura():
    from auth import _parse_last_activity
    assert _parse_last_activity(None) is None
    assert _parse_last_activity("") is None
    assert _parse_last_activity("not a date") is None
    # Naive ISO → se asume UTC
    r = _parse_last_activity("2026-04-17T10:00:00")
    assert r is not None
    assert r.tzinfo is not None
    assert r.tzinfo.utcoffset(r) == timedelta(0)


def test_parse_last_activity_aware_ok():
    from auth import _parse_last_activity
    r = _parse_last_activity("2026-04-17T10:00:00+00:00")
    assert r == datetime(2026, 4, 17, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tests del flujo completo en load_logged_in_user — se requiere una app Flask.
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_stub_db(monkeypatch):
    """App Flask con db.* mockeado: usuario existe, permisos vacíos."""
    import auth
    import db as db_mod

    def _fetch_one(sql, params=None):
        s = " ".join(sql.split()).lower()
        if "from seguridad.usuario" in s:
            return {
                "id_usuario": 1,
                "username": "u",
                "id_rol": 2,
                "activo": True,
                "nombre_rol": "Contabilidad",  # timeout = 4h
            }
        return None

    def _fetch_all(sql, params=None):
        return []

    monkeypatch.setattr(db_mod, "fetch_one", _fetch_one)
    monkeypatch.setattr(db_mod, "fetch_all", _fetch_all)

    from flask import Flask
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test"
    app.register_blueprint(auth.auth_bp)
    app.before_request(auth.load_logged_in_user)

    @app.route("/ping")
    def ping():
        from flask import g
        return "anon" if not g.get("user") else g.user["username"]

    @app.route("/healthz")
    def healthz():
        return "ok"

    return app, auth


def test_sesion_expirada_se_limpia(app_with_stub_db, monkeypatch):
    app, auth = app_with_stub_db
    # Congelar now 5h después del last_activity — Contabilidad expira a las 4h.
    fijo = datetime(2026, 4, 17, 15, 0, tzinfo=UTC)
    monkeypatch.setattr(auth, "_now_utc", lambda: fijo)

    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = 1
            sess["last_activity"] = "2026-04-17T10:00:00+00:00"  # -5h
        resp = c.get("/ping")
        # g.user es None → el view devuelve "anon"
        assert resp.data == b"anon"
        # La sesión quedó limpia (no más user_id)
        with c.session_transaction() as sess:
            assert "user_id" not in sess


def test_sesion_dentro_del_timeout_actualiza_last_activity(app_with_stub_db, monkeypatch):
    app, auth = app_with_stub_db
    fijo = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(auth, "_now_utc", lambda: fijo)

    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = 1
            sess["last_activity"] = "2026-04-17T10:00:00+00:00"  # -2h, dentro
        resp = c.get("/ping")
        assert resp.data == b"u"
        with c.session_transaction() as sess:
            assert sess["user_id"] == 1
            # last_activity se actualizó a `fijo`
            assert sess["last_activity"] == fijo.isoformat()


def test_healthz_no_resetea_last_activity(app_with_stub_db, monkeypatch):
    """Un GET /healthz no debe reiniciar el contador."""
    app, auth = app_with_stub_db
    fijo = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(auth, "_now_utc", lambda: fijo)

    inicial = "2026-04-17T10:00:00+00:00"
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = 1
            sess["last_activity"] = inicial
        c.get("/healthz")
        with c.session_transaction() as sess:
            # No se actualizó
            assert sess["last_activity"] == inicial


def test_sesion_sin_last_activity_no_expira_inmediata(app_with_stub_db, monkeypatch):
    """Una sesión legacy sin last_activity (creada antes de este patch)
    no debe deslogear al usuario en el primer request — simplemente se le
    pone un last_activity = now y sigue."""
    app, auth = app_with_stub_db
    fijo = datetime(2026, 4, 17, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(auth, "_now_utc", lambda: fijo)

    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = 1
            # ← sin last_activity
        resp = c.get("/ping")
        assert resp.data == b"u"
        with c.session_transaction() as sess:
            assert sess["last_activity"] == fijo.isoformat()
