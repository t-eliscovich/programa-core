"""La pantalla «Emparejar con Asinfo» y el modo «solo con saldo».

TMT 2026-08-30 (dueña): *"quiero resolver solo las 489"* — las facturas sin
número del SRI que además tienen saldo pendiente. Y TMT 2026-08-26, sobre la
URL de JSON que había antes: *"no puedo hacer nada en esa página"* — por eso
la pantalla con botón.

Sin Postgres ni Asinfo: se stubbean `db` y `audit_asinfo`.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from modules.facturas import audit_asinfo

# --- el filtro "solo con saldo" ---------------------------------------------

def _sql_de_huerfanas(monkeypatch, **kwargs) -> str:
    capturado = {}

    def _fetch_all(sql, params=None, conn=None):
        capturado["sql"] = sql
        return []

    import db
    monkeypatch.setattr(db, "fetch_all", _fetch_all)
    audit_asinfo._huerfanas_pc(**kwargs)
    return " ".join(capturado["sql"].split())


def test_solo_con_saldo_filtra_por_saldo_calculado(monkeypatch):
    """El saldo va CALCULADO (importe − abono − retención, mig 0179)."""
    sql = _sql_de_huerfanas(monkeypatch, solo_con_saldo=True)
    assert "f.importe - COALESCE(f.abono, 0) - COALESCE(f.retencion, 0)) > 0.01" in sql


def test_sin_la_bandera_no_filtra(monkeypatch):
    sql = _sql_de_huerfanas(monkeypatch)
    assert "retencion" not in sql.lower()


def test_auditar_pasa_la_bandera(monkeypatch):
    capturado = {}

    def _huerfanas(limite=500, solo_con_saldo=False):
        capturado["solo_con_saldo"] = solo_con_saldo
        return []

    monkeypatch.setattr(audit_asinfo, "_huerfanas_pc", _huerfanas)
    audit_asinfo.auditar_huerfanas(solo_con_saldo=True)
    assert capturado["solo_con_saldo"] is True


# --- la pantalla -------------------------------------------------------------

_HUERFANA = {
    "pc_factura": {
        "id_factura": 555, "numf": 172013, "fecha": date(2026, 3, 31),
        "codigo_cli": "NGU", "cliente": "GUSQUI MACAS NELLY PATRICIA",
        "kg": 428.85, "importe": 3327.07, "abono": 0, "saldo": 3327.07,
        "stat": "Z",
    },
    "candidatos": [{
        "ai_numero": "001-099-000172013", "ai_tipo": "FACTURA",
        "ai_cliente_codigo": "NGU", "ai_kg": 428.85, "ai_usd": 3327.07,
        "score": 0.01,
    }],
    "mejor_score": 0.01,
}


def _login(app):
    @app.before_request
    def _acc():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {"id_usuario": 1, "username": "tamara", "id_rol": 1,
                  "nombre_rol": "Accionista", "activo": True}
        g.permisos = {"*"}


def test_la_pantalla_muestra_el_numero_que_le_pone(app, monkeypatch):
    _login(app)
    import db
    monkeypatch.setattr(db, "fetch_one",
                        lambda *a, **k: {"viejas": 16, "con_saldo": 489})
    with patch.object(audit_asinfo, "auditar_huerfanas",
                      return_value=[_HUERFANA]) as m:
        r = app.test_client().get("/facturas/emparejar-asinfo")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "001-099-000172013" in html          # el número que le pondría
    assert "NGU" in html
    assert "Emparejar las 1 facturas" in html   # el botón dice cuántas
    assert m.call_args.kwargs["solo_con_saldo"] is True
    assert "anteriores a 2025" in html          # las 16 que quedan afuera


def test_la_vista_previa_no_escribe(app, monkeypatch):
    _login(app)
    import db
    monkeypatch.setattr(db, "fetch_one",
                        lambda *a, **k: {"viejas": 0, "con_saldo": 1})
    with patch.object(audit_asinfo, "auditar_huerfanas",
                      return_value=[_HUERFANA]), \
         patch.object(audit_asinfo, "asociar") as asoc:
        app.test_client().get("/facturas/emparejar-asinfo")
    asoc.assert_not_called()


def test_el_boton_escribe(app, monkeypatch):
    _login(app)
    import db
    monkeypatch.setattr(db, "fetch_one",
                        lambda *a, **k: {"viejas": 0, "con_saldo": 1})
    with patch.object(audit_asinfo, "auditar_huerfanas",
                      return_value=[_HUERFANA]), \
         patch.object(audit_asinfo, "asociar") as asoc:
        r = app.test_client().post("/facturas/emparejar-asinfo")
    assert r.status_code == 200
    asoc.assert_called_once_with(555, "001-099-000172013", usuario="web")
    assert "se emparejaron" in r.get_data(as_text=True).lower()


def test_sin_permiso_no_hay_pantalla(app):
    @app.before_request
    def _sin_permiso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 7
        g.user = {"id_usuario": 7, "username": "vendedor", "id_rol": 9,
                  "nombre_rol": "Vendedor", "activo": True}
        g.permisos = {"mi_cartera.ver"}

    assert app.test_client().get("/facturas/emparejar-asinfo").status_code == 404


def test_el_json_viejo_acepta_con_saldo(app, monkeypatch):
    """La URL de siempre también sabe acotarse: ?con_saldo=1."""
    _login(app)
    with patch.object(audit_asinfo, "auditar_huerfanas",
                      return_value=[]) as m:
        r = app.test_client().get("/facturas/backfill-asinfo?con_saldo=1")
    assert r.status_code == 200
    assert m.call_args.kwargs["solo_con_saldo"] is True
