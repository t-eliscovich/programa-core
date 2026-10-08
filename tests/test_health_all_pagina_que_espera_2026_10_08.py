"""Tamara 2026-10-08: la rutina entra con Chrome a /admin/health/all y la
pestaña se congelaba esperando el JSON. Al navegador que entra se le da una
página liviana que se pide el JSON sola; los fetch siguen recibiendo JSON."""
from __future__ import annotations

from modules.admin_dbase import health_audit_view as h


def _login(app):
    from flask import g

    @app.before_request
    def _l():
        g.user = {"id_usuario": 0, "username": "t", "id_rol": 0,
                  "nombre_rol": "Accionista", "activo": True, "vend": None}
        g.permisos = {"*"}


def test_el_navegador_recibe_la_pagina_al_toque(app, monkeypatch):
    _login(app)
    llamado = []
    monkeypatch.setattr(h, "usuario_crea_audit", lambda: llamado.append(1))
    r = app.test_client().get("/admin/health/all", headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and r.content_type.startswith("text/html")
    assert "/admin/health/all?json=1" in html and 'id="json"' in html
    assert r.headers["Cache-Control"] == "no-store"
    assert llamado == [], "la página no corre los chequeos: los pide el fetch"


def test_quien_decide_que_entra_un_navegador(app):
    with app.test_request_context("/admin/health/all", headers={
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}):
        assert h._entra_un_navegador() is True
    with app.test_request_context("/admin/health/all?json=1", headers={
            "Accept": "text/html"}):
        assert h._entra_un_navegador() is False
    for acc in ("*/*", "application/json", "application/json, text/html;q=0.5"):
        with app.test_request_context("/admin/health/all", headers={"Accept": acc}):
            assert h._entra_un_navegador() is False, acc
    with app.test_request_context("/admin/health/all"):
        assert h._entra_un_navegador() is False
