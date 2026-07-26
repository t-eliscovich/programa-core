"""Gating de las rutas de cheques — comportamiento INTENCIONAL (decisión dueña).

Decisión de la dueña (2026-07-11, ver detalle.html "all users can do it"): el
cambio de estado del cheque (depositar, caja, marcar devuelto, re-depositar, sin
fondos) lo puede hacer CUALQUIER usuario logueado — el dropdown NO tiene gate de
permiso. Las rutas transicionar / reversar / anular-error-carga sólo piden
`@requiere_login`, a propósito.

El ÚNICO candado de permiso real es `cheques.aplicar` sobre el depósito por LOTE
(`/cheques/_api/depositar-lote`).

Estos tests fijan ese contrato para que nadie vuelva a agregar un
`@requiere_permiso` por error (lo cual dejaría a usuarios como Andres/Alex con un
404 en operaciones que hoy pueden hacer):

  - un usuario logueado con permisos mínimos SÍ puede transicionar (depositar);
  - depositar-lote SIN `cheques.aplicar` → 404, y CON el permiso procede;
  - sin login → redirect a /login.

Correr: pytest -m db tests/test_cheques_permisos_rutas.py -q
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

HOY = date.today()


@pytest.fixture
def real_app(migrated_db):
    from app import create_app
    from extensions import limiter

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    limiter.enabled = False
    return app


def _seed_rol_usuario(conn, *, rol_nombre: str, permisos: set[str]) -> int:
    """Crea (o resetea) un rol con EXACTAMENTE `permisos` + un usuario activo.
    Devuelve id_usuario."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO seguridad.rol (nombre_rol, descripcion) VALUES (%s,'t') "
        "ON CONFLICT (nombre_rol) DO UPDATE SET descripcion=EXCLUDED.descripcion "
        "RETURNING id_rol",
        (rol_nombre,),
    )
    id_rol = cur.fetchone()[0]
    cur.execute("DELETE FROM seguridad.permiso WHERE id_rol=%s", (id_rol,))
    for opt in permisos:
        cur.execute(
            "INSERT INTO seguridad.permiso (id_rol, nombre_opcion) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (id_rol, opt),
        )
    username = f"u_{rol_nombre}"[:40]
    cur.execute(
        "INSERT INTO seguridad.usuario (username, password_hash, id_rol, activo) "
        "VALUES (%s,'x',%s,TRUE) "
        "ON CONFLICT (username) DO UPDATE SET id_rol=EXCLUDED.id_rol, activo=TRUE "
        "RETURNING id_usuario",
        (username, id_rol),
    )
    return cur.fetchone()[0]


def _client_con(real_app, real_db_conn, *, rol_nombre, permisos):
    id_usuario = _seed_rol_usuario(real_db_conn, rol_nombre=rol_nombre, permisos=permisos)
    real_db_conn.commit()
    client = real_app.test_client()
    import datetime as _dt

    with client.session_transaction() as sess:
        sess["user_id"] = id_usuario
        sess["last_activity"] = _dt.datetime.now(_dt.UTC).isoformat()
    return client


def _db():
    import db
    return db


def _seed_cheque(no_cheque, stat="Z", importe=500.0):
    db = _db()
    db.execute("INSERT INTO scintela.cliente (codigo_cli, nombre, stop, pago, cupo, activo) "
               "VALUES ('PRM','Perm test','N',30,100000,TRUE) ON CONFLICT (codigo_cli) DO NOTHING")
    for nb, nom in ((1, "PICHINCHA"), (2, "INTERNACIONAL")):
        db.execute("INSERT INTO scintela.banco (no_banco, nombre) VALUES (%s,%s) "
                   "ON CONFLICT (no_banco) DO NOTHING", (nb, nom))
    row = db.execute_returning(
        "INSERT INTO scintela.cheque (fecha, no_cheque, codigo_cli, importe, fechad, stat, no_banco, pasaconta) "
        "VALUES (%s,%s,'PRM',%s,%s,%s,1,'0') RETURNING id_cheque",
        (HOY, no_cheque, importe, HOY - timedelta(days=1), stat),
    )
    return row["id_cheque"]


