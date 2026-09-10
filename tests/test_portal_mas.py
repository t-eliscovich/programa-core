"""La pestaña "Perfil" del portal del cliente (desde el 10/09/2026; antes
"Más", con pedidos, avisar un pago, cómo pagar, actividad, su año en kilos,
facturas pagadas, mis datos, y el buzón de la oficina.

Lo que protegen: que el cliente NUNCA escriba plata ni en la ficha (deja
avisos), que un aviso de pago con archivo raro o pesado no entre, que los
pedidos que ve sean SÓLO los suyos, y que el buzón de la oficina pida permiso.
"""
from __future__ import annotations

import datetime as dt
import io
import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.portal import mas, presentacion  # noqa: E402

TPL = ROOT / "modules" / "portal" / "templates" / "portal"


def _app_portal():
    from tests.test_routes_smoke import build_app
    with patch.dict(os.environ, {**os.environ, "MODO": "portal"}):
        app, deshacer = build_app()
    app.config["WTF_CSRF_ENABLED"] = False
    return app, deshacer


def _cliente(monkeypatch):
    from modules.portal import acceso
    fic = {"codigo_cli": "AJT", "nombre": "TOTOY BUITRON ANDRES JULIO", "ruc": "1724354004001",
           "vend": "EDG", "correo": "", "telefono": "0997857539", "direccion1": "FARO",
           "direccion2": "", "provincia": "PICHINCHA", "canton": "QUITO", "cupo": 20000}
    monkeypatch.setattr(acceso, "ficha", lambda cod: dict(fic))
    monkeypatch.setattr(acceso, "cliente", lambda cod: dict(fic))
    monkeypatch.setattr(acceso, "acceso", lambda cod: {"mail": "teliscovich@gmail.com", "activo": True, "clave_hash": "x"})


