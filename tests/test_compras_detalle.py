"""Ficha de compra — /compras/<id>.

TMT 2026-08-03 (dueña): "cuando clické el link de compra 473 me dice 404 no
encontrado". El link del Historial apuntaba a /compras/473 y la ruta no
existía. Estos tests fijan el contrato de la ficha nueva:

1. GET /compras/<id_interno> → 200 y renderea el template.
2. GET /compras/<numero> (cuando el id no existe) → 200, cae a por_numero.
3. GET /compras/<inexistente> → 404 (no 500).
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tests.test_routes_smoke import build_app  # noqa: E402

COMPRA = {
    "id_compra": 473, "fecha": date(2026, 8, 3), "fechad": date(2026, 9, 2),
    "codigo_prov": "AI", "tipo": "H", "comprobante": "0001-15", "numero": 15,
    "kg": 1200, "importe": 15527.85, "concepto": "HILADO", "clave": "",
    "no_banco": 0, "stat": "", "observacion": "",
    "fecha_crea": date(2026, 8, 3), "usuario_crea": "andres",
    "proveedor": "AGRO INDUSTRIAL", "banco": "",
}


@pytest.fixture
def app_con_compra(monkeypatch):
    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False
    # `app.py` hace `from auth import load_logged_in_user` en el import, así
    # que pisar `auth.load_logged_in_user` sólo funciona si app.py todavía no
    # se importó (o sea: sólo si este archivo corre primero). En la suite
    # completa ya está importado. Por eso registramos NUESTRO before_request
    # después de create_app: corre siempre, sin depender del orden.
    @app.before_request
    def _login_falso():  # pragma: no cover - infra de test
        from flask import g, session
        session["usuario_id"] = 1
        g.user = {
            "id_usuario": 1, "username": "test",
            "id_rol": 1, "nombre_rol": "Dueño", "activo": True,
        }
        g.permisos = {"*"}

    try:
        yield app
    finally:
        deshacer()


def _patch_queries(monkeypatch, *, por_id=None, por_numero=None):
    from modules.compras import queries as q
    monkeypatch.setattr(q, "por_id", lambda i: por_id)
    monkeypatch.setattr(q, "por_numero", lambda n: por_numero)
    monkeypatch.setattr(q, "movimientos", lambda i, limite=100: [])


def test_detalle_por_id_interno_renderea(app_con_compra, monkeypatch):
    _patch_queries(monkeypatch, por_id=COMPRA)
    rv = app_con_compra.test_client().get("/compras/473")
    assert rv.status_code == 200
    body = rv.get_data(as_text=True)
    assert "Compra N° 15" in body
    assert "AGRO INDUSTRIAL" in body


def test_detalle_cae_a_numero_si_el_id_no_existe(app_con_compra, monkeypatch):
    _patch_queries(monkeypatch, por_id=None, por_numero=COMPRA)
    rv = app_con_compra.test_client().get("/compras/15")
    assert rv.status_code == 200
    assert "Compra N° 15" in rv.get_data(as_text=True)


def test_detalle_inexistente_da_404_no_500(app_con_compra, monkeypatch):
    _patch_queries(monkeypatch, por_id=None, por_numero=None)
    rv = app_con_compra.test_client().get("/compras/999999")
    assert rv.status_code == 404


def test_detalle_muestra_los_movimientos(app_con_compra, monkeypatch):
    from modules.compras import queries as q
    monkeypatch.setattr(q, "por_id", lambda i: COMPRA)
    monkeypatch.setattr(q, "movimientos", lambda i, limite=100: [{
        "id_mov_doble": 19860, "fecha_operacion": date(2026, 8, 3),
        "tipo": "anticipo_a_compra", "importe": 15527.85,
        "concepto": "AI 15 · 1 anticipo(s) → compra #473",
        "usuario": "andres", "estado": "activo",
        "origen_table": "dolares", "origen_id": 3209,
        "destino_table": "compra", "destino_id": 473,
    }])
    body = app_con_compra.test_client().get("/compras/473").get_data(as_text=True)
    assert "Anticipo A Compra" in body
    assert "15.527,85" in body


# ── Descontar anticipos (dueña 10/09/2026) ──────────────────────────────────

def _patch_anticipos(monkeypatch, *, vivos, descuentos=(), deuda=248137.86):
    from modules.compras import queries as q
    monkeypatch.setattr(q, "por_id", lambda i: {**COMPRA, "codigo_prov": "C2", "proveedor": "COLOURTEX"})
    monkeypatch.setattr(q, "movimientos", lambda i, limite=100: [])
    monkeypatch.setattr(q, "anticipos_vivos_del_proveedor", lambda p: list(vivos))
    monkeypatch.setattr(q, "descuentos_de_anticipos", lambda i: list(descuentos))
    monkeypatch.setattr(q, "deuda_abierta", lambda p, n: deuda)


VIVOS = [
    {"id_dolares": 2950, "fecha": date(2026, 9, 10), "importe": 31944.58, "concepto": "CAE"},
    {"id_dolares": 2903, "fecha": date(2026, 4, 21), "importe": 64607.0, "concepto": "INI"},
]


def test_detalle_muestra_los_anticipos_vivos_para_tildar(app_con_compra, monkeypatch):
    _patch_anticipos(monkeypatch, vivos=VIVOS)
    body = app_con_compra.test_client().get("/compras/473").get_data(as_text=True)
    main = body[body.index("<main"):]
    assert "Anticipos de C2" in main
    assert 'name="id_dolares" value="2950"' in main
    assert "31.944,58" in main and "CAE" in main
    assert "Descontar de la deuda" in main
    assert "/compras/473/descontar-anticipos" in main
    assert "248.137,86" in main  # la deuda abierta


def test_detalle_sin_anticipos_no_muestra_el_bloque(app_con_compra, monkeypatch):
    _patch_anticipos(monkeypatch, vivos=[])
    body = app_con_compra.test_client().get("/compras/473").get_data(as_text=True)
    assert "Anticipos de C2" not in body


def test_detalle_sin_deuda_abierta_no_deja_descontar(app_con_compra, monkeypatch):
    _patch_anticipos(monkeypatch, vivos=VIVOS, deuda=None)
    body = app_con_compra.test_client().get("/compras/473").get_data(as_text=True)
    assert "no tiene deuda abierta" in body
    assert "Descontar de la deuda" not in body


def test_detalle_muestra_lo_descontado_con_deshacer(app_con_compra, monkeypatch):
    _patch_anticipos(monkeypatch, vivos=[], deuda=150749.58, descuentos=[{
        "id_mov_doble": 777, "importe": 97388.28, "fecha": date(2026, 9, 10),
        "anticipos": [{"id_dolares": 2950, "importe": 31944.58, "concepto": "CAE"}],
    }])
    body = app_con_compra.test_client().get("/compras/473").get_data(as_text=True)
    assert "97.388,28" in body
    assert "/compras/473/deshacer-descuento/777" in body
    assert "Deshacer" in body


def test_post_descontar_llama_a_queries_y_vuelve_a_la_ficha(app_con_compra, monkeypatch):
    from modules.compras import queries as q
    llamadas = []

    def _desc(id_compra, ids, usuario="web"):
        llamadas.append((id_compra, list(ids), usuario))
        return {"numero": 15, "n": 2, "total": 96551.58, "deuda_despues": 151586.28}
    monkeypatch.setattr(q, "descontar_anticipos", _desc)
    rv = app_con_compra.test_client().post(
        "/compras/473/descontar-anticipos",
        data={"id_dolares": ["2950", "2903"]},
    )
    assert rv.status_code == 302 and rv.headers["Location"].endswith("/compras/473")
    assert llamadas == [(473, ["2950", "2903"], "test")]


def test_post_descontar_error_de_negocio_avisa_sin_500(app_con_compra, monkeypatch):
    from modules.compras import queries as q

    def _desc(*a, **k):
        raise ValueError("Los anticipos suman 1 y la deuda abierta es 0: destildá alguno.")
    monkeypatch.setattr(q, "descontar_anticipos", _desc)
    rv = app_con_compra.test_client().post("/compras/473/descontar-anticipos",
                                           data={"id_dolares": ["1"]}, follow_redirects=False)
    assert rv.status_code == 302


def test_post_deshacer_descuento(app_con_compra, monkeypatch):
    from modules.compras import queries as q
    monkeypatch.setattr(q, "deshacer_descuento_anticipos",
                        lambda idm, usuario="web": {"numero": 15, "n": 1, "total": 31944.58})
    rv = app_con_compra.test_client().post("/compras/473/deshacer-descuento/777")
    assert rv.status_code == 302 and rv.headers["Location"].endswith("/compras/473")
