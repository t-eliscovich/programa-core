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

import pytest

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


def _sin_duenos(monkeypatch):
    """Ningún número candidato está tomado por otra fila."""
    import modules.facturas.views as fviews
    monkeypatch.setattr(fviews.db, "fetch_all", lambda *a, **k: [])


def test_la_pantalla_muestra_el_numero_que_le_pone(app, monkeypatch):
    _login(app)
    _sin_duenos(monkeypatch)
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
    _sin_duenos(monkeypatch)
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
    _sin_duenos(monkeypatch)
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


# --- el número ya lo tiene otra fila (TMT 2026-08-30) ------------------------
#
# "se emparejaron 3, 24 dieron error": el número candidato ya estaba tomado
# por una COPIA sin plata del backfill viejo (asinfo-backfill, stat T). El
# emparejador ahora la absorbe: elimina la copia (con los frenos de
# borrar_carga_erronea) y le pasa el número a la factura de verdad.

def _con_dueno(monkeypatch, dueno: dict):
    import modules.facturas.views as fviews
    monkeypatch.setattr(fviews.db, "fetch_all", lambda *a, **k: [dueno])


_COPIA = {"id_factura": 31260, "numf_completo": "001-099-000172013",
          "stat": "T", "usuario_crea": "asinfo-backfill", "abono": 0}


def test_la_copia_fantasma_se_absorbe(app, monkeypatch):
    _login(app)
    _con_dueno(monkeypatch, dict(_COPIA))
    import db
    monkeypatch.setattr(db, "fetch_one",
                        lambda *a, **k: {"viejas": 0, "con_saldo": 1})
    with patch.object(audit_asinfo, "auditar_huerfanas",
                      return_value=[_HUERFANA]), \
         patch.object(audit_asinfo, "absorber_copia_backfill") as borrar, \
         patch.object(audit_asinfo, "asociar") as asoc:
        r = app.test_client().post("/facturas/emparejar-asinfo")
    assert r.status_code == 200
    borrar.assert_called_once_with(31260, usuario="web")
    asoc.assert_called_once_with(555, "001-099-000172013", usuario="web")
    assert "absorbiendo su copia" in r.get_data(as_text=True)


def test_la_copia_se_muestra_antes_de_tocarla(app, monkeypatch):
    """En la vista previa la copia se LISTA y no se borra nada."""
    _login(app)
    _con_dueno(monkeypatch, dict(_COPIA))
    import db
    monkeypatch.setattr(db, "fetch_one",
                        lambda *a, **k: {"viejas": 0, "con_saldo": 1})
    with patch.object(audit_asinfo, "auditar_huerfanas",
                      return_value=[_HUERFANA]), \
         patch.object(audit_asinfo, "absorber_copia_backfill") as borrar, \
         patch.object(audit_asinfo, "asociar") as asoc:
        r = app.test_client().get("/facturas/emparejar-asinfo")
    borrar.assert_not_called()
    asoc.assert_not_called()
    html = r.get_data(as_text=True)
    assert "una copia sin plata (1)" in html
    assert "elimina la copia" in html


def test_el_dueno_de_verdad_no_se_pisa(app, monkeypatch):
    """Si el número lo tiene una factura real (no la copia del backfill),
    el botón NO la toca: queda listada para que la mire una persona."""
    _login(app)
    _con_dueno(monkeypatch, {"id_factura": 99, "numf_completo": "001-099-000172013",
                             "stat": "Z", "usuario_crea": "web", "abono": 0})
    import db
    monkeypatch.setattr(db, "fetch_one",
                        lambda *a, **k: {"viejas": 0, "con_saldo": 1})
    with patch.object(audit_asinfo, "auditar_huerfanas",
                      return_value=[_HUERFANA]), \
         patch.object(audit_asinfo, "absorber_copia_backfill") as borrar, \
         patch.object(audit_asinfo, "asociar") as asoc:
        r = app.test_client().post("/facturas/emparejar-asinfo")
    borrar.assert_not_called()
    asoc.assert_not_called()
    assert "el número ya lo tiene otra factura" in r.get_data(as_text=True)


def test_la_copia_con_plata_queda_en_errores(app, monkeypatch):
    """borrar_carga_erronea levanta (la copia tenía abonos): no se asocia
    y el caso queda contado como error, no borrado a medias."""
    _login(app)
    _con_dueno(monkeypatch, dict(_COPIA))
    import db
    monkeypatch.setattr(db, "fetch_one",
                        lambda *a, **k: {"viejas": 0, "con_saldo": 1})
    with patch.object(audit_asinfo, "auditar_huerfanas",
                      return_value=[_HUERFANA]), \
         patch.object(audit_asinfo, "absorber_copia_backfill",
                      side_effect=ValueError("La copia tiene cheques aplicados: no se toca.")), \
         patch.object(audit_asinfo, "asociar") as asoc:
        r = app.test_client().post("/facturas/emparejar-asinfo")
    asoc.assert_not_called()
    html = r.get_data(as_text=True)
    assert "dieron error" in html
    assert "no se toca" in html          # el detalle del error se VE


