"""Unit tests for login + session + permission decorators.

These run against the FakeDB fixture — no real Postgres needed.
"""
import bcrypt
import pytest


def _hash(pw: str) -> bytes:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4))


def _seed(fake_db):
    rid = fake_db.add_role("Dueño", ["*"])
    uid = fake_db.add_user("tmt", _hash("secret123"), rid)
    return rid, uid


def test_login_success(app, client, fake_db):
    _seed(fake_db)
    r = client.post("/login", data={"username": "tmt", "password": "secret123"})
    assert r.status_code in (302, 303)
    with client.session_transaction() as s:
        assert s.get("user_id") is not None


def test_login_wrong_password(app, client, fake_db):
    _seed(fake_db)
    r = client.post("/login", data={"username": "tmt", "password": "nope"})
    assert r.status_code == 401
    with client.session_transaction() as s:
        assert "user_id" not in s


def test_login_unknown_user(app, client, fake_db):
    _seed(fake_db)
    r = client.post("/login", data={"username": "ghost", "password": "whatever"})
    assert r.status_code == 401


def test_login_inactive_user(app, client, fake_db):
    rid = fake_db.add_role("Dueño", ["*"])
    fake_db.add_user("inactive", _hash("pw123456"), rid, activo=False)
    r = client.post("/login", data={"username": "inactive", "password": "pw123456"})
    assert r.status_code == 401


def test_login_missing_fields(app, client, fake_db):
    r = client.post("/login", data={"username": "", "password": ""})
    assert r.status_code == 400


def test_login_is_case_insensitive_on_username(app, client, fake_db):
    _seed(fake_db)
    r = client.post("/login", data={"username": "TMT", "password": "secret123"})
    assert r.status_code in (302, 303)


def test_logout_clears_session(app, client, fake_db):
    _seed(fake_db)
    client.post("/login", data={"username": "tmt", "password": "secret123"})
    r = client.get("/logout")
    assert r.status_code in (302, 303)
    with client.session_transaction() as s:
        assert "user_id" not in s


def test_requiere_login_redirects(client):
    r = client.get("/tablero/")
    assert r.status_code in (302, 303)
    assert "/login" in r.headers["Location"]


def test_dueno_wildcard_permission(app, fake_db):
    """`*` should satisfy any requiere_permiso check."""
    from auth import tiene_permiso
    fake_db.add_role("Dueño", ["*"])
    with app.test_request_context():
        from flask import g
        g.user = {"username": "tmt"}
        g.permisos = {"*"}
        assert tiene_permiso("facturas.anular")
        assert tiene_permiso("cualquier.cosa")


def test_permiso_denied_when_missing(app, fake_db):
    from auth import tiene_permiso
    fake_db.add_role("Ventas", ["clientes.ver"])
    with app.test_request_context():
        from flask import g
        g.user = {"username": "v"}
        g.permisos = {"clientes.ver"}
        assert tiene_permiso("clientes.ver")
        assert not tiene_permiso("facturas.anular")


# =====================================================================
# Regression: el login debe tolerar que la migración 0008 no se haya
# corrido (columnas 2FA / password_debe_cambiar todavía no existen).
# Ver `auth.login()` — intenta SELECT con cols nuevas, fallback al viejo
# si la DB levanta UndefinedColumn.
# =====================================================================

def test_login_funciona_sin_migracion_0008(app, fake_db, monkeypatch, client):
    """Simula UndefinedColumn en el primer SELECT y verifica que el fallback
    loguea al usuario igual. Es la clase de error que rompió el deploy de
    la user el 2026-04-17 por no haber corrido `scripts/migrate.py`."""
    import bcrypt

    import db

    rid = fake_db.add_role("Dueño", ["*"])
    pw_hash = bcrypt.hashpw(b"secretpw", bcrypt.gensalt())
    fake_db.add_user("admin", pw_hash, rid)

    orig_fetch_one = db.fetch_one
    call_count = {"n": 0}
    def _fetch(sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        # Primer SELECT de login: tiene totp_secret en la lista de cols.
        if "totp_secret" in s:
            call_count["n"] += 1
            raise Exception('column "totp_secret" does not exist')
        return orig_fetch_one(sql, params, conn)

    monkeypatch.setattr(db, "fetch_one", _fetch)

    r = client.post("/login", data={"username": "admin", "password": "secretpw"},
                    follow_redirects=False)
    # Esperamos redirect al dashboard (302), no 500.
    assert r.status_code in (302, 303), f"status={r.status_code} body={r.data[:300]}"
    assert "/tablero" in r.headers.get("Location", "")
    assert call_count["n"] == 1  # el intento original falló
