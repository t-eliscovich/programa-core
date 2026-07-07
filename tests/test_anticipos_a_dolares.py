"""TMT 2026-07-06 (dueña): "/anticipos/ borrar, tiene que ser /dolares".

- El blueprint anticipos queda reducido a redirects de compatibilidad.
- El alta (nuevo_anticipo) y la cancelación (cancelar_anticipo) viven ahora
  en modules/dolares, con el MISMO permiso de escritura (facturas.crear).
- dolares.lista acepta informes.ver O facturas.ver (patrón granular
  /informes/deudas) para no dejar afuera a quien hoy entraba por /anticipos.
"""
from __future__ import annotations


def _login(app, fake_db, perms):
    rid = fake_db.add_role("Tester", perms)
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


# ---------------------------------------------------------------------------
# /anticipos/* → redirects de compatibilidad
# ---------------------------------------------------------------------------

def test_anticipos_lista_redirige_a_dolares(app, fake_db):
    c = _login(app, fake_db, ["facturas.ver"])
    r = c.get("/anticipos/")
    assert r.status_code == 302
    assert "/dolares" in r.headers["Location"]


def test_anticipos_nuevo_post_redirige_a_dolares(app, fake_db):
    c = _login(app, fake_db, ["facturas.crear"])
    r = c.post("/anticipos/nuevo", data={"cta": "ABC", "importe": "10"})
    assert r.status_code == 302
    assert "/dolares" in r.headers["Location"]


def test_anticipos_cancelar_post_redirige_a_dolares(app, fake_db):
    c = _login(app, fake_db, ["facturas.crear"])
    r = c.post("/anticipos/123/cancelar", data={})
    assert r.status_code == 302
    assert "/dolares" in r.headers["Location"]


# ---------------------------------------------------------------------------
# Acceso a /dolares — informes.ver O facturas.ver
# ---------------------------------------------------------------------------

def test_dolares_lista_con_facturas_ver_200(app, fake_db):
    """Quien hoy usaba /anticipos (facturas.ver, ej. Bodega/Ventas) entra."""
    c = _login(app, fake_db, ["facturas.ver"])
    r = c.get("/dolares")
    assert r.status_code == 200


def test_dolares_lista_con_informes_ver_200(app, fake_db):
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/dolares")
    assert r.status_code == 200


def test_dolares_lista_sin_permiso_404(app, fake_db):
    c = _login(app, fake_db, ["stock.ver"])
    r = c.get("/dolares")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Alta / cancelar viven en dolares — permiso de escritura intacto
# ---------------------------------------------------------------------------

def _patch_anticipo_deps(monkeypatch):
    """TMT 2026-07-07: el alta ahora crea una ND en Pichincha + mov_doble
    dentro de db.tx(). Mockeamos esas dependencias para el test unitario y
    capturamos el INSERT (que ahora va por execute_returning)."""
    import contextlib
    import bank_helpers
    import db as dbmod
    import mov_doble as _md
    import periodo_guard
    ins: list[tuple] = []

    @contextlib.contextmanager
    def _fake_tx():
        yield object()

    monkeypatch.setattr(dbmod, "tx", _fake_tx)
    monkeypatch.setattr(
        dbmod, "execute_returning",
        lambda sql, params=None, conn=None: (ins.append((sql, params)) or {"id_dolares": 1}),
    )
    monkeypatch.setattr(dbmod, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(dbmod, "fetch_one", lambda *a, **k: {"no_banco": 10, "no_cta": None})
    monkeypatch.setattr(bank_helpers, "insert_movimiento_bancario",
                        lambda *a, **k: {"id_transaccion": 99})
    monkeypatch.setattr(_md, "registrar", lambda **k: 1)
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **k: None)
    return ins


def test_alta_anticipo_vive_en_dolares_e_inserta(app, fake_db, monkeypatch):
    c = _login(app, fake_db, ["facturas.crear"])
    ins = _patch_anticipo_deps(monkeypatch)  # después del login (usa fake_db)
    r = c.post("/dolares/nuevo-anticipo", data={
        "fecha": "2026-07-06", "cta": "abc",
        "concepto": "anticipo test", "importe": "1.234,56",  # formato EU
    })
    assert r.status_code == 302
    assert "/dolares" in r.headers["Location"]
    inserts = [x for x in ins if "INSERT INTO scintela.dolares" in x[0]]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert params[1] == "ABC"          # cta upper
    assert params[3] == 1234.56        # parse EU: 1.234,56 → 1234.56
    assert "' '" in sql                # ST=' ' literal = vivo (suma a ANTICIPOS)


def test_alta_anticipo_sin_datos_no_inserta(app, fake_db, monkeypatch):
    c = _login(app, fake_db, ["facturas.crear"])
    ins = _patch_anticipo_deps(monkeypatch)
    r = c.post("/dolares/nuevo-anticipo", data={"cta": "", "importe": "0"})
    assert r.status_code == 302
    # Falta cta/importe → return temprano, no inserta en scintela.dolares.
    assert not [x for x in ins if "INSERT INTO scintela.dolares" in x[0]]


def test_alta_anticipo_sin_permiso_escritura_404(app, fake_db):
    """facturas.ver alcanza para VER /dolares pero no para dar de alta."""
    c = _login(app, fake_db, ["facturas.ver"])
    r = c.post("/dolares/nuevo-anticipo", data={"cta": "ABC", "importe": "10"})
    assert r.status_code == 404


def test_cancelar_anticipo_vive_en_dolares(app, fake_db, monkeypatch):
    import db as dbmod
    ejecutados: list[tuple] = []
    monkeypatch.setattr(
        dbmod, "execute",
        lambda sql, params=None, conn=None: ejecutados.append((sql, params)) or 1,
    )
    c = _login(app, fake_db, ["facturas.crear"])
    r = c.post("/dolares/anticipo/77/cancelar", data={})
    assert r.status_code == 302
    updates = [x for x in ejecutados if "SET st = 'B'" in x[0]]
    assert len(updates) == 1
    assert updates[0][1] == (77,)


def test_cancelar_anticipo_sin_permiso_escritura_404(app, fake_db):
    c = _login(app, fake_db, ["facturas.ver"])
    r = c.post("/dolares/anticipo/77/cancelar", data={})
    assert r.status_code == 404
