"""`/admin/deudas-borradas` y el health `deudas_desaparecidas` — las deudas
de proveedores que la procedure vieja `procesa_provisiones` borró de posdat
el 01/09/2026 sin que estuvieran pagadas (ver el docstring del módulo).

Lo que se prueba: la detección mira la compra a crédito cuyo posdat ya no
existe y a la que nadie le registró pago/anulación/restauración; la
restauración crea la posdat con los datos de la compra y deja su mov_doble,
todo o nada; la pantalla no toca nada si no hay qué restaurar; el health se
pone rojo si hay una sola.
"""
from __future__ import annotations

import json

import db
import mov_doble as _md
from modules.admin_dbase import deudas_borradas_view as v
from modules.admin_dbase import health_audit_view as h


def _fila(id_compra=629, prov="RY", numero=10277, importe=2167.26, borrado=1044):
    return {
        "id_mov_doble": 30061, "id_posdat_borrado": borrado, "importe_md": importe,
        "fecha_operacion": "2026-08-29", "id_compra": id_compra, "fecha": "2026-08-29",
        "fechad": "2026-08-30", "codigo_prov": prov, "numero": numero,
        "importe": importe, "concepto": "OFT-000040412 M REYES KW22 C-T40",
        "clave": None, "kg": 1076.9,
    }


def _login(app):
    @app.before_request
    def _l():
        from flask import g
        g.user = {"id_usuario": 0, "username": "tamara", "id_rol": 0,
                  "nombre_rol": "Accionista", "activo": True, "vend": None}
        g.permisos = {"*"}


class _Tx:
    def __enter__(self):
        return object()

    def __exit__(self, *a):
        return False


# ── la consulta ──────────────────────────────────────────────────────────

def test_detectar_parte_de_la_traza_y_exige_que_nada_explique_la_baja(monkeypatch):
    """La fuente es `dia_movimiento` (lo que ESTABA vivo y dejó de estarlo), no
    "compras sin posdat": el primer intento con eso listó $160K de deudas que
    sí estaban pagadas (cheque a varios proveedores, sync del dBase)."""
    visto = {}

    def fake_fetch_all(sql, params=None, conn=None):
        visto["sql"] = " ".join(sql.split())
        visto["params"] = params
        return [_fila()]

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    filas = v.detectar()
    assert len(filas) == 1
    assert visto["params"] == {"tipo_rest": "posdat_restaurada", "desde": "2026-08-25"}
    sql = visto["sql"]
    # Parte de la traza: sólo posdat que estaban VIVAS y dejaron de estarlo…
    assert "FROM scintela.dia_movimiento m" in sql
    assert "m.regla = 'Deuda pagada o dada de baja'" in sql
    assert "t.creado_en >= %(desde)s::date" in sql
    # …que siguen sin existir…
    assert "NOT EXISTS (SELECT 1 FROM scintela.posdat p WHERE p.id_posdat = substring(m.doc_id from 2)::int)" in sql
    # …y que ningún movimiento explica: pago directo, pago EN LOTE (metadata), anulación.
    assert "o.destino_table = 'posdat' AND o.destino_id = substring(m.doc_id from 2)::int" in sql
    assert "o.metadata->'id_posdats' @> to_jsonb(substring(m.doc_id from 2)::int)" in sql
    assert "o.tipo NOT IN ('compra_a_posdat', 'compra_saldo_a_posdat')" in sql
    # La compra está viva y no se pagó al contado, y no la restauramos ya.
    assert "COALESCE(stat, '') <> 'Y'" in sql
    assert "COALESCE(no_banco, 0) = 0" in sql
    assert "id_transaccion IS NULL" in sql
    assert "cuenta_pagada" in sql
    assert "r.tipo = %(tipo_rest)s" in sql


def test_desde_es_la_fecha_en_que_todo_pago_deja_rastro():
    """Antes del 25/08 los cheques a varios proveedores no anotaban qué posdat
    cerraban (07/08, 17/08): una baja sin rastro de entonces NO es una deuda
    borrada. Si alguien corre la fecha hacia atrás, va a listar pagadas."""
    assert v.DESDE == "2026-08-25"


