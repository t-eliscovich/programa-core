"""El paquete PDF de cierre de mes (`modules/informes/cierres_paquete.py`).

TMT 2026-08-31: reemplazo del Word que se armaba a mano pegando capturas del
dBase al cerrar el mes (ver FEBRERO.docx). Se genera solo, dentro de
`crear_snapshot_historia()`, y sólo en la rama LIVE.

Tests de vista (`/informes/cierres*`) van con el patrón fake_db estándar
(ver `test_anticipos_a_dolares.py`), mockeando `cierres_paquete.listar` /
`.obtener` -- no hace falta la tabla real para probar permiso, template y
headers de la descarga. Los tests de la lógica de armado (`armar_pdf`,
`guardar`/`listar`/`obtener` reales, el hook en `crear_snapshot_historia`)
van marcados `@pytest.mark.db` porque escriben/leen `scintela.cierre_paquete`
de verdad.
"""
from __future__ import annotations

import io
from contextlib import contextmanager

import pytest

from modules.informes import cierres_paquete


def _login(app, fake_db, perms):
    rid = fake_db.add_role("Tester", perms)
    uid = fake_db.add_user("test", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _pdf_de_una_pagina() -> bytes:
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# /informes/cierres — lista
# ---------------------------------------------------------------------------

def test_cierres_lista_con_informes_ver_200(app, fake_db, monkeypatch):
    monkeypatch.setattr(cierres_paquete, "listar", lambda: [])
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/informes/cierres")
    assert r.status_code == 200
    assert "Todav" in r.get_data(as_text=True)  # "Todavía no se generó..."


def test_cierres_lista_sin_permiso_404(app, fake_db):
    c = _login(app, fake_db, ["stock.ver"])
    r = c.get("/informes/cierres")
    assert r.status_code == 404


def test_cierres_lista_muestra_las_filas(app, fake_db, monkeypatch):
    import datetime
    monkeypatch.setattr(cierres_paquete, "listar", lambda: [{
        "anio": 2026, "mes": 8, "mes_nombre": "agosto",
        "tamano_bytes": 512_000, "paginas": 8,
        "generado_en": datetime.datetime(2026, 9, 1, 6, 5),
        "generado_por": "cron_snapshot_historia",
    }])
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/informes/cierres")
    body = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "agosto 2026" in body
    assert "8/8" in body


# ---------------------------------------------------------------------------
# /informes/cierres/<anio>/<mes>/pdf — descarga
# ---------------------------------------------------------------------------

def test_cierres_pdf_404_si_no_existe(app, fake_db, monkeypatch):
    monkeypatch.setattr(cierres_paquete, "obtener", lambda a, m: None)
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/informes/cierres/2026/8/pdf")
    assert r.status_code == 404


def test_cierres_pdf_descarga_bytes_con_nombre(app, fake_db, monkeypatch):
    contenido = b"%PDF-fake-bytes"
    monkeypatch.setattr(cierres_paquete, "obtener",
                        lambda a, m: contenido if (a, m) == (2026, 8) else None)
    c = _login(app, fake_db, ["informes.ver"])
    r = c.get("/informes/cierres/2026/8/pdf")
    assert r.status_code == 200
    assert r.data == contenido
    assert r.mimetype == "application/pdf"
    assert "agosto" in r.headers["Content-Disposition"]
    assert "2026" in r.headers["Content-Disposition"]
    assert "attachment" in r.headers["Content-Disposition"]


def test_cierres_pdf_sin_permiso_404(app, fake_db):
    c = _login(app, fake_db, ["stock.ver"])
    r = c.get("/informes/cierres/2026/8/pdf")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# generar_y_guardar — nunca revienta
# ---------------------------------------------------------------------------

def test_generar_y_guardar_sin_navegador_no_revienta(monkeypatch):
    from modules._lib import pdf_motor
    monkeypatch.setattr(pdf_motor, "binario", lambda: None)
    r = cierres_paquete.generar_y_guardar(2026, 8, usuario="test")
    assert r["aplicado"] is False
    assert "navegador" in r["razon"]


def test_generar_y_guardar_atrapa_cualquier_excepcion(monkeypatch):
    def _revienta(anio, mes):
        raise ValueError("boom")
    monkeypatch.setattr(cierres_paquete, "armar_pdf", _revienta)
    r = cierres_paquete.generar_y_guardar(2026, 8, usuario="test")
    assert r["aplicado"] is False
    assert "boom" in r["razon"]


# ---------------------------------------------------------------------------
# armar_pdf — el pipeline completo (marcado @pytest.mark.db: necesita la app
# real + Postgres real para el test client y el usuario '*').
# ---------------------------------------------------------------------------

@pytest.mark.db
def test_armar_pdf_pega_una_pagina_por_seccion(migrated_db, real_db_conn,
                                                monkeypatch):
    from modules._lib import pdf_motor

    conn = real_db_conn
    conn.autocommit = True
    cur = conn.cursor()
    # Un usuario activo con permiso '*' para que _usuario_sistema_id lo
    # encuentre (si ya existe uno de rol Accionista en el dump, no molesta:
    # ON CONFLICT/where activo alcanza con que exista AL MENOS uno).
    cur.execute("SELECT id_rol FROM seguridad.rol WHERE nombre_rol = 'Accionista'")
    row = cur.fetchone()
    assert row, "el dump legacy tiene que traer el rol Accionista"
    id_rol = row[0]
    cur.execute(
        "INSERT INTO seguridad.usuario (username, password_hash, id_rol, activo) "
        "VALUES ('cierre_test', '$2b$12$fakehash', %s, TRUE) "
        "ON CONFLICT (username) DO NOTHING",
        (id_rol,),
    )

    # Dos secciones baratas en vez de las 8 reales -- lo que se prueba acá es
    # el PEGADO, no cada pantalla (esas ya tienen sus propios tests).
    monkeypatch.setattr(cierres_paquete, "PAGINAS", (
        ("Deudas", "/informes/deudas"),
        ("Anticipos", "/dolares"),
    ))
    monkeypatch.setattr(pdf_motor, "disponible", lambda: True)
    monkeypatch.setattr(pdf_motor, "desde_html", lambda html, static_dir=None:
                        _pdf_de_una_pagina())

    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    with flask_app.app_context():
        pdf_bytes, ok = cierres_paquete.armar_pdf(2026, 8)

    assert ok == 2
    from pypdf import PdfReader
    assert len(PdfReader(io.BytesIO(pdf_bytes)).pages) == 2


@pytest.mark.db
def test_armar_pdf_sigue_si_una_seccion_falla(migrated_db, real_db_conn,
                                               monkeypatch):
    from modules._lib import pdf_motor

    conn = real_db_conn
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT id_rol FROM seguridad.rol WHERE nombre_rol = 'Accionista'")
    id_rol = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO seguridad.usuario (username, password_hash, id_rol, activo) "
        "VALUES ('cierre_test2', '$2b$12$fakehash', %s, TRUE) "
        "ON CONFLICT (username) DO NOTHING",
        (id_rol,),
    )

    monkeypatch.setattr(cierres_paquete, "PAGINAS", (
        ("No existe", "/esta-ruta-no-existe-nunca"),
        ("Deudas", "/informes/deudas"),
    ))
    monkeypatch.setattr(pdf_motor, "disponible", lambda: True)
    monkeypatch.setattr(pdf_motor, "desde_html", lambda html, static_dir=None:
                        _pdf_de_una_pagina())

    from app import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    with flask_app.app_context():
        pdf_bytes, ok = cierres_paquete.armar_pdf(2026, 8)

    assert ok == 1  # la ruta rota no tira todo el paquete


