"""Contestarle al cliente que escribió desde el portal, DESDE EL PROGRAMA.

Tamara 12/10/2026, caso PG1 ("NO EXISTEN VALORES"): la respuesta tiene que
salir con la dirección del programa (la misma de los mails del portal), no
desde el Gmail de quien conteste. Lo que protegen estos tests:

- sin el permiso del aviso la pantalla no existe;
- el correo se precarga con el que el cliente dejó en el portal;
- el mail sale UNA vez, al correo del cliente, y si contesta vuelve a la lista
  de "Mensajes del portal";
- queda anotado (para ver en la misma pantalla qué se le contestó), y si el
  mail no sale NO se anota nada;
- el aviso y el mail del mensaje del cliente llevan a esta pantalla.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.portal_aviso import queries  # noqa: E402
from tests.test_portal_aviso import DUENA, OFICINA, _login, _mailer_falso  # noqa: E402

FICHA = {"codigo_cli": "PG1", "nombre": "PG1", "ruc": "06049786430011", "vend": "",
         "correo": "", "telefono": "", "direccion1": "", "direccion2": "",
         "provincia": "", "canton": "", "cupo": None}


def _cliente(monkeypatch, anotados):
    from modules.avisos import queries as avisos
    from modules.portal import acceso, datos
    from modules.portal import mas as portal_mas
    monkeypatch.setattr(acceso, "ficha", lambda cod: dict(FICHA) if cod == "PG1" else None)
    monkeypatch.setattr(acceso, "acceso", lambda cod: {"mail": ""})
    monkeypatch.setattr(datos, "leer", lambda cod: {"correo_facturas": "cliente@gmail.com"})
    monkeypatch.setattr(portal_mas, "mensajes_a", lambda: ["teliscovich@gmail.com"])
    monkeypatch.setattr(avisos, "avisar", lambda **kw: anotados.append(kw) or True)
    monkeypatch.setattr(queries, "conversacion", lambda cod, limite=10: [
        {"de": "cliente", "texto": "NO EXISTEN VALORES", "cuando": "08/10/2026 15:24"}])


def test_sin_el_permiso_no_existe(app, monkeypatch):
    _login(app, OFICINA, {"clientes.ver"})
    _cliente(monkeypatch, [])
    assert app.test_client().get("/portal-aviso/contestar/PG1").status_code == 404


def test_muestra_lo_que_escribio_y_precarga_su_correo(app, monkeypatch):
    _login(app, DUENA, {"portal.avisar"})
    _cliente(monkeypatch, [])
    html = app.test_client().get("/portal-aviso/contestar/PG1").get_data(as_text=True)
    assert "NO EXISTEN VALORES" in html and 'value="cliente@gmail.com"' in html


def test_el_mail_sale_al_cliente_y_queda_anotado(app, monkeypatch):
    _login(app, DUENA, {"portal.avisar"})
    anotados = []
    _cliente(monkeypatch, anotados)
    salidos = _mailer_falso(monkeypatch)
    r = app.test_client().post("/portal-aviso/contestar/PG1", data={
        "para": "cliente@gmail.com", "asunto": "Intela", "texto": "Ya está arreglado."})
    assert r.status_code == 302
    assert len(salidos) == 1 and salidos[0]["a"] == ["cliente@gmail.com"]
    assert salidos[0]["texto"] == "Ya está arreglado."
    assert salidos[0]["responder_a"] == "teliscovich@gmail.com"
    assert anotados and anotados[0]["titulo"] == "Le contestamos a PG1"
    assert "Ya está arreglado." in anotados[0]["detalle"]


def test_si_el_mail_no_sale_no_se_anota(app, monkeypatch):
    from modules._lib import mailer
    _login(app, DUENA, {"portal.avisar"})
    anotados = []
    _cliente(monkeypatch, anotados)
    monkeypatch.setattr(mailer, "enviar", lambda *a, **k: {"ok": False, "motivo": "SES caído"})
    r = app.test_client().post("/portal-aviso/contestar/PG1", data={
        "para": "cliente@gmail.com", "asunto": "Intela", "texto": "hola"})
    assert r.status_code == 200 and "SES caído" in r.get_data(as_text=True)
    assert anotados == []


def test_sin_texto_o_sin_correo_no_manda(app, monkeypatch):
    _login(app, DUENA, {"portal.avisar"})
    _cliente(monkeypatch, [])
    salidos = _mailer_falso(monkeypatch)
    c = app.test_client()
    c.post("/portal-aviso/contestar/PG1", data={"para": "cliente@gmail.com", "texto": "  "})
    c.post("/portal-aviso/contestar/PG1", data={"para": "", "texto": "hola"})
    assert salidos == []


def test_la_conversacion_separa_lo_del_cliente_de_lo_nuestro(monkeypatch):
    import db
    monkeypatch.setattr(db, "fetch_all", lambda sql, params: [
        {"titulo": "Le contestamos a PG1", "detalle": "A x@y.com, por tamara:\nYa está.", "cuando": "12/10"},
        {"titulo": "PG1 escribió desde el portal",
         "detalle": "Pg1 (vendedor —) escribió desde el portal:\nNO EXISTEN VALORES", "cuando": "08/10"}])
    conv = queries.conversacion("pg1")
    assert [m["de"] for m in conv] == ["nosotros", "cliente"]
    assert conv[1]["texto"] == "NO EXISTEN VALORES"


def test_el_mensaje_del_cliente_lleva_a_contestarle():
    fuente = (ROOT / "modules" / "portal" / "mas.py").read_text(encoding="utf-8")
    assert 'url=f"/portal-aviso/contestar/{cod}"' in fuente
    assert "portal-aviso/contestar/{cod}" in fuente.split("mailer.enviar(", 1)[1]
