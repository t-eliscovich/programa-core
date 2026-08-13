"""Bug reportado por Alex Velastegui vía Instagram, 13/08/2026 18:13 EC.

Contexto: Alex re-subió `mov-12-08-202634XXXX6004.xlsx` (28 filas ya
cargadas la sesión anterior #63, que se había cerrado). El toast decía
"Sesión #64: las 28 filas del archivo ya estaban cargadas — nada nuevo para
agregar", y a continuación la pantalla /conciliacion/banco-v2 tiraba 500
con error id `c1379abc`.

Causa raíz: `crear_sesion(sesion.py)` dedupeaba las 28 filas contra los
historicos y matches, quedando `nuevos == []`. Como no había sesión abierta
(la #63 estaba cerrada) el bloque "No hay sesión abierta → crear una"
insertaba una sesión NUEVA con `extracto_payload = []`. El GET siguiente a
`/conciliacion/banco-v2` levantaba esa sesión huérfana y `banco_post_procesar`
reventaba al no encontrar movs.

Fix:
  1. `crear_sesion`: si no hay sesión abierta Y `nuevos == []`, devolver
     `(0, 0, skipped)` sin INSERT.
  2. `banco_crear_sesion` (view): si sid==0, flash + redirect al hub.
  3. `banco_post_procesar` (defensa profunda): si al abrir sesión encuentra
     una con payload vacío y sin matches, cerrarla auto y redirect al hub.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest


@pytest.fixture
def app_logueada():
    from tests.test_routes_smoke import build_app
    app, deshacer = build_app()

    @app.before_request
    def _login_falso():  # pragma: no cover
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "alex", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}

    try:
        yield app
    finally:
        deshacer()


# ── (1) modelo: `crear_sesion` no crea sesión huérfana ────────────────

def test_crear_sesion_no_crea_orfana_cuando_todo_se_dedupea(monkeypatch):
    from datetime import date as _date
    from modules.conciliacion import sesion as _sesion

    monkeypatch.setattr(_sesion, "sesion_abierta", lambda no_banco: None)
    sig_a = _sesion._firma_mov("A1", "", "C", "500", _date(2026, 5, 28))
    sig_b = _sesion._firma_mov("A2", "", "C", "500", _date(2026, 5, 28))
    monkeypatch.setattr(_sesion, "_firmas_ya_conocidas",
                        lambda no_banco: {sig_a, sig_b})

    calls = {"ins": 0}
    def fake_execute_returning(sql, params=None, conn=None):
        calls["ins"] += 1
        return {"id": 999}
    monkeypatch.setattr(_sesion.db, "execute_returning", fake_execute_returning)

    class _M:
        def __init__(self, doc):
            self.documento = doc
            self.codigo = ""
            self.tipo = "C"
            self.monto = "500"
            self.fecha = _date(2026, 5, 28)
            self.saldo = None
            self.concepto = ""
            self.numreferencia = ""

    sid, n_added, n_skipped = _sesion.crear_sesion(
        no_banco=10, usuario="alex",
        movs=[_M("A1"), _M("A2")],
        extracto_nombre="mov-12-08-202634XXXX6004.xlsx",
    )
    assert sid == 0, "no debe crear sesión huérfana"
    assert n_added == 0
    assert n_skipped == 2
    assert calls["ins"] == 0, "no debe correr INSERT"


# ── (2) view: banco_crear_sesion traduce sid=0 en flash + redirect ────

def test_banco_crear_sesion_view_redirect_al_hub_si_todo_dedup(app_logueada, monkeypatch):
    from modules.conciliacion import banco_v2_view as v

    # Payload xlsx que el parser va a devolver.
    monkeypatch.setattr(v, "parse_banco_xlsx", lambda raw: [
        type("MB", (), {"documento": "A1", "codigo": "", "tipo": "C",
                        "monto": 500.0, "fecha": date(2026, 8, 12),
                        "saldo": None, "concepto": "", "numreferencia": ""})(),
    ])
    # Simular que `_sesion.crear_sesion` devuelve el resultado del fix.
    monkeypatch.setattr(v._sesion, "crear_sesion",
                        lambda **kw: (0, 0, 28))

    # Ninguna migración pendiente.
    monkeypatch.setattr(v, "_migracion_lista_o_redirect", lambda: None)

    import io
    data = {"archivo": (io.BytesIO(b"xlsx-bytes"), "mov.xlsx")}
    rv = app_logueada.test_client().post(
        "/conciliacion/banco-v2/crear-sesion",
        data=data, content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert rv.status_code == 302, "debería redirect, no 500"
    # Redirect al hub, NO al post-procesar (que revienta con la sesión rota).
    assert rv.headers["Location"].rstrip("/").endswith("/conciliacion") \
        or "hub" in rv.headers["Location"] \
        or "/conciliacion" in rv.headers["Location"] \
           and "banco-v2" not in rv.headers["Location"]


# ── (3) view: banco_post_procesar tolera sesión abierta rota (defensa)

def test_banco_post_procesar_cierra_sesion_orfana(app_logueada, monkeypatch):
    from modules.conciliacion import banco_v2_view as v

    # Simular la sesión #64 rota en producción: payload vacío, 0 matches.
    fake_ses = {
        "id": 64, "no_banco": 10,
        "abierta_en": datetime(2026, 8, 13, 22, 11, 19),
        "cerrada_en": None,
        "matches_hechos": 0,
        "saldo_banco_objetivo": None,
        "saldo_banco_detectado": 1875978.8,
        "extracto_nombre": "mov-12-08-202634XXXX6004.xlsx",
    }
    monkeypatch.setattr(v._sesion, "sesion_abierta",
                        lambda nb, usuario=None: fake_ses)
    monkeypatch.setattr(v._sesion, "cargar_movs", lambda s: [])
    monkeypatch.setattr(v, "_migracion_lista_o_redirect", lambda: None)

    executed = {"sqls": []}
    def fake_execute(sql, params=None, conn=None):
        executed["sqls"].append((sql, params))
        return 1
    monkeypatch.setattr(v._db, "execute", fake_execute)

    rv = app_logueada.test_client().get("/conciliacion/banco-v2",
                                        follow_redirects=False)
    assert rv.status_code == 302, "no debe reventar 500"
    # Cerró la sesión huérfana con UPDATE.
    joined = "\n".join(sql for sql, _p in executed["sqls"])
    assert "UPDATE scintela.banco_conciliacion_sesion" in joined
    assert "cerrada_en = NOW()" in joined
    assert any(64 in (p or ()) for _s, p in executed["sqls"])
