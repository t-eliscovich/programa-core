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


# ---------------------------------------------------------------------------
# Las pantallas
# ---------------------------------------------------------------------------


def test_las_pantallas_de_mas_usan_el_armazon_y_estan_en_el_menu():
    for pantalla in ("mas", "mis_datos", "mi_anio", "pedidos"):
        t = (TPL / f"{pantalla}.html").read_text(encoding="utf-8")
        assert '{% extends "portal/_app.html" %}' in t, pantalla
    menu = (TPL / "mas.html").read_text(encoding="utf-8")
    for destino in ("/pedidos", "/mi-anio", "/mis-datos", "/mis-cuentas", "/salir"):
        assert f'href="{destino}"' in menu, destino
    # Dueña 09/09/2026: "o no sirven o la info está en otros lados".
    for fuera in ("/avisar-pago", "/como-pagar", "/actividad", "/facturas?ver=pagadas"):
        assert fuera not in menu, fuera
    app_ = (TPL / "_app.html").read_text(encoding="utf-8")
    assert app_.count('href="/mas"') >= 2


def test_sin_sesion_todo_manda_a_la_puerta():
    app, deshacer = _app_portal()
    try:
        for ruta in ("/mas", "/mis-datos", "/mi-anio", "/pedidos"):
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


def test_los_pedidos_dicen_medio_rollo_y_un_rollo_como_una_persona(monkeypatch):
    """🐞 09/09/2026, con AJT: "0 rollos" (era medio rollo redondeado a
    entero) y "1 rollos"."""
    app, deshacer = _app_portal()
    try:
        _cliente(monkeypatch)
        monkeypatch.setattr(mas, "pedidos_de", lambda cod: {"ok": True, "etapas": {}, "pedidos": [{
            "numero": "PDCL-1", "fecha": "2026-09-07", "fecha_es": "7 sep", "dias": 2,
            "codigo_cliente": "AJT", "lineas": [
                {"tela": "Alemania 1.2", "color": "BLA", "producto": "p1", "cantidad": 0.5, "unidad": "roll"},
                {"tela": "Kiana Mundial", "color": "NAF", "producto": "p2", "cantidad": 1.0, "unidad": "roll"},
                {"tela": "Pique Especial", "color": "VPI", "producto": "p3", "cantidad": 2.0, "unidad": "roll"},
                {"tela": "Cuellos T40", "color": "ARV", "producto": "p4", "cantidad": 1.0, "unidad": "un"},
                {"tela": "Puños", "color": "SAL", "producto": "p5", "cantidad": 2.0, "unidad": "kg"},
            ]}]})
        html = _sesion(app).get("/pedidos").get_data(as_text=True)
        assert "0,5 rollos" in html and "1 rollo<" in html and "2 rollos" in html
        assert "1 unidad<" in html and "2 kg" in html
        assert "0 rollos" not in html and "1 rollos" not in html
    finally:
        deshacer()


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


def test_el_anio_en_kilos_tambien_sabe_de_otros_anios(monkeypatch):
    """Dueña 09/09/2026: "puede ser también que puedan ver otros años"."""
    from modules.informes import queries as q
    from modules.portal import mas as m
    monkeypatch.setattr(m, "today_ec", lambda: dt.date(2026, 9, 9))
    monkeypatch.setattr(q, "compras_por_mes_cliente_anio", lambda cod, anio: [
        {"mes": dt.date(anio, 2, 1), "kg": 100.0, "importe": 900, "facturas": 1}])
    a = m.anio_en_kilos("AJT", 2025)
    assert a["anio"] == 2025 and a["anios"] == [2026, 2025, 2024, 2023]
    assert [x["mes"] for x in a["meses"]] == [dt.date(2025, mm, 1) for mm in range(1, 13)]
    assert a["meses"][1]["kg"] == 100.0 and a["kg"] == 100.0
    # Un año fuera del selector cae a los últimos 12 meses.
    monkeypatch.setattr(q, "compras_por_mes_cliente", lambda cod, meses=12: [])
    assert m.anio_en_kilos("AJT", 1999)["anio"] is None


