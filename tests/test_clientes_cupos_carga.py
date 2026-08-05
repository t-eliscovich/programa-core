"""Carga masiva de cupos — /clientes/cupos-carga (TMT 2026-08-05).

La dueña completó el Excel "clientes-sin-cupo" (423 filas) con CUPO A CARGAR
y pidió subirlos todos, con la regla *"los vacíos son clientes sin crédito,
quedan en 0"*. Cargar 423 fichas a mano es inviable → pantalla de carga
masiva (operar-por-la-ui). Contrato:

  * El parser encuentra los encabezados aunque haya columnas decorativas,
    lee CÓDIGO + CUPO A CARGAR, y celda vacía = cupo 0.
  * La vista previa clasifica: cambia / ya estaba así / sin ficha — y NADA
    se escribe hasta el Confirmar.
  * El aplicar va por queries.editar() (misma función que la ficha) y sólo
    acepta el payload COD=ENTERO — cualquier otra cosa es 400.
  * Sin `cupos.editar` la pantalla es 404 (mig 0167: el cupo lo tocan sólo
    Andrés y accionistas).
"""
from __future__ import annotations

import io
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.clientes.cupos_xlsx import parse_cupos_xlsx  # noqa: E402


def _xlsx(filas: list[list], headers: list[str] | None = None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["listado decorativo"])  # fila basura arriba, como el export real
    ws.append(headers or ["CÓDIGO", "CLIENTE", "SALDO ACTUAL", "CUPO A CARGAR"])
    for f in filas:
        ws.append(f)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- parser ---

def test_parser_lee_codigo_y_cupo_y_vacio_es_cero():
    data = _xlsx([
        ["RRV", "RAMON VERA", 127281, 150000],
        ["AIG", "AIDA GUANUCHI", 15676, None],   # vacío → 0, no se saltea
        ["", "fila sin código", 1, 1],           # se ignora
    ])
    filas, avisos = parse_cupos_xlsx(data)
    assert [(f.codigo, f.cupo) for f in filas] == [("RRV", 150000), ("AIG", 0)]
    assert avisos == []


def test_parser_cupo_no_numerico_o_negativo_saltea_con_aviso():
    data = _xlsx([
        ["AAA", "x", 0, "no se"],
        ["BBB", "x", 0, -5],
        ["CCC", "x", 0, 1000],
    ])
    filas, avisos = parse_cupos_xlsx(data)
    assert [(f.codigo, f.cupo) for f in filas] == [("CCC", 1000)]
    assert len(avisos) == 2 and "AAA" in avisos[0] and "BBB" in avisos[1]


def test_parser_codigo_repetido_vale_el_ultimo_con_aviso():
    data = _xlsx([["AAA", "x", 0, 100], ["AAA", "x", 0, 200]])
    filas, avisos = parse_cupos_xlsx(data)
    assert [(f.codigo, f.cupo) for f in filas] == [("AAA", 200)]
    assert len(avisos) == 1 and "repetido" in avisos[0]


def test_parser_elige_la_columna_cargar_no_un_cupo_actual():
    data = _xlsx(
        [["AAA", "CUPO VIEJO...", 999, 1234]],
        headers=["CODIGO", "CLIENTE", "CUPO ACTUAL", "CUPO A CARGAR"],
    )
    filas, _ = parse_cupos_xlsx(data)
    assert filas[0].cupo == 1234


def test_parser_sin_encabezados_devuelve_aviso_y_nada():
    data = _xlsx([], headers=["A", "B", "C", "D"])
    filas, avisos = parse_cupos_xlsx(data)
    assert filas == [] and "encabezados" in avisos[0]


# ----------------------------------------------------------------- rutas ---

