"""`/admin/regenerar-snapshot` — botón "APLICAR DETALLE REAL" (Tamara
2026-09-01: "no de julio. del cierre. busca y ponelo bien"). Tiene que
pisar SOLO las 10 columnas de detalle (kcom/ucom/ktej/utej/ktin/utin/
gasto/gstotal/kvent/uvent) de la fila anclada del mes cerrado — nunca
patrimonio/cart/banco/stock/deuda/anticipos/dolar/maquinaria/realty/
usret/usuti/costo/ustock/uqui/retiro, que ya están anclados a los
últimos valores buenos conocidos (ver el botón ANCLAR).
"""
from __future__ import annotations

from unittest.mock import patch

import db
from modules.informes import queries as iq

CAMPOS = {
    "ucom": 1111.0, "kcom": 222.0,
    "utej": 3333.0, "ktej": 444.0,
    "utin": 5555.0, "ktin": 666.0,
    "gasto": 7777.0, "gstotal": 3333.0 + 5555.0 + 7777.0,
    "kvent": 888.0, "uvent": 9999.0,
}


def _login(app):
    @app.before_request
    def _l():
        from flask import g
        g.user = {"id_usuario": 0, "username": "test", "id_rol": 0,
                   "nombre_rol": "Accionista", "activo": True, "vend": None}
        g.permisos = {"*"}


def test_aplica_solo_las_10_columnas_de_detalle_con_update(app, monkeypatch):
    updates: list[tuple[str, dict]] = []

    def fake_execute_returning(sql, params=None, conn=None):
        s = " ".join(sql.split())
        updates.append((s, params))
        return {"id_historia": 517, **CAMPOS, "patrimonio": 21732772.07,
                "usuti": 651173.41}

    def fake_fetch_all(sql, params=None, conn=None):
        return []

    def fake_fetch_one(sql, params=None, conn=None):
        return None

    monkeypatch.setattr(db, "execute_returning", fake_execute_returning)
    monkeypatch.setattr(db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        iq, "historia_detalle_mes_cerrado",
        lambda anio, mes: {"ok": True, "anio": anio, "mes": mes,
                            "campos": CAMPOS, "fuente": {}},
    )
    _login(app)
    client = app.test_client()
    # Mes SIN override de _MATERIA_PRIMA_VERIFICADA (ese caso tiene su
    # propio test dedicado abajo) -- éste prueba el mecanismo genérico:
    # los 10 campos reconstruidos pasan tal cual, nada de balance se toca.
    resp = client.post(
        "/admin/regenerar-snapshot/",
        data={"anio": "2026", "mes": "6",
              "anio_detalle": "2026", "mes_detalle": "6",
              "aplicar_detalle_real": "1"},
    )
    assert resp.status_code == 200

    assert updates, "el POST tendría que haber corrido el UPDATE"
    sql, params = updates[0]
    assert sql.upper().startswith("UPDATE SCINTELA.HISTORIA")
    assert "RETURNING" in sql.upper()

    # Sólo las 10 columnas de detalle en el SET -- nada de balance.
    campos_prohibidos = ("patrimonio", "cart", "banco", "stock", "deuda",
                          "anticipos", "dolar", "maquinaria", "realty",
                          "usret", "usuti", "costo", "ustock", "uqui",
                          "retiro")
    set_clause = sql.upper().split("WHERE")[0]
    for campo in campos_prohibidos:
        assert campo.upper() + " =" not in set_clause, (
            f"el UPDATE de detalle NO debería tocar '{campo}'"
        )
    for campo in ("kcom", "ucom", "ktej", "utej", "ktin", "utin",
                  "gasto", "gstotal", "kvent", "uvent"):
        assert campo.upper() + " =" in set_clause

    assert "fecha = %(fecha)s" in sql
    assert params["fecha"].isoformat() == "2026-06-30"
    for k, v in CAMPOS.items():
        assert params[k] == v


def test_agosto_2026_pisa_kcom_ucom_con_el_valor_verificado_del_pdf(app, monkeypatch):
    """Tamara 2026-09-02, 'no es algo menor': ninguna reconstrucción
    automática de Materia Prima cuadra contra un mes cerrado (probado en
    vivo: SUM crudo da 523.712 kg, kg_hilado_mes da 597.023 -- las dos
    lejos de los 408.972 reales). Para agosto 2026 puntual, el UPDATE
    tiene que aplicar el valor verificado contra 'Vista previa cierre
    31-08-2026.pdf' (kcom=408972.0, ucom=1289746.0), pisando lo que haya
    devuelto `historia_detalle_mes_cerrado` para esos dos campos -- el
    resto (ktej/utej/ktin/utin/gasto/gstotal/kvent/uvent) se aplica tal
    cual lo reconstruido."""
    updates: list[tuple[str, dict]] = []

    def fake_execute_returning(sql, params=None, conn=None):
        s = " ".join(sql.split())
        updates.append((s, params))
        return {"id_historia": 517, **params, "patrimonio": 21732772.07,
                "usuti": 651173.41}

    monkeypatch.setattr(db, "execute_returning", fake_execute_returning)
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: [])
    monkeypatch.setattr(db, "fetch_one", lambda *a, **kw: None)
    monkeypatch.setattr(
        iq, "historia_detalle_mes_cerrado",
        lambda anio, mes: {
            "ok": True, "anio": anio, "mes": mes,
            "campos": {**CAMPOS, "kcom": 523712.46, "ucom": 1319702.88},
            "fuente": {},
        },
    )
    _login(app)
    client = app.test_client()
    resp = client.post(
        "/admin/regenerar-snapshot/",
        data={"anio": "2026", "mes": "8",
              "anio_detalle": "2026", "mes_detalle": "8",
              "aplicar_detalle_real": "1"},
    )
    assert resp.status_code == 200
    assert updates, "el POST tendría que haber corrido el UPDATE"
    _, params = updates[0]
    assert params["kcom"] == 408972.0
    assert params["ucom"] == 1289746.0
    # el resto de los campos reconstruidos NO se pisan
    assert params["ktej"] == CAMPOS["ktej"]
    assert params["kvent"] == CAMPOS["kvent"]


def test_no_reconstruido_no_hace_ningun_update(app, monkeypatch):
    """Si el mes no está cerrado (ok=False), no se toca la base."""
    updates: list = []

    def fake_execute_returning(sql, params=None, conn=None):
        updates.append((sql, params))
        return None

    monkeypatch.setattr(db, "execute_returning", fake_execute_returning)
    monkeypatch.setattr(db, "fetch_all", lambda *a, **kw: [])
    monkeypatch.setattr(db, "fetch_one", lambda *a, **kw: None)
    monkeypatch.setattr(
        iq, "historia_detalle_mes_cerrado",
        lambda anio, mes: {"ok": False, "razon": "el mes tiene que ser un mes ya cerrado"},
    )
    _login(app)
    client = app.test_client()
    resp = client.post(
        "/admin/regenerar-snapshot/",
        data={"anio": "2026", "mes": "9",
              "anio_detalle": "2026", "mes_detalle": "9",
              "aplicar_detalle_real": "1"},
    )
    assert resp.status_code == 200
    assert not updates, "no debería haber corrido UPDATE si ok=False"