def _sesion(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["portal_cliente"] = "AJT"
    return c


# ---------------------------------------------------------------------------
# Piezas sueltas
# ---------------------------------------------------------------------------


def test_el_mensaje_no_toca_la_ficha():
    fuente = (ROOT / "modules" / "portal" / "mas.py").read_text(encoding="utf-8")
    assert "UPDATE scintela.cliente" not in fuente
def test_perfil_es_la_ultima_pestana_y_tiene_salir():
    """Dueña 10/09: sin "Más" (se fueron pedidos y su año en kilos); la
    pestaña es "Perfil" = mis datos + cambiar de cuenta + salir."""
    app_ = (TPL / "_app.html").read_text(encoding="utf-8")
    assert app_.count('href="/mis-datos"') >= 2 and ">Perfil<" in app_
    assert '"/mas"' not in app_
    perfil = (TPL / "mis_datos.html").read_text(encoding="utf-8")
    assert '{% extends "portal/_app.html" %}' in perfil
    assert 'href="/salir"' in perfil and 'href="/mis-cuentas"' in perfil
    for fuera in ("mas.html", "pedidos.html", "mi_anio.html"):
        assert not (TPL / fuera).exists(), fuera
def test_sin_sesion_todo_manda_a_la_puerta():
    app, deshacer = _app_portal()
    try:
        for ruta in ("/mis-datos",):
            r = app.test_client().get(ruta)
            assert r.status_code == 302 and r.headers["Location"].endswith("/ingresar"), ruta
    finally:
        deshacer()


def test_mis_datos_muestra_la_ficha_y_el_pedido_de_correccion_deja_aviso(monkeypatch):
    app, deshacer = _app_portal()
    try:
        _cliente(monkeypatch)
        dejados = []
        monkeypatch.setattr(mas, "mensaje_del_cliente", lambda cod, nombre, vend, texto, correo="": dejados.append(texto) or True)
        c = _sesion(app)
        html = c.get("/mis-datos").get_data(as_text=True)
        assert "0997857539" in html and "teliscovich@gmail.com" in html and "Quito" in html.title() or "QUITO" in html
        r = c.post("/mis-datos", data={"texto": "cambió el teléfono"})
        assert r.status_code == 302 and dejados == ["cambió el teléfono"]
    finally:
        deshacer()


def test_el_inicio_NO_muestra_el_cupo():
    """Dueña 04/09/2026: *"no mostremos cupo porque muchos clientes están
    pasados"*. Que nadie lo vuelva a poner sin hablar con ella."""
    inicio = (TPL / "inicio.html").read_text(encoding="utf-8")
    assert "cupo" not in inicio.lower()
    vistas = (ROOT / "modules" / "portal" / "views.py").read_text(encoding="utf-8")
    assert "cupo=" not in vistas


# ---------------------------------------------------------------------------
# El buzón de la oficina
# ---------------------------------------------------------------------------


def _login(app, user, permisos):
    @app.before_request
    def _entrar():  # pragma: no cover - infra de test
        from flask import g, session
        session["user_id"] = user["id_usuario"]
        g.user = user
        g.permisos = set(permisos)


def test_mis_datos_no_repite_el_pedazo_de_direccion_que_asinfo_trae_dos_veces(monkeypatch):
    """🐞 09/09/2026, con AJT: la dirección salía "…Y ALFARO FTE PARQUE
    ECOLOGICO" y debajo "FARO FTE PARQUE ECOLOGICO" — `direccion2` de Asinfo
    es la cola de `direccion1`. Si ya está adentro, no se repite."""
    app, deshacer = _app_portal()
    try:
        _cliente(monkeypatch)
        from modules.portal import acceso
        fic = acceso.ficha("AJT")
        fic.update(direccion1="SOLANO DE QUIÑONEZ OE1-126 Y ALFARO FTE PARQUE ECOLOGICO",
                   direccion2="FARO FTE PARQUE ECOLOGICO")
        monkeypatch.setattr(acceso, "ficha", lambda cod: dict(fic))
        html = _sesion(app).get("/mis-datos").get_data(as_text=True)
        assert html.count("FTE PARQUE ECOLOGICO") == 1
        fic["direccion2"] = "OFICINA 3"
        html = _sesion(app).get("/mis-datos").get_data(as_text=True)
        assert "OFICINA 3" in html
    finally:
        deshacer()


def test_mis_cuentas_no_dice_dos_cuando_hay_una():
    """🐞 09/09/2026: /mis-cuentas decía "Tiene dos cuentas" mostrando una."""
    app, deshacer = _app_portal()
    try:
        c = app.test_client()
        with c.session_transaction() as s:
            s["portal_cliente"] = "AJT"
            s["portal_cuentas"] = [{"codigo_cli": "AJT", "nombre": "TOTOY"}]
        html = c.get("/mis-cuentas").get_data(as_text=True)
        assert "dos cuentas" not in html and "Su cuenta" in html
        with c.session_transaction() as s:
            s["portal_cuentas"] = [{"codigo_cli": "AJO", "nombre": "PUEBLA"}, {"codigo_cli": "AJ2", "nombre": "PUEBLA"}]
            s["portal_cliente"] = "AJO"
        html = c.get("/mis-cuentas").get_data(as_text=True)
        assert "Tiene dos cuentas" in html
    finally:
        deshacer()




def test_el_mensaje_del_cliente_va_a_novedades_y_por_mail_a_la_lista(monkeypatch):
    """Dueña 10/09: "quiero que me llegue a mí también, está ok que vaya".
    Texto libre: un dato mal o "no me gusta el botón" van por el mismo camino."""
    from modules._lib import mailer
    from modules.avisos import queries as avisos
    dejados, salidos = [], []
    monkeypatch.setattr(avisos, "avisar", lambda **kw: dejados.append(kw) or True)
    monkeypatch.setattr(mailer, "enviar", lambda asunto, texto, dest, html="", responder_a="": salidos.append((asunto, texto, dest, responder_a)) or {"ok": True})
    monkeypatch.setattr(mas, "mensajes_a", lambda: ["teliscovich@gmail.com"])
    assert mas.mensaje_del_cliente("AJT", "TOTOY BUITRON", "EDG", "no me gusta el botón", correo="cliente@x.com") is True
    assert dejados[0]["titulo"] == "AJT escribió desde el portal" and "no me gusta el botón" in dejados[0]["detalle"]
    assert dejados[0]["url"] == "/clientes/AJT/editar"
    asunto, texto, dest, responder_a = salidos[0]
    assert dest == ["teliscovich@gmail.com"] and "no me gusta el botón" in texto and responder_a == "cliente@x.com"
    assert "AJT" in asunto
    # Sin lista, sólo Novedades; sin texto, nada.
    salidos.clear()
    monkeypatch.setattr(mas, "mensajes_a", lambda: [])
    assert mas.mensaje_del_cliente("AJT", "X", "EDG", "hola") is True and salidos == []
    assert mas.mensaje_del_cliente("AJT", "X", "EDG", "  ") is False


def test_la_lista_de_mensajes_se_lee_y_se_guarda_limpia(monkeypatch):
    import db
    monkeypatch.setattr(db, "fetch_one", lambda sql, params: {"valor": " a@x.com ,b@y.com,, "})
    assert mas.mensajes_a() == ["a@x.com", "b@y.com"]
    escrito = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: escrito.append(params))
    mas.guardar_mensajes_a(" a@x.com , ,b@y.com ")
    assert escrito[0] == (mas.CLAVE_MENSAJES_A, "a@x.com, b@y.com")
    fuente = (ROOT / "migrations" / "0248_portal_mensajes_a.sql").read_text(encoding="utf8")
    assert "('portal_mensajes_a', 'teliscovich@gmail.com')" in fuente
