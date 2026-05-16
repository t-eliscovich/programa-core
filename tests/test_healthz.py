"""Tests del endpoint /healthz (liveness + readiness)."""
from __future__ import annotations


def test_liveness_responde_200(app):
    c = app.test_client()
    r = c.get("/healthz")
    assert r.status_code == 200
    data = r.get_json()
    assert data["status"] == "ok"
    assert "ts" in data


def test_liveness_trailing_slash_tambien_responde(app):
    c = app.test_client()
    r = c.get("/healthz/")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


def test_readiness_con_db_viva_responde_200(app, fake_db):
    """El fake_db devuelve [] en fetch_all pero fetch_one devuelve None por
    default. Para que readiness sea OK necesitamos que fetch_one devuelva
    {'ok': 1}."""
    import db
    c = app.test_client()
    orig = db.fetch_one
    db.fetch_one = lambda *a, **kw: {"ok": 1}
    try:
        r = c.get("/healthz/ready")
        assert r.status_code == 200
        data = r.get_json()
        assert data["status"] == "ok"
        assert data["db"] == "connected"
        assert "latency_ms" in data
    finally:
        db.fetch_one = orig


def test_readiness_con_db_caida_responde_503(app, fake_db):
    import db
    c = app.test_client()
    orig = db.fetch_one
    def _boom(*a, **kw):
        raise Exception("connection refused")
    db.fetch_one = _boom
    try:
        r = c.get("/healthz/ready")
        assert r.status_code == 503
        data = r.get_json()
        assert data["status"] == "degraded"
        assert data["db"] == "unreachable"
    finally:
        db.fetch_one = orig


def test_readiness_row_inesperado_marca_degraded(app, fake_db):
    """Si el SELECT devuelve algo raro (DB corrupta o confundida), también degraded."""
    import db
    c = app.test_client()
    orig = db.fetch_one
    db.fetch_one = lambda *a, **kw: {"ok": 999}  # no es 1
    try:
        r = c.get("/healthz/ready")
        assert r.status_code == 503
    finally:
        db.fetch_one = orig


def test_healthz_no_requiere_auth(app, fake_db):
    """/healthz es path de skip — ni siquiera requiere sesión."""
    c = app.test_client()
    # sin setear s["user_id"]
    r = c.get("/healthz")
    assert r.status_code == 200


def test_healthz_no_bloqueado_por_ip_allowlist(app, fake_db, monkeypatch):
    """Aún con allowlist estricta, /healthz nunca se bloquea."""
    import ip_allowlist as ial
    monkeypatch.setenv("ROLE_IP_ALLOWLIST_DUENO", "1.2.3.4")
    ial._parse_allowlist.cache_clear()
    rid = fake_db.add_role("Dueño", ["*"])
    fake_db.add_user("admin", b"$2b$12$fake", rid)

    c = app.test_client()
    r = c.get("/healthz")
    assert r.status_code == 200
