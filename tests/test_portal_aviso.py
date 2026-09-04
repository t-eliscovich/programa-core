"""El aviso del portal a los clientes, por mail (fase 4).

TMT 2026-09-04 (dueña): *"fase 4 hagámosla por mail por el momento"* y *"hasta
no testear no mandamos nada a los clientes"*. Lo que protegen estos tests:

* que a los CLIENTES no les salga nada con el interruptor apagado, aunque
  alguien apriete el botón — la prueba a la casa sí sale siempre;
* que el mail no lleve el monto, y que la respuesta le llegue al vendedor;
* que el correo se resuelva en el MISMO orden que el código de entrada al
  portal (el que confirmó él → la ficha → Asinfo);
* que la pantalla la vea sólo quien tiene `portal.avisar`.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.portal_aviso import envio, queries  # noqa: E402

DUENA = {"id_usuario": 1, "username": "tamara", "nombre_rol": "Accionista",
         "activo": True, "vend": None}
OFICINA = {"id_usuario": 3, "username": "maribel", "nombre_rol": "INT",
           "activo": True, "vend": None}

AJT = {"codigo_cli": "AJT", "nombre": "TOTOY BUITRON ANDRES JULIO", "vend": "EDG",
       "saldo": 10225.02, "vencido": 0, "correo": "contabilidad@totoy.com",
       "de_donde": "asinfo", "entro": True, "ultimo_aviso": None, "ultimo_aviso_ok": None}
SIN = {**AJT, "codigo_cli": "SIN", "nombre": "SIN CORREO", "correo": "", "de_donde": ""}


def _login(app, user, permisos):
    @app.before_request
    def _entrar():  # pragma: no cover - infra de test
        from flask import g, session
        session["user_id"] = user["id_usuario"]
        g.user = user
        g.permisos = set(permisos)


def _mailer_falso(monkeypatch):
    from modules._lib import mailer
    salidos = []

    def enviar(asunto, texto, destinatarios, html="", responder_a=""):
        salidos.append({"asunto": asunto, "texto": texto, "a": list(destinatarios),
                        "html": html, "responder_a": responder_a})
        return {"ok": True, "motivo": "", "id": "m-1", "enviados": 1, "fallidos": 0}

    monkeypatch.setattr(mailer, "enviar", enviar)
    return salidos


# ---------------------------------------------------------------------------
# El mensaje
# ---------------------------------------------------------------------------


def test_el_nombre_sale_como_persona_y_no_a_los_gritos():
    assert envio.nombre_lindo("TOTOY BUITRON ANDRES JULIO") == "Totoy Buitron Andres Julio"
    assert envio.nombre_lindo("") == "cliente"


def test_el_mail_no_lleva_el_monto_y_lleva_la_puerta():
    """Quien lo reciba por error se entera de que es cliente de Intela, y
    nada más. Y el botón va a la puerta del portal, que le pide el RUC."""
    for cuerpo in (envio.texto_del_aviso(AJT["nombre"], date(2026, 9, 7)),
                   envio.html_del_aviso(AJT["nombre"], date(2026, 9, 7))):
        assert "Totoy Buitron" in cuerpo
        assert "07/09/2026" in cuerpo
        assert "portal.intela.com.ec" in cuerpo
        assert "RUC" in cuerpo
        assert "10.225" not in cuerpo and "$" not in cuerpo


# ---------------------------------------------------------------------------
# Mandar
# ---------------------------------------------------------------------------


def test_la_prueba_va_a_la_casilla_nuestra_y_no_al_cliente(monkeypatch):
    salidos = _mailer_falso(monkeypatch)
    anotado = []
    monkeypatch.setattr(queries, "anotar", lambda *a: anotado.append(a))
    monkeypatch.setattr(queries, "correo_del_vendedor", lambda v: "edgar@intela.com.ec")
    r = envio.mandar([AJT], "tamara", tipo="prueba", a="teliscovich@gmail.com")
    assert r == {"enviados": 1, "fallidos": 0, "sin_correo": 0}
    assert salidos[0]["a"] == ["teliscovich@gmail.com"]
    assert salidos[0]["responder_a"] == "edgar@intela.com.ec"
    assert salidos[0]["html"]
    # Queda anotado como PRUEBA, no como aviso al cliente.
    assert anotado[0][0] == "AJT" and anotado[0][2] == "prueba"


def test_al_cliente_le_va_a_su_correo_y_el_que_no_tiene_se_cuenta(monkeypatch):
    salidos = _mailer_falso(monkeypatch)
    monkeypatch.setattr(queries, "anotar", lambda *a: None)
    monkeypatch.setattr(queries, "correo_del_vendedor", lambda v: "")
    r = envio.mandar([AJT, SIN], "tamara")
    assert r == {"enviados": 1, "fallidos": 0, "sin_correo": 1}
    assert salidos[0]["a"] == ["contabilidad@totoy.com"]
    assert salidos[0]["responder_a"] == ""


def test_un_mail_que_no_sale_queda_anotado_con_el_motivo(monkeypatch):
    from modules._lib import mailer
    monkeypatch.setattr(mailer, "enviar", lambda *a, **k: {
        "ok": False, "motivo": "MessageRejected", "id": ""})
    anotado = []
    monkeypatch.setattr(queries, "anotar", lambda *a: anotado.append(a))
    monkeypatch.setattr(queries, "correo_del_vendedor", lambda v: "")
    r = envio.mandar([AJT], "tamara")
    assert r["fallidos"] == 1
    assert anotado[0][3] is False and anotado[0][4] == "MessageRejected"


# ---------------------------------------------------------------------------
# El correo se resuelve como en el portal
# ---------------------------------------------------------------------------


def test_el_correo_del_portal_gana_a_la_ficha_y_la_ficha_a_asinfo():
    assert queries._resolver({"mail_portal": "p@x", "correo_ficha": "f@x", "mail_asinfo": "a@x"}) == ("p@x", "portal")
    assert queries._resolver({"mail_portal": "", "correo_ficha": "f@x", "mail_asinfo": "a@x"}) == ("f@x", "ficha")
    assert queries._resolver({"mail_portal": None, "correo_ficha": " ", "mail_asinfo": "a@x"}) == ("a@x", "asinfo")
    assert queries._resolver({}) == ("", "")


def test_la_lista_es_la_de_los_estados_de_cuenta_con_saldo_a_favor_nuestro(monkeypatch):
    from modules.informes import queries as iq
    monkeypatch.setattr(iq, "estado_cuenta_clientes_saldos", lambda: [
        {"codigo_cli": "AJT", "nombre": "TOTOY", "vend": "EDG", "saldo": 100, "vencido": 0},
        {"codigo_cli": "NEG", "nombre": "A FAVOR", "vend": "EDG", "saldo": -5, "vencido": 0},
    ])
    monkeypatch.setattr(queries, "_correos_y_portal", lambda codigos: [
        {"codigo_cli": "AJT", "correo_ficha": "", "mail_portal": "", "mail_asinfo": "a@x",
         "eligio_clave": True, "ultimo_aviso": None, "ultimo_aviso_ok": None}])
    filas = queries.lista()
    assert [f["codigo_cli"] for f in filas] == ["AJT"]
    assert filas[0]["correo"] == "a@x" and filas[0]["entro"] is True


# ---------------------------------------------------------------------------
# La pantalla
# ---------------------------------------------------------------------------


def _pantalla_lista(monkeypatch, encendido=False):
    monkeypatch.setattr(queries, "lista", lambda: [AJT, SIN])
    monkeypatch.setattr(queries, "historial", lambda limite=200: [])
    monkeypatch.setattr(queries, "a_clientes_encendido", lambda: encendido)


def test_sin_el_permiso_no_existe(app, monkeypatch):
    _login(app, OFICINA, {"clientes.ver", "bitacora.ver"})
    _pantalla_lista(monkeypatch)
    assert app.test_client().get("/portal-aviso").status_code == 404


def test_la_pantalla_muestra_a_quien_le_va_y_nace_apagada(app, monkeypatch):
    _login(app, DUENA, {"portal.avisar"})
    _pantalla_lista(monkeypatch)
    r = app.test_client().get("/portal-aviso")
    assert r.status_code == 200
    cuerpo = r.get_data(as_text=True)
    assert "AJT" in cuerpo and "contabilidad@totoy.com" in cuerpo
    assert "sin correo" in cuerpo
    assert "APAGADO" in cuerpo
    assert "Mandarme una prueba" in cuerpo
    # El botón de mandar a clientes está, pero deshabilitado.
    assert "disabled" in cuerpo.split("Mandar a los marcados")[0][-400:]


def test_apagado_el_boton_de_mandar_no_manda_nada(app, monkeypatch):
    """🚨 El freno de la dueña. Aunque alguien haga el POST a mano."""
    _login(app, DUENA, {"portal.avisar"})
    _pantalla_lista(monkeypatch, encendido=False)
    mandados = []
    monkeypatch.setattr(envio, "mandar_en_fondo", lambda filas, quien: mandados.append(filas))
    r = app.test_client().post("/portal-aviso/mandar", data={"codigos": ["AJT"]})
    assert r.status_code == 302
    assert mandados == []


def test_prendido_manda_a_los_marcados_con_correo(app, monkeypatch):
    _login(app, DUENA, {"portal.avisar"})
    _pantalla_lista(monkeypatch, encendido=True)
    mandados = []
    monkeypatch.setattr(envio, "mandar_en_fondo", lambda filas, quien: mandados.append((filas, quien)))
    app.test_client().post("/portal-aviso/mandar", data={"codigos": ["AJT", "SIN"]})
    assert len(mandados) == 1
    filas, quien = mandados[0]
    assert [f["codigo_cli"] for f in filas] == ["AJT"]
    assert quien == "tamara"


def test_la_prueba_sale_aunque_este_apagado(app, monkeypatch):
    _login(app, DUENA, {"portal.avisar"})
    _pantalla_lista(monkeypatch, encendido=False)
    llamadas = []
    monkeypatch.setattr(envio, "mandar",
                        lambda filas, quien, tipo="cliente", a="": llamadas.append((filas, tipo, a))
                        or {"enviados": 1, "fallidos": 0, "sin_correo": 0})
    app.test_client().post("/portal-aviso/prueba",
                           data={"codigo_cli": "AJT", "a": "teliscovich@gmail.com"})
    assert llamadas == [([AJT], "prueba", "teliscovich@gmail.com")]


def test_la_prueba_sin_correo_no_manda(app, monkeypatch):
    _login(app, DUENA, {"portal.avisar"})
    _pantalla_lista(monkeypatch)
    llamadas = []
    monkeypatch.setattr(envio, "mandar", lambda *a, **k: llamadas.append(1))
    app.test_client().post("/portal-aviso/prueba", data={"codigo_cli": "AJT", "a": ""})
    assert llamadas == []


def test_el_interruptor_se_prende_y_se_apaga(app, monkeypatch):
    _login(app, DUENA, {"portal.avisar"})
    _pantalla_lista(monkeypatch)
    estados = []
    monkeypatch.setattr(queries, "encender_a_clientes", lambda p: estados.append(p))
    c = app.test_client()
    c.post("/portal-aviso/interruptor", data={"prender": "1"})
    c.post("/portal-aviso/interruptor", data={"prender": "0"})
    assert estados == [True, False]


def test_el_interruptor_vive_en_la_base_y_nace_en_cero():
    """La migración lo siembra en '0'; sin la fila, apagado."""
    fuente = (ROOT / "migrations" / "0242_portal_aviso.sql").read_text(encoding="utf8")
    assert "('portal_aviso_a_clientes', '0')" in fuente
    assert queries.CLAVE_INTERRUPTOR == "portal_aviso_a_clientes"
