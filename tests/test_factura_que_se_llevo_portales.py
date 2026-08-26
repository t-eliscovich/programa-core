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
