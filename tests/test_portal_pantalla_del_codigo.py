"""La pantalla del código de 6 números manda el código a /codigo.

🚨 04/09/2026, probando el portal con AJT: el form de `codigo.html` no tenía
`action`, y como la pantalla se muestra como RESPUESTA del POST a /ingresar
(sin redirect), el código volvía a /ingresar. Ahí se lo tomaba como un ingreso
nuevo: se generaba otro código, salía otro mail, y se mostraba la misma
pantalla otra vez. Un cliente real nunca habría podido entrar, y ningún test
lo veía porque los tests le pegaban a /codigo directo.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _app_portal(csrf: bool = False):
    from tests.test_routes_smoke import build_app
    with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
        app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = csrf
    return app, deshacer


def _primera_vez(monkeypatch):
    from modules.portal import acceso, views
    monkeypatch.setattr(acceso, "entrar", lambda *a, **k: {
        "ok": False, "mensaje": "", "codigo_cli": "ATE",
        "cuentas": [{"codigo_cli": "ATE", "nombre": "ALMACENES TEXTILES"}],
        "primera_vez": True})
    monkeypatch.setattr(acceso, "pedir_codigo", lambda cod: ("123456", "compras@ate.com.ec"))
    monkeypatch.setattr(views, "_mandar_el_codigo", lambda seis, mail: None)


def _action_del_form(cuerpo: str) -> str:
    m = re.search(r'<form[^>]*method="post"[^>]*action="([^"]+)"', cuerpo)
    assert m, "el form del código no tiene action"
    return m.group(1)


def test_desde_ingresar_el_codigo_va_a_codigo(monkeypatch):
    app, deshacer = _app_portal()
    try:
        _primera_vez(monkeypatch)
        r = app.test_client().post("/ingresar", data={"identificador": "1791234567001",
                                                       "clave": ""})
        assert r.status_code == 200
        cuerpo = r.get_data(as_text=True)
        assert 'name="seis"' in cuerpo
        assert _action_del_form(cuerpo) == "/codigo"
    finally:
        deshacer()


def test_desde_olvide_la_clave_el_codigo_tambien_va_a_codigo(monkeypatch):
    app, deshacer = _app_portal()
    try:
        from modules.portal import acceso, views
        monkeypatch.setattr(acceso, "cuentas_de", lambda t: [])
        monkeypatch.setattr(views, "_mandar_el_codigo", lambda seis, mail: None)
        r = app.test_client().post("/olvide-la-clave", data={"identificador": "1791234567001"})
        assert r.status_code == 200
        assert _action_del_form(r.get_data(as_text=True)) == "/codigo"
    finally:
        deshacer()


def test_ingresar_con_el_codigo_no_genera_otro_codigo(monkeypatch):
    """El síntoma de verdad: si el código viajara a /ingresar, `pedir_codigo`
    se llamaría otra vez. Con el form apuntando a /codigo, la vuelta entera
    (ingresar → código) pide UN solo código."""
    app, deshacer = _app_portal()
    try:
        from modules.portal import acceso, views
        pedidos = []
        _primera_vez(monkeypatch)
        monkeypatch.setattr(acceso, "pedir_codigo",
                            lambda cod: pedidos.append(cod) or ("123456", "x@y.z"))
        monkeypatch.setattr(acceso, "usar_codigo", lambda cod, seis: seis == "123456")
        monkeypatch.setattr(acceso, "cuentas_de",
                            lambda t: [{"codigo_cli": "ATE", "nombre": "ATE"}])
        monkeypatch.setattr(acceso, "anotar", lambda *a, **k: None)
        monkeypatch.setattr(views, "_mandar_el_codigo", lambda seis, mail: None)
        c = app.test_client()
        r = c.post("/ingresar", data={"identificador": "1791234567001", "clave": ""})
        destino = _action_del_form(r.get_data(as_text=True))
        r2 = c.post(destino, data={"identificador": "1791234567001", "seis": "123456"})
        assert r2.status_code == 302 and r2.headers["Location"].endswith("/elegir-clave")
        assert pedidos == ["ATE"]
    finally:
        deshacer()


def test_un_token_vencido_en_el_portal_no_es_un_500():
    """El manejador del CSRF vencido mandaba a `auth.login`, que en el portal
    no existe: `url_for` explotaba y el cliente veía un 500. Va a la puerta."""
    app, deshacer = _app_portal(csrf=True)
    try:
        # El modo se lee del entorno también al atender el request.
        with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
            r = app.test_client().post("/codigo", data={"identificador": "x", "seis": "1"})
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/ingresar")
    finally:
        deshacer()