def test_que_compro_agrupa_por_tela_y_color_y_los_cuellos_van_en_unidades():
    from modules.asinfo import factura_lineas as fl
    telas = fl._agrupar_anio([
        {"tela": "Jersey 3", "codigo": "NEG", "color": "NEGRO", "categoria": "Jersey", "cantidad": 65.0, "renglones": 3},
        {"tela": "Jersey 3", "codigo": "MAR", "color": "MARINO", "categoria": "Jersey", "cantidad": 130.2, "renglones": 6},
        {"tela": "Cuellos T40", "codigo": "ARV", "color": "ARVEJA", "categoria": "Cuellos", "cantidad": 40, "renglones": 2},
        {"tela": "Rib", "codigo": "CAR", "color": "CARDENILLO", "categoria": "Rib", "cantidad": 0.0, "renglones": 0},
    ])
    assert [t["tela"] for t in telas] == ["Jersey 3", "Cuellos T40"]
    j = telas[0]
    assert j["kg"] == 195.2 and j["rollos"] == 9
    assert [c["codigo"] for c in j["colores"]] == ["MAR", "NEG"]
    assert telas[1]["unidades"] == 40 and telas[1]["kg"] == 0


def test_la_consulta_del_anio_ata_al_cliente_por_codigo_y_ruc_y_resta_las_devoluciones():
    from modules.asinfo import factura_lineas as fl
    sql = fl._sql_anio("AJT", "1724354004", 2026)
    assert "= 'AJT'" in sql and "'1724354004'" in sql
    assert "fc.fecha >= '2026-01-01' AND fc.fecha < '2027-01-01'" in sql
    assert "THEN -dfc.cantidad" in sql and "'SERVICIOS'" in sql


def test_la_pantalla_del_anio_muestra_el_selector_y_las_telas(monkeypatch):
    app, deshacer = _app_portal()
    try:
        _cliente(monkeypatch)
        monkeypatch.setattr(mas, "anio_en_kilos", lambda cod, anio=None: {
            "meses": [], "kg": 0, "importe": 0, "max_kg": 0, "anio": 2025, "anios": [2026, 2025, 2024, 2023]})
        monkeypatch.setattr(mas, "que_compro", lambda cod, ruc, anio: {"estado": "ok", "telas": [
            {"tela": "Jersey 3", "kg": 195.2, "rollos": 9, "unidades": 0,
             "colores": [{"codigo": "MAR", "color": "MARINO", "kg": 130.2, "rollos": 6, "unidades": 0}]}]})
        html = _sesion(app).get("/mi-anio?anio=2025").get_data(as_text=True)
        assert 'href="/mi-anio?anio=2025" class="on"' in html
        assert "Qué compró en 2025" in html and "Jersey 3" in html and "MAR · Marino" in html
        # 🐞 09/09: el bloque había quedado adentro del <title> (un replace
        # sobre el primer endblock). Lo que se ve tiene que estar en <main>.
        assert "<title>Su año en kilos — Intela</title>" in html
        assert html.index("<main>") < html.index("Qué compró en 2025") < html.index("</main>")
    finally:
        deshacer()


def test_que_compro_de_verdad_sin_puente_no_revienta(monkeypatch):
    """🐞 09/09/2026: `/mi-anio` dio 500 en producción por un import con el
    nombre equivocado que ningún test recorría (todos pisaban `que_compro`).
    Acá se llama la función REAL con el puente apagado."""
    from modules._lib import metabase_client
    from modules.asinfo import factura_lineas as fl
    monkeypatch.setattr(metabase_client, "disponible", lambda: False)
    fl._ANIO_CACHE.clear()
    assert fl.que_compro_en_el_anio("AJT", "1724354004001", 2026) == {"estado": "sin-puente", "telas": []}
    assert fl.que_compro_en_el_anio("", "", 2026)["estado"] == "sin-datos"
    assert mas.que_compro("AJT", "1724354004001", None)["estado"] == "sin-puente"


def test_la_pantalla_del_anio_sin_asinfo_lo_dice_y_no_revienta(monkeypatch):
    app, deshacer = _app_portal()
    try:
        _cliente(monkeypatch)
        from modules._lib import metabase_client
        from modules.informes import queries as q
        monkeypatch.setattr(metabase_client, "disponible", lambda: False)
        monkeypatch.setattr(q, "compras_por_mes_cliente", lambda cod, meses=12: [])
        monkeypatch.setattr(q, "compras_por_mes_cliente_anio", lambda cod, anio: [])
        r = _sesion(app).get("/mi-anio?anio=2025")
        assert r.status_code == 200
        assert "No pudimos traer el detalle por tela" in r.get_data(as_text=True)
    finally:
        deshacer()