def test_las_limpias_se_aplican_aunque_las_copias_fallen(app, monkeypatch):
    """La vuelta del 31/08: '0 emparejadas, 24 errores' — el freno cortaba
    todo. Las que no dependen de nada van primero y quedan escritas."""
    _login(app)
    limpia = {
        "pc_factura": dict(_HUERFANA["pc_factura"], id_factura=777),
        "candidatos": [dict(_HUERFANA["candidatos"][0],
                            ai_numero="001-099-000900001")],
        "mejor_score": 0.01,
    }
    dueno = dict(_COPIA)
    import modules.facturas.views as fviews
    monkeypatch.setattr(fviews.db, "fetch_all", lambda *a, **k: [dueno])
    import db
    monkeypatch.setattr(db, "fetch_one",
                        lambda *a, **k: {"viejas": 0, "con_saldo": 2})
    with patch.object(audit_asinfo, "auditar_huerfanas",
                      return_value=[_HUERFANA, limpia]), \
         patch.object(audit_asinfo, "absorber_copia_backfill",
                      side_effect=ValueError("no se toca")), \
         patch.object(audit_asinfo, "asociar") as asoc:
        r = app.test_client().post("/facturas/emparejar-asinfo")
    # La limpia (777) se asoció aunque la copia falló.
    asoc.assert_called_once_with(777, "001-099-000900001", usuario="web")
    html = r.get_data(as_text=True)
    assert "se emparejaron" in html and "<strong>1</strong>" in html


# --- los frenos del absorbedor, de cerca ------------------------------------

class _FakeDBCopia:
    """La copia y sus alrededores, configurable por test."""

    def __init__(self, fila, cheques=0, retenciones=0, movs=0):
        self.fila, self.cheques = fila, cheques
        self.retenciones, self.movs = retenciones, movs
        self.borradas = []

    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.factura where id_factura" in s:
            return dict(self.fila) if self.fila else None
        if "chequesxfact" in s:
            return {"n": self.cheques}
        if "retencion" in s:
            return {"n": self.retenciones}
        if "mov_doble" in s:
            return {"n": self.movs}
        return None

    def execute(self, sql, params=None, conn=None):
        assert "delete from scintela.factura" in " ".join(sql.split()).lower()
        self.borradas.append(params[0])
        return 1

    def apply_to(self, monkeypatch):
        import db
        monkeypatch.setattr(db, "fetch_one", self.fetch_one)
        monkeypatch.setattr(db, "execute", self.execute)


_FILA_COPIA = {"id_factura": 31260, "numf": 172013,
               "numf_completo": "001-099-000172013", "codigo_cli": "NGU",
               "stat": "T", "usuario_crea": "asinfo-backfill"}


def test_el_absorbedor_borra_la_copia_limpia(monkeypatch):
    fake = _FakeDBCopia(_FILA_COPIA)
    fake.apply_to(monkeypatch)
    res = audit_asinfo.absorber_copia_backfill(31260)
    assert fake.borradas == [31260]
    assert res["numf_completo"] == "001-099-000172013"


@pytest.mark.parametrize("cambio, texto", [
    ({"stat": "Z"}, "No es una copia del backfill"),
    ({"usuario_crea": "dbf-import"}, "No es una copia del backfill"),
])
def test_el_absorbedor_no_toca_lo_que_no_es_copia(monkeypatch, cambio, texto):
    fake = _FakeDBCopia(dict(_FILA_COPIA, **cambio))
    fake.apply_to(monkeypatch)
    with pytest.raises(ValueError, match=texto):
        audit_asinfo.absorber_copia_backfill(31260)
    assert fake.borradas == []


@pytest.mark.parametrize("kwargs, texto", [
    ({"cheques": 1}, "cheques aplicados"),
    ({"retenciones": 1}, "retenciones"),
    ({"movs": 2}, "movimientos"),
])
def test_el_absorbedor_frena_si_la_copia_tiene_algo(monkeypatch, kwargs, texto):
    fake = _FakeDBCopia(_FILA_COPIA, **kwargs)
    fake.apply_to(monkeypatch)
    with pytest.raises(ValueError, match=texto):
        audit_asinfo.absorber_copia_backfill(31260)
    assert fake.borradas == []