def test_resumen_cuenta_y_suma(monkeypatch):
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: [
        _fila(), _fila(id_compra=457, prov="AQ", numero=10106, importe=8652.6, borrado=914)])
    r = v.resumen()
    assert r == {"n": 2, "total": 10819.86, "proveedores": ["AQ", "RY"]}


# ── la restauración ──────────────────────────────────────────────────────

def test_restaurar_crea_la_posdat_con_los_datos_de_la_compra_y_deja_mov_doble(monkeypatch):
    inserts, movs = [], []

    def fake_execute_returning(sql, params=None, conn=None):
        assert "INSERT INTO scintela.posdat" in sql
        assert "banc, clave" in sql and ", 0, %(clave)s" in sql, "la deuda vuelve VIVA (banc=0)"
        inserts.append(params)
        return {"id_posdat": 2001, **{k: params[k] for k in ("fecha", "fechad", "prov", "num", "importe", "concepto")}}

    monkeypatch.setattr(db, "execute_returning", fake_execute_returning)
    monkeypatch.setattr(db, "tx", lambda: _Tx())
    monkeypatch.setattr(_md, "registrar", lambda **kw: movs.append(kw) or 1)

    creadas = v.restaurar([_fila()], "tamara")

    assert len(creadas) == 1 and creadas[0]["id_posdat"] == 2001
    p = inserts[0]
    assert (p["fecha"], p["fechad"], p["prov"], p["num"], p["importe"]) == (
        "2026-08-29", "2026-08-30", "RY", 10277, 2167.26)
    assert p["concepto"] == "OFT-000040412 M REYES KW22 C-T40"
    assert p["usuario"] == "restaurar-deudas:tamara"
    m = movs[0]
    assert m["tipo"] == "posdat_restaurada"
    assert (m["origen_table"], m["origen_id"], m["destino_table"], m["destino_id"]) == (
        "compra", 629, "posdat", 2001)
    assert m["importe"] == 2167.26
    assert m["metadata"]["id_posdat_borrado"] == 1044


def test_restaurar_corta_si_un_insert_no_devuelve_id(monkeypatch):
    """Todo o nada: la excepción sale de adentro de la tx (que la deshace)."""
    monkeypatch.setattr(db, "execute_returning", lambda *a, **kw: None)
    monkeypatch.setattr(db, "tx", lambda: _Tx())
    monkeypatch.setattr(_md, "registrar", lambda **kw: 1)
    try:
        v.restaurar([_fila()], "tamara")
    except RuntimeError as e:
        assert "compra #629" in str(e)
    else:
        raise AssertionError("tenía que cortar")


def test_restaurar_sin_fechad_usa_la_fecha(monkeypatch):
    inserts = []
    monkeypatch.setattr(db, "execute_returning",
                        lambda sql, params=None, conn=None: inserts.append(params) or {"id_posdat": 1})
    monkeypatch.setattr(db, "tx", lambda: _Tx())
    monkeypatch.setattr(_md, "registrar", lambda **kw: 1)
    f = _fila()
    f["fechad"] = None
    v.restaurar([f], "x")
    assert inserts[0]["fechad"] == "2026-08-29"


# ── la pantalla ──────────────────────────────────────────────────────────

def test_get_lista_las_deudas_y_el_total(app, monkeypatch):
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: [
        _fila(), _fila(id_compra=457, prov="AQ", numero=10106, importe=8652.6, borrado=914)])
    _login(app)
    html = app.test_client().get("/admin/deudas-borradas/").get_data(as_text=True)
    assert "10,819.86" in html
    assert "2 deudas" in html
    assert "VOLVER A PONER LAS DEUDAS" in html
    assert "/compras/629" in html and "/compras/457" in html


def test_get_sin_deudas_no_muestra_boton(app, monkeypatch):
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: [])
    _login(app)
    html = app.test_client().get("/admin/deudas-borradas/").get_data(as_text=True)
    assert "No hay deudas borradas" in html
    assert "VOLVER A PONER" not in html


