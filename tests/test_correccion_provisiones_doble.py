"""`/admin/correccion-provisiones-doble` -- corrige el doble cobro de
provisiones YY/RT del 01/09/2026 (mes completo + dia, ver el docstring del
modulo). Resta EXACTAMENTE el monto conocido de cada una de las 12 filas,
nunca a ciegas: si una fila no matchea el importe esperado, o falta algo,
no se aplica nada. Idempotente via sistema_meta.
"""
from __future__ import annotations

from unittest.mock import patch

import db
from modules.admin_dbase import correccion_provisiones_doble_view as v


def _filas_correctas():
    """Las 12 filas YY/RT con el doble cobro tal como quedaron el 01/09/2026
    (importe actual = correcto + monto_de_mas para cada una)."""
    return [
        {"id_posdat": 1, "prov": "YY", "concepto": "A,E,C AG,EN,CMB", "importe": 258275.0},
        {"id_posdat": 2, "prov": "RT", "concepto": "RETENCIONES", "importe": 302790.0},
        {"id_posdat": 3, "prov": "YY", "concepto": "SUELDOS", "importe": 229850.0},
        {"id_posdat": 4, "prov": "YY", "concepto": "SRI PROVISION", "importe": 369367.5},
        {"id_posdat": 5, "prov": "YY", "concepto": "SS IESS", "importe": 73240.0},
        {"id_posdat": 6, "prov": "YY", "concepto": "AB PROVISION", "importe": 231817.5},
        {"id_posdat": 7, "prov": "YY", "concepto": "13 DEC.TERCERO", "importe": 135575.0},
        {"id_posdat": 8, "prov": "YY", "concepto": "ALQUILER", "importe": 27032.5},
        {"id_posdat": 9, "prov": "YY", "concepto": "PROV.INCOBRABLE", "importe": 181390.0},
        {"id_posdat": 10, "prov": "YY", "concepto": "14 DEC.CUAR+RES", "importe": 9842.5},
        {"id_posdat": 11, "prov": "YY", "concepto": "INTERESES", "importe": 26442.5},
        {"id_posdat": 12, "prov": "YY", "concepto": "JP JUB.PATRONAL", "importe": 46295.0},
    ]


def test_preview_matchea_las_12_filas_y_suma_724275(monkeypatch):
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: _filas_correctas())
    prev = v._preview()
    assert not prev["sin_match"]
    assert len(prev["filas"]) == 12
    assert prev["total_a_restar"] == 724275.00
    # Cada fila corregida = importe actual - monto conocido, todas matchean.
    for f in prev["filas"]:
        assert f["matchea"] is True
        assert f["importe_corregido"] == round(f["importe_actual"] - f["monto_a_restar"], 2)


def test_preview_marca_sin_match_si_falta_una_fila(monkeypatch):
    filas = _filas_correctas()[:-1]  # falta JP JUB.PATRONAL
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: filas)
    prev = v._preview()
    assert len(prev["sin_match"]) == 1
    assert prev["sin_match"][0]["clave"] == ("YY", "^JP")
    assert len(prev["filas"]) == 11


def test_preview_no_matchea_si_importe_ya_fue_tocado(monkeypatch):
    filas = _filas_correctas()
    filas[0]["importe"] = 100.0  # alguien ya corrigió esta a mano, quedó chica
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: filas)
    prev = v._preview()
    fila_aec = next(f for f in prev["filas"] if f["clave"] == ("YY", "^A,E,C"))
    assert fila_aec["matchea"] is False


def _login(app):
    @app.before_request
    def _l():
        from flask import g
        g.user = {"id_usuario": 0, "username": "test", "id_rol": 0,
                   "nombre_rol": "Accionista", "activo": True, "vend": None}
        g.permisos = {"*"}


def test_post_aplicar_resta_exactamente_lo_esperado(app, monkeypatch):
    updates: list[tuple[float, int]] = []

    def fake_execute_returning(sql, params=None, conn=None):
        s = " ".join(sql.split()).upper()
        if s.startswith("UPDATE SCINTELA.POSDAT"):
            updates.append((params["monto"], params["id"]))
            return {"id_posdat": params["id"], "concepto": "x",
                     "importe": 1.0}
        return None

    marca_insertada = []

    def fake_execute(sql, params=None, conn=None):
        if "sistema_meta" in sql.lower():
            marca_insertada.append(params)
        return 1

    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: _filas_correctas())
    monkeypatch.setattr(db, "fetch_one", lambda *a, **kw: None)  # marca no aplicada
    monkeypatch.setattr(db, "execute_returning", fake_execute_returning)
    monkeypatch.setattr(db, "execute", fake_execute)

    class _Tx:
        def __enter__(self):
            return object()

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(db, "tx", lambda: _Tx())

    _login(app)
    client = app.test_client()
    resp = client.post("/admin/correccion-provisiones-doble/", data={"aplicar": "1"})
    assert resp.status_code == 200

    assert len(updates) == 12
    assert round(sum(m for m, _ in updates), 2) == 724275.00
    assert marca_insertada, "no se grabo la marca de idempotencia"


def test_post_aplicar_no_hace_nada_si_ya_esta_marcada(app, monkeypatch):
    updates = []

    def fake_execute_returning(sql, params=None, conn=None):
        updates.append(params)
        return None

    monkeypatch.setattr(db, "fetch_one", lambda *a, **kw: {"valor": "2026-09-01T12:00:00"})
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: _filas_correctas())
    monkeypatch.setattr(db, "execute_returning", fake_execute_returning)
    monkeypatch.setattr(db, "execute", lambda *a, **kw: 1)

    _login(app)
    client = app.test_client()
    resp = client.post("/admin/correccion-provisiones-doble/", data={"aplicar": "1"})
    assert resp.status_code == 200
    assert not updates, "no deberia tocar nada si ya esta marcada la correccion"


def test_post_aplicar_no_resta_si_falta_una_fila(app, monkeypatch):
    updates = []

    def fake_execute_returning(sql, params=None, conn=None):
        updates.append(params)
        return None

    monkeypatch.setattr(db, "fetch_one", lambda *a, **kw: None)
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: _filas_correctas()[:-1])
    monkeypatch.setattr(db, "execute_returning", fake_execute_returning)
    monkeypatch.setattr(db, "execute", lambda *a, **kw: 1)

    _login(app)
    client = app.test_client()
    resp = client.post("/admin/correccion-provisiones-doble/", data={"aplicar": "1"})
    assert resp.status_code == 200
    assert not updates, "no deberia restar nada si falta una fila conocida"