@pytest.mark.db
def test_guardar_listar_obtener_upsert(migrated_db):
    id1 = cierres_paquete.guardar(2025, 1, b"version-1", 8, "test")
    filas = cierres_paquete.listar()
    fila = next(f for f in filas if f["anio"] == 2025 and f["mes"] == 1)
    assert fila["paginas"] == 8
    assert cierres_paquete.obtener(2025, 1) == b"version-1"

    # Regrabar el mismo mes PISA, no acumula (mismo criterio que
    # scintela.historia -- ver crear_snapshot_historia forzar=True).
    id2 = cierres_paquete.guardar(2025, 1, b"version-2", 8, "test")
    assert id1 == id2
    assert cierres_paquete.obtener(2025, 1) == b"version-2"
    filas = [f for f in cierres_paquete.listar()
             if f["anio"] == 2025 and f["mes"] == 1]
    assert len(filas) == 1


# ---------------------------------------------------------------------------
# El hook en crear_snapshot_historia -- sólo dispara en la rama LIVE, y
# nunca tira abajo el snapshot si el paquete falla. Mismo estilo de fake_db
# que tests/test_cierre_mes_2026_08.py -- no hace falta Postgres real para
# probar ESTA lógica (armar_pdf ya se probó @pytest.mark.db más arriba).
# ---------------------------------------------------------------------------