def test_post_restaura_lo_detectado_y_muestra_lo_guardado(app, monkeypatch):
    llamadas = {"n": 0}
    pendientes = [_fila()]

    def fake_fetch_all(*a, **kw):
        llamadas["n"] += 1
        # 1ª llamada: lo que hay que restaurar. 2ª (después del POST): ya nada.
        return pendientes if llamadas["n"] == 1 else []

    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(db, "execute_returning", lambda sql, params=None, conn=None: {
        "id_posdat": 2001, "fecha": params["fecha"], "fechad": params["fechad"],
        "prov": params["prov"], "num": params["num"], "importe": params["importe"],
        "concepto": params["concepto"]})
    monkeypatch.setattr(db, "tx", lambda: _Tx())
    monkeypatch.setattr(_md, "registrar", lambda **kw: 1)
    _login(app)
    resp = app.test_client().post("/admin/deudas-borradas/", data={"restaurar": "1"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Se volvieron a poner 1 deudas" in html
    assert "2,167.26" in html and "2001" in html
    assert "No hay deudas borradas" not in html


def test_post_sin_nada_que_restaurar_no_escribe(app, monkeypatch):
    escrituras = []
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: [])
    monkeypatch.setattr(db, "execute_returning", lambda *a, **kw: escrituras.append(a) or {"id_posdat": 1})
    monkeypatch.setattr(db, "tx", lambda: _Tx())
    _login(app)
    html = app.test_client().post("/admin/deudas-borradas/", data={"restaurar": "1"}).get_data(as_text=True)
    assert "No quedó nada por restaurar" in html
    assert not escrituras


def test_post_con_error_lo_muestra_y_no_deja_creadas(app, monkeypatch):
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: [_fila()])
    monkeypatch.setattr(db, "execute_returning", lambda *a, **kw: None)
    monkeypatch.setattr(db, "tx", lambda: _Tx())
    monkeypatch.setattr(_md, "registrar", lambda **kw: 1)
    _login(app)
    html = app.test_client().post("/admin/deudas-borradas/", data={"restaurar": "1"}).get_data(as_text=True)
    assert "no se cambió nada" in html
    assert "Se volvieron a poner" not in html


def test_sin_permiso_no_entra(app, monkeypatch):
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: [_fila()])

    @app.before_request
    def _l():
        from flask import g
        g.user = {"id_usuario": 9, "username": "vendedor", "id_rol": 5,
                  "nombre_rol": "Vendedor", "activo": True, "vend": "PPR"}
        g.permisos = {"compras.ver"}

    resp = app.test_client().get("/admin/deudas-borradas/")
    # Sin permiso la app muestra "no existe" (404) a propósito, no 403.
    assert resp.status_code in (302, 403, 404)
    assert "VOLVER A PONER" not in resp.get_data(as_text=True)


# ── el health ────────────────────────────────────────────────────────────

def test_health_rojo_con_una_sola_deuda_borrada(app, monkeypatch):
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: [_fila()])
    _login(app)
    data = json.loads(app.test_client().get("/admin/health/deudas-desaparecidas").get_data(as_text=True))
    assert data["ok"] is False
    assert data["alerts"][0]["category"] == "deudas_desaparecidas"
    assert "2,167.26" in data["alerts"][0]["msg"] and "RY" in data["alerts"][0]["msg"]
    assert "/admin/deudas-borradas/" in data["alerts"][0]["msg"]
    assert data["stats"]["n"] == 1


def test_health_verde_sin_deudas_borradas(app, monkeypatch):
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: [])
    _login(app)
    data = json.loads(app.test_client().get("/admin/health/deudas-desaparecidas").get_data(as_text=True))
    assert data == {"ok": True, "alerts": [], "stats": {"n": 0, "total": 0.0, "proveedores": []}}


def test_health_si_la_consulta_falla_no_queda_verde(app, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("sin base")
    monkeypatch.setattr(db, "fetch_all", boom)
    _login(app)
    data = json.loads(app.test_client().get("/admin/health/deudas-desaparecidas").get_data(as_text=True))
    assert data["ok"] is False
    assert data["alerts"][0]["category"] == "deudas_desaparecidas_error"


def test_health_all_incluye_deudas_desaparecidas_en_el_ok():
    import inspect
    src = inspect.getsource(h.health_all)
    assert "deudas_desaparecidas()" in src
    assert '"deudas_desaparecidas": data25' in src
    assert 'data25["ok"]' in src
