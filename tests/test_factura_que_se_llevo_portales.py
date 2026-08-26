"""El número de factura clickea, y lleva a QUÉ SE LLEVÓ el cliente.

TMT 2026-08-25 (dueña): *"en estado de cuenta si clickean en factura que
también vaya. Hacerlo también para vendedores y que puedan compartir"*.

Tres pantallas, un solo parcial de datos:
    oficina  → /facturas/<numf>            (el bloque htmx del ERP)
    vendedor → /mi-cartera/cliente/<c>/factura/<numf>
    cliente  → /factura/<numf>             (el portal)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from werkzeug.exceptions import NotFound

ROOT = Path(__file__).resolve().parent.parent
MOV = ROOT / "modules/mi_cartera/templates/mi_cartera/_movimientos.html"
HOJA_OFI = ROOT / "modules/informes/templates/informes/_estado_cuenta_impreso.html"
QSL = ROOT / "modules/mi_cartera/templates/mi_cartera/_que_se_llevo.html"


# --- el link ---------------------------------------------------------------

def test_el_numero_del_telefono_es_un_link_cuando_hay_a_donde_ir():
    t = MOV.read_text(encoding="utf-8")
    assert "factura_endpoint" in t
    assert "url_for(factura_endpoint" in t


def test_sin_endpoint_el_numero_queda_pelado():
    """La hoja impresa no le pasa endpoint: en el papel un link no sirve."""
    t = MOV.read_text(encoding="utf-8")
    assert "{% if factura_endpoint|default(none) and f.numf %}" in t
    assert "{% else %}" in t


def test_en_la_oficina_el_link_pide_el_permiso_de_facturas():
    """Un link a una pantalla que el usuario no puede abrir es un 404 servido."""
    t = HOJA_OFI.read_text(encoding="utf-8")
    assert "tiene_permiso('facturas.ver')" in t
    assert "url_for('facturas.detalle', id_factura=f.numf)" in t


def test_el_papel_de_la_oficina_no_lleva_link():
    """El link cuelga de `interactivo`, que la impresión nunca setea."""
    t = HOJA_OFI.read_text(encoding="utf-8")
    assert "{% if interactivo|default(false) and f.numf and tiene_permiso" in t


# --- el parcial del teléfono ----------------------------------------------

def test_el_parcial_viaja_con_su_css():
    """Partirlo del CSS no avisa: renderiza sin estilo (24/08)."""
    t = QSL.read_text(encoding="utf-8")
    assert "<style>" in t and ".qsl{" in t


def test_el_telefono_apila_tela_codigo_color_y_calidad():
    """Ocho columnas no entran en 390 px; cuatro cifras sí."""
    t = QSL.read_text(encoding="utf-8")
    # `<th ` y `<th>` por separado: `t.count("<th")` a secas cuenta también el
    # `<thead>` y daba 6 donde hay 5 columnas.
    assert t.count("<th>") + t.count("<th ") == 5
    assert "l.codigo" in t and "l.color" in t and "l.calidad" in t


# --- el scope --------------------------------------------------------------

def test_la_factura_del_cliente_se_encuentra():
    from modules.mi_cartera import views

    data = {"facturas": [{"numf": 9}, {"numf": 182543}]}
    assert views._factura_de(data, 182543)["numf"] == 182543


def test_una_factura_que_no_es_de_este_cliente_da_404():
    """El scope no es un chequeo aparte: la lista ya viene acotada."""
    from modules.mi_cartera import views

    with pytest.raises(NotFound):
        views._factura_de({"facturas": [{"numf": 9}]}, 182543)


def test_tambien_busca_entre_las_totalizadas():
    from modules.mi_cartera import views

    data = {"facturas": [], "facturas_totalizadas": [{"numf": 700}]}
    assert views._factura_de(data, 700)["numf"] == 700


def test_un_numf_roto_no_rompe_la_busqueda():
    from modules.mi_cartera import views

    data = {"facturas": [{"numf": "ochenta"}, {"numf": 5}]}
    assert views._factura_de(data, 5)["numf"] == 5


# ---------------------------------------------------------------------------
# 🚨 La hoja de la factura, para mandarla — TMT 2026-08-25: "si dale"
# ---------------------------------------------------------------------------


def test_la_factura_tambien_se_manda_como_FOTO():
    """Esta pantalla nació el 25/08 con el botón de WhatsApp y el PDF, así que
    arrastra el problema de Alex tal cual: *"desde el pdf q genera no permite
    enviar por wsp"*.

    Es el mismo teléfono, el mismo botón y el mismo archivo que no sabe
    adjuntar. En un teléfono mandar una FOTO lo sabe hacer cualquiera; mandar
    un DOCUMENTO hay que saber que existe Descargas.
    """
    t = (ROOT / "modules" / "mi_cartera" / "templates" / "mi_cartera"
         / "factura.html").read_text(encoding="utf-8")
    assert "mi_cartera.factura_imagen" in t
    # Desde el 26/08 la foto es uno de los cuatro botones de la fila, con su
    # rótulo, y el gesto se dice una vez debajo (ver `_acciones_fila.html`).
    fila = (ROOT / "modules" / "mi_cartera" / "templates" / "mi_cartera"
            / "_acciones_fila.html").read_text(encoding="utf-8")
    assert ">Imagen</span>" in fila
    # El PDF no se saca: es el botón más chico de la fila.
    assert "mi_cartera.factura_pdf" in t


def test_la_fila_de_acciones_viaja_CON_SU_CSS():
    """🐞 TMT 2026-08-25. `factura.html` usaba `.acciones`, `.btn-wa` y
    `.btn-pr` sin incluir la hoja donde estaban definidos —vivían sólo en
    `_ficha_css.html`, que esta pantalla no incluye— así que el botón verde
    salía SIN ESTILO. Medido sobre la página renderizada.

    Es exactamente lo que ya advertía `_que_se_llevo.html` el 24/08 —*"el
    parcial viaja CON SU CSS; partirlo no avisa: renderiza sin estilo"*— y
    volvió a pasar por el mismo motivo. Ahora la CSS vive en su propio parcial
    y la incluyen LAS DOS pantallas que la usan.
    """
    tpl = ROOT / "modules" / "mi_cartera" / "templates" / "mi_cartera"
    acciones = (tpl / "_acciones_css.html").read_text(encoding="utf-8")
    assert ".acc4{" in acciones and ".a4{" in acciones
    assert ".a4.wa{" in acciones and ".a4.img{" in acciones
    assert ".acc4-nota{" in acciones

    for pantalla in ("cliente.html", "factura.html"):
        t = (tpl / pantalla).read_text(encoding="utf-8")
        assert '{% include "mi_cartera/_acciones_fila.html" %}' in t, pantalla
        assert '{% include "mi_cartera/_acciones_css.html" %}' in t, (
            f"{pantalla} usa la fila de acciones sin traer su CSS")

    # Y no quedó una copia vieja suelta: dos definiciones de lo mismo divergen.
    ficha = (tpl / "_ficha_css.html").read_text(encoding="utf-8")
    assert ".a4{" not in ficha, "quedó la copia vieja en _ficha_css"


# --- la ficha de la oficina no espera dos veces ------------------------------
#
# TMT 2026-08-26 (dueña): *"tarda mucho en cargarse"*. El bloque se pedía
# SIEMPRE aparte, aunque la respuesta ya estuviera guardada en la base: un
# segundo viaje, y el cartelito de "buscando el detalle" parpadeando, para algo
# que ya se sabía.

FACTURA_PC = {
    "id_factura": 1, "numf": 175698, "numf_completo": "001-099-000175698",
    "codigo_cli": "ERZ", "cliente": "ERAZO LOGACHO ALEX DAVID",
    "fecha": None, "vencimiento": None, "kg": 168.40, "importe": 1537.34,
    "abono": 0, "retencion": 0, "saldo": 1537.34, "stat": "A",
    "condic": "", "tipo": "F", "pase": "", "clave": "",
}


@pytest.fixture()
def oficina(app, fake_db):
    import bcrypt
    rid = fake_db.add_role("Oficina detalle", ["facturas.ver"])
    uid = fake_db.add_user("ofid", bcrypt.hashpw(b"x", bcrypt.gensalt(rounds=4)),
                           rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    return c


def _ficha(oficina, monkeypatch, guardado):
    from modules.asinfo import factura_lineas
    from modules.facturas import views as fv

    monkeypatch.setattr(fv.queries, "por_id", lambda n: dict(FACTURA_PC))
    monkeypatch.setattr(fv.queries, "cheques_aplicados", lambda *a: [])
    monkeypatch.setattr(fv.queries, "retenciones_aplicadas", lambda *a: [])
    monkeypatch.setattr(factura_lineas, "en_cache", lambda n: guardado)
    return oficina.get("/facturas/175698").data.decode()


def _detalle_guardado():
    from modules.asinfo import factura_lineas as fl
    return {"estado": "ok", **fl._agrupar([{
        "tela": "Fleece 102", "codigo": "PET", "producto": "Fleece 102 PETROLEO",
        "categoria": "TELAS", "color": "PETROLEO", "calidad": "PRIMERA",
        "doc": fl.DOC_FACTURA, "cantidad": 42.8, "precio": 9.25,
        "bruto": 395.9, "descuento": 64.93, "pct1": 5, "pct2": 14}])}


def test_si_el_detalle_ya_esta_guardado_la_ficha_abre_con_el_puesto(
        oficina, monkeypatch):
    html = _ficha(oficina, monkeypatch, _detalle_guardado())
    assert "PETROLEO" in html
    assert "Buscando el detalle en Asinfo" not in html
    assert "/facturas/175698/que-se-llevo" not in html   # ni el segundo viaje


def test_si_todavia_no_se_sabe_la_ficha_abre_igual_y_lo_pide_aparte(
        oficina, monkeypatch):
    """La ficha NUNCA espera a Asinfo para pintar: eso es lo que la hacía lenta
    todos los días antes de que existiera la caché."""
    html = _ficha(oficina, monkeypatch, None)
    assert "/facturas/175698/que-se-llevo" in html
    assert "Buscando el detalle en Asinfo" in html


def test_el_titulo_del_cartelito_tambien_dice_detalle():
    t = (ROOT / "modules" / "facturas" / "templates" / "facturas"
         / "detalle.html").read_text(encoding="utf-8")
    assert ">Detalle</h2>" in t
    assert "Qué se llevó</h2>" not in t
