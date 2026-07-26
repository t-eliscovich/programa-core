"""Prueba "POR UI" de cada operación de cheque: hace los POST exactos a las
RUTAS reales que disparan los botones/dropdowns de /cheques, con un test client
de Flask autenticado (usuario + permiso '*'), contra Postgres real.

Verifica el camino completo pantalla → ruta → vista → función → DB (permisos,
parseo de form, redirect), no solo la función. pytest -m db.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

HOY = date.today()


@pytest.fixture
def real_app(migrated_db):
    """App Flask apuntando a la DB de prueba real, CSRF y rate-limit off."""
    from app import create_app
    from extensions import limiter

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    limiter.enabled = False
    return app


def _seed_auth(conn) -> int:
    """Crea rol con permiso '*' + usuario activo. Devuelve id_usuario."""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO seguridad.rol (nombre_rol, descripcion) VALUES ('test-admin','t') "
        "ON CONFLICT (nombre_rol) DO UPDATE SET descripcion=EXCLUDED.descripcion "
        "RETURNING id_rol"
    )
    id_rol = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO seguridad.permiso (id_rol, nombre_opcion) VALUES (%s,'*') "
        "ON CONFLICT DO NOTHING",
        (id_rol,),
    )
    cur.execute(
        """
        INSERT INTO seguridad.usuario (username, password_hash, id_rol, activo)
        VALUES ('uitest', 'x', %s, TRUE)
        ON CONFLICT (username) DO UPDATE SET id_rol=EXCLUDED.id_rol, activo=TRUE
        RETURNING id_usuario
        """,
        (id_rol,),
    )
    return cur.fetchone()[0]


@pytest.fixture
def auth_client(real_app, real_db_conn):
    id_usuario = _seed_auth(real_db_conn)
    real_db_conn.commit()
    client = real_app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = id_usuario
        sess["last_activity"] = __import__("datetime").datetime.now(
            __import__("datetime").UTC
        ).isoformat()
    return client


# ── helpers de datos (usan el módulo db, misma DB que la app) ───────────
def _db():
    import db
    return db


def _seed_cliente_banco():
    db = _db()
    db.execute("INSERT INTO scintela.cliente (codigo_cli, nombre, stop, pago, cupo, activo) "
               "VALUES ('UIU','UI test','N',30,100000,TRUE) ON CONFLICT (codigo_cli) DO NOTHING")
    for nb, nom in ((1, "PICHINCHA"), (2, "INTERNACIONAL")):
        db.execute("INSERT INTO scintela.banco (no_banco, nombre) VALUES (%s,%s) "
                   "ON CONFLICT (no_banco) DO NOTHING", (nb, nom))


def _seed_cheque(no_cheque, stat="Z", importe=500.0):
    db = _db()
    row = db.execute_returning(
        "INSERT INTO scintela.cheque (fecha, no_cheque, codigo_cli, importe, fechad, stat, no_banco, pasaconta) "
        "VALUES (%s,%s,'UIU',%s,%s,%s,1,'0') RETURNING id_cheque",
        (HOY, no_cheque, importe, HOY - timedelta(days=1), stat),
    )
    return row["id_cheque"]


def _seed_factura_aplicada(numf, importe, id_cheque):
    db = _db()
    row = db.execute_returning(
        "INSERT INTO scintela.factura (numf, fecha, codigo_cli, kg, importe, abono, saldo, stat, tipo) "
        "VALUES (%s,%s,'UIU',0,%s,%s,0,'T','1') RETURNING id_factura",
        (numf, HOY, importe, importe),
    )
    id_fact = row["id_factura"]
    db.execute("INSERT INTO scintela.chequesxfact (id_cheque, id_fact, fechaing, codigo_cli, importe, no_banco) "
               "VALUES (%s,%s,%s,'UIU',%s,1)", (id_cheque, id_fact, HOY, importe))
    return id_fact


def _stat(id_cheque):
    return _db().fetch_one("SELECT stat FROM scintela.cheque WHERE id_cheque=%s", (id_cheque,))["stat"]


def _factura(id_fact):
    return _db().fetch_one("SELECT abono, saldo, stat FROM scintela.factura WHERE id_factura=%s", (id_fact,))


def _cxf(id_cheque):
    return _db().fetch_one("SELECT COUNT(*) AS n FROM scintela.chequesxfact WHERE id_cheque=%s", (id_cheque,))["n"]


def _de_movs(no_cheque, doc):
    return _db().fetch_all(
        "SELECT importe FROM scintela.transacciones_bancarias WHERE documento=%s AND concepto ILIKE %s",
        (doc, f"%{no_cheque}%"),
    )


@pytest.fixture(autouse=True)
def _limpiar(migrated_db):
    _seed_cliente_banco()
    db = _db()
    db.execute("DELETE FROM scintela.transacciones_bancarias WHERE concepto LIKE '%%U000%%'")
    db.execute("DELETE FROM scintela.chequextransaccion WHERE id_cheque IN "
               "(SELECT id_cheque FROM scintela.cheque WHERE no_cheque LIKE 'U000%%')")
    db.execute("DELETE FROM scintela.chequesxfact WHERE codigo_cli='UIU'")
    db.execute("DELETE FROM scintela.cheque WHERE no_cheque LIKE 'U000%%'")
    db.execute("DELETE FROM scintela.factura WHERE codigo_cli='UIU'")


# ── 1. Depositar (dropdown → transicionar stat_destino=B) ───────────────
@pytest.mark.db
def test_ui_depositar_Z_a_B(auth_client):
    idc = _seed_cheque("U0001", stat="Z", importe=500)
    r = auth_client.post(f"/cheques/{idc}/transicionar", data={"stat_destino": "B", "no_banco": "1"})
    assert r.status_code in (302, 303)
    assert _stat(idc) == "B"
    assert len(_de_movs("U0001", "DE")) == 1


# ── 2. Marcar devuelto (dropdown → transicionar stat_destino=1) ─────────
@pytest.mark.db
def test_ui_marcar_devuelto_Z_a_1(auth_client):
    idc = _seed_cheque("U0002", stat="Z")
    r = auth_client.post(f"/cheques/{idc}/transicionar", data={"stat_destino": "1"})
    assert r.status_code in (302, 303)
    assert _stat(idc) == "1"


# ── 3. Re-depositar devuelto individual (dropdown → transicionar V) ─────
@pytest.mark.db
def test_ui_redepositar_1_a_V_crea_banco(auth_client):
    idc = _seed_cheque("U0003", stat="1", importe=750)
    r = auth_client.post(f"/cheques/{idc}/transicionar", data={"stat_destino": "V", "no_banco": "1"})
    assert r.status_code in (302, 303)
    assert _stat(idc) == "V"
    assert len(_de_movs("U0003", "DE")) == 1


# ── 4. Depositar lote un devuelto (ruta _api/depositar-lote) ────────────
@pytest.mark.db
def test_ui_depositar_lote_devuelto_a_V(auth_client):
    idc = _seed_cheque("U0004", stat="1")
    r = auth_client.post("/cheques/_api/depositar-lote", json={"ids": [idc], "no_banco": 1})
    assert r.status_code in (200, 302, 303)
    assert _stat(idc) == "V"


# ── 5. Rebote real (botón "Marcar como rebotado" → reversar) ───────────
@pytest.mark.db
def test_ui_rebote_real_no_reabre_factura(auth_client):
    idc = _seed_cheque("U0005", stat="Z", importe=1000)
    idf = _seed_factura_aplicada(600005, 1000, idc)
    # el usuario deposita primero (crea el DE)
    auth_client.post(f"/cheques/{idc}/transicionar", data={"stat_destino": "B", "no_banco": "1"})
    assert _stat(idc) == "B"
    r = auth_client.post(f"/cheques/{idc}/reversar", data={"motivo": "rebote banco"})
    assert r.status_code in (302, 303)
    assert _stat(idc) == "1"
    assert len(_de_movs("U0005", "ND")) >= 1  # ND en el banco
    f = _factura(idf)
    assert abs(float(f["abono"]) - 1000) < 0.01 and f["stat"] == "T"  # factura NO reabierta
    assert _cxf(idc) == 1  # aplicación sigue viva


# ── 6. Anular por error de carga (ruta anular-error-carga) ─────────────
@pytest.mark.db
def test_ui_anular_error_carga_reabre_factura(auth_client):
    idc = _seed_cheque("U0006", stat="Z", importe=1000)
    idf = _seed_factura_aplicada(600006, 1000, idc)
    r = auth_client.post(f"/cheques/{idc}/anular-error-carga", data={"motivo": "error"})
    assert r.status_code in (302, 303)
    assert _stat(idc) == "X"
    f = _factura(idf)
    assert abs(float(f["abono"])) < 0.01 and f["stat"] == "Z"  # factura reabierta
    assert _cxf(idc) == 0  # aplicación borrada


# ── 7. Postergar (dropdown → transicionar stat_destino=P) ──────────────
@pytest.mark.db
def test_ui_postergar_Z_a_P(auth_client):
    idc = _seed_cheque("U0007", stat="Z")
    r = auth_client.post(f"/cheques/{idc}/transicionar",
                         data={"stat_destino": "P", "fecha_postergada": (HOY + timedelta(days=10)).isoformat()})
    assert r.status_code in (302, 303)
    assert _stat(idc) == "P"


# ── 8. Cobrar en efectivo (dropdown → transicionar stat_destino=C) ─────
@pytest.mark.db
def test_ui_cobrar_efectivo_Z_a_C(auth_client):
    idc = _seed_cheque("U0008", stat="Z", importe=300)
    r = auth_client.post(f"/cheques/{idc}/transicionar", data={"stat_destino": "C"})
    assert r.status_code in (302, 303)
    assert _stat(idc) == "C"
