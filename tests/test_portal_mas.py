"""Las pantallas de "Más" del portal del cliente (04/09/2026, "dale con
todas"): pedidos, avisar un pago, cómo pagar, actividad, su año en kilos,
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


def test_los_pedidos_son_solo_los_suyos(monkeypatch):
    from modules.pedidos import service
    monkeypatch.setattr(service, "por_pedido", lambda: ([
        {"numero": "1", "codigo_cliente": "AJT", "lineas": [], "fecha": "2026-09-01"},
        {"numero": "2", "codigo_cliente": "LUT", "lineas": [], "fecha": "2026-09-01"},
        {"numero": "3", "codigo_cliente": " ajt ", "lineas": [], "fecha": "2026-09-02"},
    ], True))
    monkeypatch.setattr(service, "etapas_por_pedido", lambda mios, activos: {})
    from modules._lib import formulas_memos
    monkeypatch.setattr(formulas_memos, "estados", lambda numeros: {})
    r = mas.pedidos_de("AJT")
    assert r["ok"] and [p["numero"] for p in r["pedidos"]] == ["1", "3"]


def test_si_asinfo_no_contesta_los_pedidos_lo_dicen(monkeypatch):
    from modules.pedidos import service
    monkeypatch.setattr(service, "por_pedido", lambda: ([], False))
    assert mas.pedidos_de("AJT") == {"ok": False, "pedidos": [], "etapas": {}}


def test_el_aviso_de_pago_rechaza_lo_que_no_sirve(monkeypatch):
    escrito = []
    import db
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: escrito.append(params))
    assert mas.guardar_aviso_pago("AJT", "regalo", "10", None, "", "")[0] is False
    assert mas.guardar_aviso_pago("AJT", "cheque", "abc", None, "", "")[0] is False
    assert mas.guardar_aviso_pago("AJT", "cheque", "-5", None, "", "")[0] is False

    class _F:
        filename = "x.exe"
        mimetype = "application/x-msdownload"
    assert mas.guardar_aviso_pago("AJT", "cheque", "10", None, "", "", _F())[0] is False

    class _G:
        filename = "foto.jpg"
        mimetype = "image/jpeg"

        def read(self):
            return b"x" * (mas.TOPE_ARCHIVO + 1)
    assert mas.guardar_aviso_pago("AJT", "cheque", "10", None, "", "", _G())[0] is False
    assert escrito == []


def test_el_aviso_de_pago_bueno_entra_con_el_importe_en_formato_ecuador(monkeypatch):
    escrito = []
    import db
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: escrito.append((sql, params)))

    class _G:
        filename = "comprobante.jpg"
        mimetype = "image/jpeg"

        def read(self):
            return b"jpegdata"
    ok, msg = mas.guardar_aviso_pago("AJT", "transferencia", "1.234,56", "2026-09-04",
                                     " 778899 ", "por la 183341", _G())
    assert ok and "Recibimos" in msg
    sql, params = escrito[0]
    assert "portal_aviso_pago" in sql
    assert params[0] == "AJT" and params[1] == "transferencia" and params[2] == 1234.56
    assert params[4] == "778899" and params[6] == b"jpegdata" and params[8] == "image/jpeg"


def test_pedir_correccion_deja_un_aviso_y_no_toca_la_ficha(monkeypatch):
    from modules.avisos import queries as avisos
    dejados = []
    monkeypatch.setattr(avisos, "avisar", lambda **kw: dejados.append(kw) or True)
    assert mas.pedir_correccion("AJT", "TOTOY BUITRON", "EDG", "la dirección cambió") is True
    assert dejados[0]["titulo"] == "AJT pide corregir sus datos"
    assert "la dirección cambió" in dejados[0]["detalle"]
    assert dejados[0]["url"] == "/clientes/AJT/editar"
    assert mas.pedir_correccion("AJT", "X", "EDG", "   ") is False
    fuente = (ROOT / "modules" / "portal" / "mas.py").read_text(encoding="utf-8")
    assert "UPDATE scintela.cliente" not in fuente


def test_el_anio_en_kilos_trae_doce_meses_con_los_vacios_en_cero(monkeypatch):
    from modules.informes import queries as q
    from modules.portal import mas as m
    hoy = dt.date(2026, 9, 4)
    monkeypatch.setattr(m, "today_ec", lambda: hoy)
    monkeypatch.setattr(q, "compras_por_mes_cliente", lambda cod, meses=12: [
        {"mes": dt.date(2026, 9, 1), "kg": 120.5, "importe": 1500, "facturas": 2},
        {"mes": dt.date(2026, 3, 1), "kg": 241.0, "importe": 3000, "facturas": 4},
    ])
    a = m.anio_en_kilos("AJT")
    assert len(a["meses"]) == 12
    assert a["meses"][0]["mes"] == dt.date(2025, 10, 1) and a["meses"][-1]["mes"] == dt.date(2026, 9, 1)
    assert a["kg"] == 361.5 and a["max_kg"] == 241.0
    assert a["meses"][-1]["pct"] == 50 and a["meses"][-1]["etiqueta"] == "Sep"
    assert a["meses"][0]["kg"] == 0 and a["meses"][0]["pct"] == 0


def test_la_actividad_mezcla_todo_por_fecha():
    hoy = dt.date(2026, 9, 4)
    facturas = presentacion.con_estado([
        {"numf": 1, "numf_completo": "001-099-000000001", "id_factura": 1, "fecha": dt.date(2026, 9, 2),
         "importe": 100, "saldo": 100, "vencimiento": dt.date(2026, 12, 1)},
        {"numf": 2, "numf_completo": "", "id_factura": 2, "fecha": dt.date(2026, 8, 31),
         "importe": -50, "saldo": -50, "vencimiento": None}], hoy)
    pagos = [{"que_es": "Cheque", "no_cheque": "1840", "nombre_banco": "PICHINCHA",
              "dia_ingreso": dt.date(2026, 9, 1), "importe": 300}]
    despachos = [{"numero": "DES-1", "corto": "1", "dia": "2026-09-03", "rollos": 2, "unidades": 0}]
    pedidos = [{"numero": "P9", "fecha": "2026-08-20", "n_lineas": 3}]
    items = mas.actividad(facturas, pagos, despachos, pedidos)
    assert [x["tipo"] for x in items] == ["despacho", "factura", "pago", "credito", "pedido"]
    assert items[2]["titulo"] == "Recibimos su cheque 1840" and items[2]["importe"] == 300
    assert items[3]["titulo"] == "Devolución 2" and items[3]["importe"] == 50


# ---------------------------------------------------------------------------
# Las pantallas
# ---------------------------------------------------------------------------


def test_las_pantallas_de_mas_usan_el_armazon_y_estan_en_el_menu():
    for pantalla in ("mas", "como_pagar", "mis_datos", "mi_anio", "pedidos", "avisar_pago", "actividad"):
        t = (TPL / f"{pantalla}.html").read_text(encoding="utf-8")
        assert '{% extends "portal/_app.html" %}' in t, pantalla
    menu = (TPL / "mas.html").read_text(encoding="utf-8")
    for destino in ("/pedidos", "/avisar-pago", "/como-pagar", "/actividad", "/mi-anio",
                    "/facturas?ver=pagadas", "/mis-datos", "/mis-cuentas", "/salir"):
        assert f'href="{destino}"' in menu, destino
    app_ = (TPL / "_app.html").read_text(encoding="utf-8")
    assert app_.count('href="/mas"') >= 2


def test_sin_sesion_todo_manda_a_la_puerta():
    app, deshacer = _app_portal()
    try:
        for ruta in ("/mas", "/como-pagar", "/mis-datos", "/mi-anio", "/pedidos",
                     "/avisar-pago", "/actividad"):
            r = app.test_client().get(ruta)
            assert r.status_code == 302 and r.headers["Location"].endswith("/ingresar"), ruta
    finally:
        deshacer()


def test_mis_datos_muestra_la_ficha_y_el_pedido_de_correccion_deja_aviso(monkeypatch):
    app, deshacer = _app_portal()
    try:
        _cliente(monkeypatch)
        dejados = []
        monkeypatch.setattr(mas, "pedir_correccion", lambda cod, nombre, vend, texto: dejados.append(texto) or True)
        c = _sesion(app)
        html = c.get("/mis-datos").get_data(as_text=True)
        assert "0997857539" in html and "teliscovich@gmail.com" in html and "Quito" in html.title() or "QUITO" in html
        r = c.post("/mis-datos", data={"texto": "cambió el teléfono"})
        assert r.status_code == 302 and dejados == ["cambió el teléfono"]
    finally:
        deshacer()


def test_avisar_un_pago_por_la_pantalla_guarda_y_avisa_a_la_oficina(monkeypatch):
    app, deshacer = _app_portal()
    try:
        _cliente(monkeypatch)
        guardados, campanita = [], []
        monkeypatch.setattr(mas, "guardar_aviso_pago",
                            lambda *a: guardados.append(a) or (True, "Recibimos su aviso."))
        monkeypatch.setattr(mas, "avisos_de_pago_de", lambda cod, limite=20: [])
        from modules.avisos import queries as avisos
        monkeypatch.setattr(avisos, "avisar", lambda **kw: campanita.append(kw) or True)
        c = _sesion(app)
        assert "Mandar el aviso" in c.get("/avisar-pago").get_data(as_text=True)
        r = c.post("/avisar-pago", data={
            "tipo": "transferencia", "importe": "500", "fecha": "2026-09-04",
            "referencia": "123", "nota": "",
            "comprobante": (io.BytesIO(b"jpg"), "c.jpg", "image/jpeg")},
            content_type="multipart/form-data")
        assert r.status_code == 302
        assert guardados[0][0] == "AJT" and guardados[0][1] == "transferencia"
        assert campanita[0]["titulo"] == "AJT avisa un pago desde el portal"
        assert campanita[0]["url"] == "/clientes/avisos-de-pago"
    finally:
        deshacer()


def test_como_pagar_sin_texto_dice_que_llame(monkeypatch):
    app, deshacer = _app_portal()
    try:
        _cliente(monkeypatch)
        monkeypatch.setattr(mas, "como_pagar", lambda: "")
        html = _sesion(app).get("/como-pagar").get_data(as_text=True)
        assert "Llámenos" in html
        monkeypatch.setattr(mas, "como_pagar", lambda: "Banco Pichincha cta 123")
        html = _sesion(app).get("/como-pagar").get_data(as_text=True)
        assert "Banco Pichincha cta 123" in html
    finally:
        deshacer()


def test_el_inicio_dice_el_cupo_solo_si_lo_tiene(monkeypatch):
    from modules.portal import views
    monkeypatch.setattr(views.acceso, "ficha", lambda cod: {"cupo": 20000})
    c = views._cupo_de("AJT", {"saldo": 5000, "saldo_neto": 5000, "cheques_por_cobrar": 3000})
    assert c == {"cupo": 20000.0, "usado": 8000.0, "libre": 12000.0, "pct": 40}
    monkeypatch.setattr(views.acceso, "ficha", lambda cod: {"cupo": None})
    assert views._cupo_de("AJT", {"saldo": 5000}) is None
    monkeypatch.setattr(views.acceso, "ficha", lambda cod: {"cupo": 0})
    assert views._cupo_de("AJT", {"saldo": 5000}) is None


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


def test_el_buzon_de_la_oficina_pide_permiso_de_cheques(app, monkeypatch):
    import db
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [])
    _login(app, {"id_usuario": 3, "username": "maribel", "nombre_rol": "INT", "activo": True, "vend": None},
           {"clientes.ver"})
    assert app.test_client().get("/clientes/avisos-de-pago").status_code == 404


def test_el_buzon_lista_y_marca_atendido(app, monkeypatch):
    import db
    monkeypatch.setattr(db, "fetch_all", lambda *a, **k: [
        {"id_aviso_pago": 7, "codigo_cli": "AJT", "nombre": "TOTOY", "vend": "EDG",
         "tipo": "transferencia", "importe": 500, "fecha": dt.date(2026, 9, 4),
         "referencia": "123", "nota": None, "archivo_nombre": "c.jpg", "archivo_tipo": "image/jpeg",
         "creado_en": dt.datetime(2026, 9, 4, 15, 0), "atendido_en": None,
         "atendido_por": None, "atendido_nota": None}])
    ejecutado = []
    monkeypatch.setattr(db, "execute", lambda sql, params=None, conn=None: ejecutado.append((sql, params)) or 1)
    _login(app, {"id_usuario": 3, "username": "alex", "nombre_rol": "INT", "activo": True, "vend": None},
           {"cheques.ver"})
    c = app.test_client()
    html = c.get("/clientes/avisos-de-pago").get_data(as_text=True)
    assert "AJT" in html and "Transferencia" in html and "500,00" in html and "Atendido" in html
    r = c.post("/clientes/avisos-de-pago/7/atender", data={"nota": "cheque 1840"})
    assert r.status_code == 302
    assert "atendido_en = now()" in ejecutado[0][0] and ejecutado[0][1] == ("alex", "cheque 1840", 7)