@pytest.fixture()
def app_cliente():
    from tests.test_routes_smoke import build_app

    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False

    # En la suite COMPLETA el patch de auth.load_logged_in_user de build_app
    # no agarra (app.py ya importó la función por valor — gotcha documentado):
    # un before_request propio registrado DESPUÉS de create_app() corre
    # último y pisa g.user/g.permisos siempre.
    @app.before_request
    def _login_wildcard():
        from flask import g
        g.user = {"id_usuario": 1, "username": "test", "id_rol": 1,
                  "nombre_rol": "Dueño", "activo": True}
        g.permisos = {"*"}

    yield app
    deshacer()


def test_preview_clasifica_y_no_escribe(app_cliente, monkeypatch):
    from modules.clientes import views

    fichas = [
        {"codigo_cli": "RRV", "nombre": "RAMON VERA", "cupo": None},
        {"codigo_cli": "AIG", "nombre": "AIDA G", "cupo": 100000},
    ]
    monkeypatch.setattr(views.db, "fetch_all", lambda sql, params=None, conn=None: fichas)
    ediciones = []
    monkeypatch.setattr(views.queries, "editar", lambda *a, **k: ediciones.append((a, k)) or 1)

    data = _xlsx([
        ["RRV", "RAMON VERA", 0, 150000],   # cambia (None → 150000)
        ["AIG", "AIDA G", 0, 100000],       # ya estaba así
        ["ZZZ", "NO EXISTE", 0, 5000],      # sin ficha
    ])
    c = app_cliente.test_client()
    r = c.post("/clientes/cupos-carga", data={"archivo": (io.BytesIO(data), "cupos.xlsx")})
    html = r.data.decode()
    assert r.status_code == 200
    assert ediciones == []          # la preview NO escribe
    assert "sin ficha en Programa Core" in html and "ZZZ" in html
    assert "RRV=150000" in html and "AIG=100000" in html
    assert "ZZZ=5000" not in html   # los sin ficha no viajan al confirmar


def test_aplicar_escribe_por_queries_editar(app_cliente, monkeypatch):
    from modules.clientes import views

    ediciones = []

    def fake_editar(codigo, *, cupo=None, usuario="web", **k):
        ediciones.append((codigo, cupo))
        return 1 if codigo == "RRV" else 0

    monkeypatch.setattr(views.queries, "editar", fake_editar)
    c = app_cliente.test_client()
    r = c.post(
        "/clientes/cupos-carga/aplicar",
        data={"payload": "RRV=150000\nAIG=0"},
        follow_redirects=True,
    )
    assert r.status_code == 200
    assert ediciones == [("RRV", 150000), ("AIG", 0)]
    assert "1 ficha(s) actualizadas de 2 filas" in r.data.decode()


def test_aplicar_payload_invalido_es_400(app_cliente, monkeypatch):
    from modules.clientes import views

    monkeypatch.setattr(
        views.queries, "editar",
        lambda *a, **k: pytest.fail("no tiene que escribir"),
    )
    c = app_cliente.test_client()
    r = c.post(
        "/clientes/cupos-carga/aplicar",
        data={"payload": "RRV=150000; DROP TABLE"},
    )
    assert r.status_code == 400


def test_sin_permiso_cupos_editar_es_404():
    from tests.test_routes_smoke import build_app

    app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        # Pisar auth.load_logged_in_user acá NO sirve: app.py lo importó por
        # valor en el import (gotcha documentado). Un before_request propio
        # registrado DESPUÉS de create_app() corre último y pisa g.permisos.
        # OJO: ALL_PERMS es {"*"} (wildcard) — restarle un permiso tampoco
        # sirve; un INT real tiene permisos puntuales, sin cupos.editar
        # (mig 0167).
        @app.before_request
        def _como_int():
            from flask import g
            g.user = {"id_usuario": 2, "username": "int", "id_rol": 3,
                      "nombre_rol": "INT", "activo": True}
            g.permisos = {"clientes.ver", "clientes.editar"}

        c = app.test_client()
        assert c.get("/clientes/cupos-carga").status_code == 404
        # Y el listado NO le muestra el link (gatear los LINKS, no sólo la ruta).
        assert "cupos-carga" not in c.get("/clientes").data.decode()
    finally:
        deshacer()