@contextmanager
def _tx_dummy():
    yield object()


def _bal(componentes: dict) -> dict:
    return {"diagnostico": {"componentes": componentes}, "kg": {},
            "stock_subpanels": {}}


def test_snapshot_historia_dispara_el_paquete_en_rama_live(monkeypatch):
    from modules.informes import queries as iq

    ultimo_dia = __import__("datetime").date(2026, 8, 31)
    monkeypatch.setattr(iq, "today_ec", lambda: ultimo_dia)
    monkeypatch.setattr(iq, "informe_balance",
                        lambda *a, **k: _bal({"patr": 1.0, "usret": 0.0,
                                               "utilidad": 0.0}))
    monkeypatch.setattr(iq.db, "fetch_one", lambda *a, **k: None)  # sin foto previa
    monkeypatch.setattr(iq.db, "tx", _tx_dummy)
    monkeypatch.setattr(iq.db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(iq.db, "execute_returning",
                        lambda *a, **k: {"id_historia": 1})

    llamadas = []
    monkeypatch.setattr(
        cierres_paquete, "generar_y_guardar",
        lambda anio, mes, usuario="auto": llamadas.append((anio, mes)) or {},
    )

    r = iq.crear_snapshot_historia(2026, 8, usuario="test")

    assert r["aplicado"] is True
    assert llamadas == [(2026, 8)]  # SÍ dispara: se cierra HOY, el último día


def test_snapshot_historia_no_dispara_el_paquete_en_rama_as_of(monkeypatch):
    """Un backfill de un mes viejo no tiene de dónde sacar cartera/gastos de
    ESE mes -- mostraría el estado de HOY con el rótulo de un mes pasado."""
    from modules.informes import queries as iq

    # "Hoy" es agosto, pero se está grabando JULIO -> rama as_of.
    monkeypatch.setattr(iq, "today_ec",
                        lambda: __import__("datetime").date(2026, 8, 15))
    monkeypatch.setattr(iq, "informe_balance_as_of",
                        lambda *a, **k: _bal({"patr": 1.0, "usret": 0.0,
                                               "utilidad": 0.0}))
    monkeypatch.setattr(iq.db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(iq.db, "tx", _tx_dummy)
    monkeypatch.setattr(iq.db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(iq.db, "execute_returning",
                        lambda *a, **k: {"id_historia": 1})

    llamadas = []
    monkeypatch.setattr(
        cierres_paquete, "generar_y_guardar",
        lambda anio, mes, usuario="auto": llamadas.append((anio, mes)) or {},
    )

    r = iq.crear_snapshot_historia(2026, 7, usuario="test")

    assert r["aplicado"] is True
    assert llamadas == []  # NO dispara: es backfill/as-of


def test_snapshot_historia_no_revienta_si_el_paquete_falla(monkeypatch):
    from modules.informes import queries as iq

    monkeypatch.setattr(iq, "today_ec",
                        lambda: __import__("datetime").date(2026, 8, 31))
    monkeypatch.setattr(iq, "informe_balance",
                        lambda *a, **k: _bal({"patr": 1.0, "usret": 0.0,
                                               "utilidad": 0.0}))
    monkeypatch.setattr(iq.db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(iq.db, "tx", _tx_dummy)
    monkeypatch.setattr(iq.db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(iq.db, "execute_returning",
                        lambda *a, **k: {"id_historia": 1})

    def _revienta(anio, mes, usuario="auto"):
        raise RuntimeError("el navegador se colgó")
    monkeypatch.setattr(cierres_paquete, "generar_y_guardar", _revienta)

    # No debe levantar -- el best-effort atrapa la excepción adentro.
    r = iq.crear_snapshot_historia(2026, 8, usuario="test")
    assert r["aplicado"] is True
