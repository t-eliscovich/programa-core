"""Quién lee el aviso del portal (Tamara 02/10/2026, mig 0257): *"después
hay que ver quién lee los mails, ¿podemos hacer eso?"*.

Lo que cuidan estos tests:
* cada mail sale con SU marca: la imagen invisible y el botón la llevan, el
  texto plano no (queda limpio), y la marca queda anotada sólo si salió;
* en el portal, /a/<marca>.gif devuelve un GIF y anota la apertura; /a/<marca>
  anota el clic y sigue al portal — sin estar logueado;
* una marca rara no toca la base, y si la base falla el cliente igual ve
  el portal;
* la pantalla de la oficina muestra Abrió / Clic y el resumen por envío.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.portal_aviso import envio, queries  # noqa: E402

AJT = {"codigo_cli": "AJT", "nombre": "TOTOY BUITRON ANDRES JULIO", "vend": "EDG",
       "correo": "contabilidad@totoy.com"}
MARCA = "AbCdEfGhIjKlMnOpQrStUv"


def _mailer(monkeypatch, ok=True):
    from modules._lib import mailer
    salidos = []

    def enviar(asunto, texto, destinatarios, html="", responder_a=""):
        salidos.append({"texto": texto, "html": html})
        return {"ok": ok, "motivo": "" if ok else "MessageRejected", "id": "m-1"}

    monkeypatch.setattr(mailer, "enviar", enviar)
    monkeypatch.setattr(queries, "tomar_envio", lambda: True)
    monkeypatch.setattr(queries, "soltar_envio", lambda: None)
    monkeypatch.setattr(queries, "ya_avisado", lambda cod: False)
    monkeypatch.setattr(queries, "correo_del_vendedor", lambda v: "")
    return salidos


def test_cada_mail_lleva_su_marca_en_la_imagen_y_en_el_boton(monkeypatch):
    salidos = _mailer(monkeypatch)
    anotados = []
    monkeypatch.setattr(queries, "anotar", lambda *a: anotados.append(a))
    otro = {**AJT, "codigo_cli": "XYZ", "correo": "x@x.com"}
    envio.mandar([AJT, otro], "automatico", contenido="recordatorio")

    marcas = [a[7] for a in anotados]
    assert len(set(marcas)) == 2 and all(len(m) >= 16 for m in marcas)
    for s, m in zip(salidos, marcas, strict=True):
        assert f'{envio.PORTAL_URL}a/{m}.gif' in s["html"]      # la imagen
        assert f'href="{envio.PORTAL_URL}a/{m}"' in s["html"]   # el botón
        assert m not in s["texto"]                              # el texto, limpio


def test_el_mail_que_no_salio_no_guarda_marca(monkeypatch):
    _mailer(monkeypatch, ok=False)
    anotados = []
    monkeypatch.setattr(queries, "anotar", lambda *a: anotados.append(a))
    envio.mandar([AJT], "automatico")
    assert anotados[0][7] == ""


def test_el_lanzamiento_tambien_lleva_marca():
    html = envio.html_del_aviso("AJT", MARCA)
    assert f"a/{MARCA}.gif" in html and f'href="{envio.PORTAL_URL}a/{MARCA}"' in html
    # Sin marca (la vista previa) no hay imagen y el botón va directo.
    assert ".gif" not in envio.html_del_aviso("AJT")


# ---------------------------------------------------------------------------
# Las dos puertas del portal
# ---------------------------------------------------------------------------


def _app_portal():
    from tests.test_routes_smoke import build_app
    with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
        app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["RATELIMIT_ENABLED"] = False
    return app, deshacer


def test_la_imagen_anota_la_apertura_sin_estar_logueado(monkeypatch):
    abiertos = []
    monkeypatch.setattr(queries, "marcar_apertura", lambda m: abiertos.append(m) or 1)
    app, deshacer = _app_portal()
    try:
        r = app.test_client().get(f"/a/{MARCA}.gif")
    finally:
        deshacer()
    assert r.status_code == 200
    assert r.headers["Content-Type"] == "image/gif"
    assert r.data.startswith(b"GIF89a")
    assert "no-store" in r.headers["Cache-Control"]
    assert abiertos == [MARCA]


def test_el_boton_anota_el_clic_y_sigue_al_portal(monkeypatch):
    clics = []
    monkeypatch.setattr(queries, "marcar_clic", lambda m: clics.append(m) or 1)
    app, deshacer = _app_portal()
    try:
        r = app.test_client().get(f"/a/{MARCA}")
    finally:
        deshacer()
    assert r.status_code == 302
    assert "/a/" not in r.headers["Location"]
    assert clics == [MARCA]


def test_una_marca_rara_no_toca_la_base_y_contesta_igual(monkeypatch):
    tocados = []
    monkeypatch.setattr(queries, "marcar_apertura", lambda m: tocados.append(m))
    monkeypatch.setattr(queries, "marcar_clic", lambda m: tocados.append(m))
    app, deshacer = _app_portal()
    try:
        c = app.test_client()
        r1 = c.get("/a/corta.gif")
        r2 = c.get("/a/x'%3B--drop")
    finally:
        deshacer()
    assert r1.status_code == 200 and r2.status_code == 302
    assert not tocados


def test_si_la_base_falla_el_cliente_igual_entra(monkeypatch):
    def roto(m):
        raise RuntimeError("sin base")

    monkeypatch.setattr(queries, "marcar_clic", roto)
    app, deshacer = _app_portal()
    try:
        r = app.test_client().get(f"/a/{MARCA}")
    finally:
        deshacer()
    assert r.status_code == 302


# ---------------------------------------------------------------------------
# La pantalla de la oficina
# ---------------------------------------------------------------------------


def test_la_pantalla_muestra_abrio_clic_y_el_resumen(app, monkeypatch):
    from tests.test_portal_aviso import DUENA, VENDEDORES, _login

    _login(app, DUENA, {"portal.avisar"})
    fila = {**AJT, "saldo": 100, "vencido": 0, "de_donde": "asinfo", "entro": False,
            "ultimo_aviso": datetime(2026, 10, 12, 14, 0, tzinfo=UTC),
            "ultimo_aviso_ok": True, "ultima_compra": date(2026, 9, 1),
            "con_marca": True,
            "abierto_en": datetime(2026, 10, 12, 15, 5, tzinfo=UTC),
            "clic_en": None}
    vieja = {**fila, "codigo_cli": "OLD", "con_marca": False, "abierto_en": None}
    monkeypatch.setattr(queries, "lista", lambda: [fila, vieja])
    monkeypatch.setattr(queries, "historial", lambda limite=200: [])
    monkeypatch.setattr(queries, "a_clientes_encendido", lambda: True)
    monkeypatch.setattr(queries, "vendedores", lambda: VENDEDORES)
    monkeypatch.setattr(queries, "resumen_envios", lambda limite=6: [
        {"dia": date(2026, 10, 12), "enviados": 400, "con_marca": 400, "abrieron": 120, "clic": 30},
        {"dia": date(2026, 10, 2), "enviados": 870, "con_marca": 0, "abrieron": 0, "clic": 0},
    ])
    cuerpo = app.test_client().get("/portal-aviso").get_data(as_text=True)
    assert "Quién lo leyó" in cuerpo
    assert "120" in cuerpo and "(30 %)" in cuerpo and "(8 %)" in cuerpo
    assert "sin seguimiento" in cuerpo
    fila_ajt = cuerpo.split('name="codigos" value="AJT"')[1].split("</tr>")[0]
    assert "12/10 10:05" in fila_ajt  # abrió (hora de Ecuador)
    assert ">no<" in fila_ajt              # clic: todavía no
    fila_old = cuerpo.split('name="codigos" value="OLD"')[1].split("</tr>")[0]
    assert ">no<" not in fila_old          # sin marca: "—", no "no"