def _stat(id_cheque):
    return _db().fetch_one("SELECT stat FROM scintela.cheque WHERE id_cheque=%s", (id_cheque,))["stat"]


@pytest.fixture(autouse=True)
def _limpiar(migrated_db):
    db = _db()
    db.execute("DELETE FROM scintela.chequextransaccion WHERE id_cheque IN "
               "(SELECT id_cheque FROM scintela.cheque WHERE no_cheque LIKE 'P000%%')")
    db.execute("DELETE FROM scintela.transacciones_bancarias WHERE concepto LIKE '%%P000%%'")
    db.execute("DELETE FROM scintela.cheque WHERE no_cheque LIKE 'P000%%'")


# ── transicionar: cualquier usuario logueado puede (decisión dueña) ────
@pytest.mark.db
def test_transicionar_lo_puede_cualquier_usuario_logueado(real_app, real_db_conn):
    """Un usuario SIN permisos de cheques (solo 'cheques.ver') igual puede depositar
    por la ruta individual — es la decisión explícita de la dueña ("all users can
    do it"). Si alguien agrega un @requiere_permiso acá, este test se pone rojo."""
    idc = _seed_cheque("P0001", stat="Z")
    client = _client_con(real_app, real_db_conn, rol_nombre="rol-min",
                         permisos={"cheques.ver"})
    r = client.post(f"/cheques/{idc}/transicionar", data={"stat_destino": "B", "no_banco": "1"})
    assert r.status_code in (302, 303)
    assert _stat(idc) == "B"


# ── reversar: cualquier usuario logueado puede (dropdown "sin fondos") ─
@pytest.mark.db
def test_reversar_lo_puede_cualquier_usuario_logueado(real_app, real_db_conn):
    idc = _seed_cheque("P0002", stat="Z")
    client = _client_con(real_app, real_db_conn, rol_nombre="rol-min2",
                         permisos={"cheques.ver"})
    r = client.post(f"/cheques/{idc}/reversar", data={"motivo": "x"})
    assert r.status_code in (302, 303)
    assert _stat(idc) == "X"  # Z→X administrativo


# ── depositar-lote: SIN cheques.aplicar → 404 (único candado real) ─────
@pytest.mark.db
def test_depositar_lote_sin_aplicar_404(real_app, real_db_conn):
    idc = _seed_cheque("P0003", stat="Z")
    client = _client_con(real_app, real_db_conn, rol_nombre="rol-sin-apl",
                         permisos={"cheques.ver"})
    r = client.post("/cheques/_api/depositar-lote", json={"ids": [idc], "no_banco": 1})
    assert r.status_code == 404
    assert _stat(idc) == "Z"  # no se depositó


# ── depositar-lote: CON cheques.aplicar → procede ─────────────────────
@pytest.mark.db
def test_depositar_lote_con_aplicar_ok(real_app, real_db_conn):
    idc = _seed_cheque("P0004", stat="Z")
    client = _client_con(real_app, real_db_conn, rol_nombre="rol-con-apl",
                         permisos={"cheques.aplicar"})
    r = client.post("/cheques/_api/depositar-lote", json={"ids": [idc], "no_banco": 1})
    assert r.status_code in (200, 302, 303)
    assert _stat(idc) == "B"


# ── sin login → redirect a /login, sin mutar ──────────────────────────
@pytest.mark.db
def test_transicionar_sin_login_redirect(real_app):
    idc = _seed_cheque("P0005", stat="Z")
    client = real_app.test_client()  # SIN session
    r = client.post(f"/cheques/{idc}/transicionar", data={"stat_destino": "B", "no_banco": "1"})
    assert r.status_code in (301, 302, 303)
    assert "/login" in r.headers.get("Location", "")
    assert _stat(idc) == "Z"
