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
    assert t.count("<th") == 5
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
