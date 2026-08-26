"""La hoja de la factura, en las tres pantallas y en el PDF de WhatsApp.

TMT 2026-08-26 (dueña): *"eso para que los vendedores envíen"* y *"tenemos que
poder descargar esto desde el programa core los demás empleados"*.

    oficina  → /facturas/<id>/papel                       (facturas.ver)
    vendedor → /mi-cartera/cliente/<c>/factura/<n>/hoja    (micartera.ver)
    cliente  → /factura/<n>/papel                          (su propio portal)

Lo que protegen:
  1. Que las tres sirvan LA MISMA hoja. Si el cliente y el vendedor vieran dos
     facturas distintas, la discusión no se puede tener.
  2. Que el PDF y la foto que salen por WhatsApp lleven esa hoja y no otra.
  3. Que si Asinfo no contesta, el vendedor no se quede con la mano vacía
     frente al cliente: se cae a la hoja de siempre.
  4. Que nadie vea la factura de otro.
"""
from __future__ import annotations

import pytest

from modules._lib import imagen_motor, pdf_motor
from modules.asinfo import factura_papel as fp
from modules.mi_cartera import queries as q
from tests.test_estado_cuenta_pdf import oficina, vendedor  # noqa: F401
from tests.test_factura_papel import CLAVE, _filas
from tests.test_mi_cartera import _ec_con_facturas

PLANTILLA = "informes/factura_papel.html"


@pytest.fixture(autouse=True)
def _limpiar_cache():
    fp.reset_cache()
    yield
    fp.reset_cache()


def _asinfo_contesta(monkeypatch, filas=None):
    """Asinfo contesta con la 001-099-000182675, sin tocar la caché de la base."""
    monkeypatch.setattr(fp, "_de_la_base", lambda n: None)
    monkeypatch.setattr(fp, "_guardar", lambda n, r: None)
    monkeypatch.setattr("modules._lib.metabase_client.disponible", lambda: True)
    monkeypatch.setattr("modules._lib.metabase_client.fetch_dataset_estado",
                        lambda db, sql, **kw: (filas if filas is not None
                                               else _filas(), True))


def _asinfo_mudo(monkeypatch):
    monkeypatch.setattr(fp, "_de_la_base", lambda n: None)
    monkeypatch.setattr("modules._lib.metabase_client.disponible", lambda: False)


def _cartera_del_vendedor(monkeypatch):
    from modules.mi_cartera import views

    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: True)
    monkeypatch.setattr(views.informes_queries, "estado_cuenta_cliente",
                        _ec_con_facturas)


# --- el vendedor ------------------------------------------------------------

def test_la_hoja_del_vendedor_es_la_factura_de_asinfo(vendedor, monkeypatch):  # noqa: F811
    _cartera_del_vendedor(monkeypatch)
    _asinfo_contesta(monkeypatch)
    r = vendedor.get("/mi-cartera/cliente/TDV/factura/101/hoja")
    html = r.data.decode()
    assert r.status_code == 200
    assert "FACTURA: 001-099-000182675" in html
    assert CLAVE in html
    assert "2.620,30" in html
    assert "<rect" in html          # el código de barras de la clave


def test_sin_asinfo_el_vendedor_no_se_queda_con_la_mano_vacia(
        vendedor, monkeypatch):  # noqa: F811
    """Está parado frente al cliente: el resumen de siempre es mejor que nada."""
    _cartera_del_vendedor(monkeypatch)
    _asinfo_mudo(monkeypatch)
    r = vendedor.get("/mi-cartera/cliente/TDV/factura/101/hoja")
    html = r.data.decode()
    assert r.status_code == 200
    assert "FACTURA: 001-099-000182675" not in html
    assert "Factura 101" in html          # la hoja vieja, la de Qué se llevó


def test_la_factura_sin_numero_del_sri_cae_en_la_hoja_vieja(
        vendedor, monkeypatch):  # noqa: F811
    """La 100 de la cartera de prueba no tiene `numf_completo`."""
    _cartera_del_vendedor(monkeypatch)
    _asinfo_contesta(monkeypatch)
    html = vendedor.get("/mi-cartera/cliente/TDV/factura/100/hoja").data.decode()
    assert "Factura 100" in html
    assert "CLAVE DE ACCESO" not in html


@pytest.mark.parametrize("formato,motor,blob", [
    ("pdf", pdf_motor, b"%PDF-1.4"),
    ("imagen", imagen_motor, b"\x89PNG"),
])
def test_lo_que_sale_por_whatsapp_lleva_la_factura_copiada(
        vendedor, monkeypatch, formato, motor, blob):  # noqa: F811
    """El PDF y la foto se arman IMPRIMIENDO la hoja: si la hoja cambia, el
    archivo cambia solo. Acá se mira el html que recibe el motor."""
    _cartera_del_vendedor(monkeypatch)
    _asinfo_contesta(monkeypatch)
    visto = {}

    def _capturar(html, **kw):
        visto["html"] = html
        return blob

    monkeypatch.setattr(motor, "desde_html", _capturar)
    r = vendedor.get(f"/mi-cartera/cliente/TDV/factura/101/{formato}")
    assert r.status_code == 200
    assert "FACTURA: 001-099-000182675" in visto["html"]
    assert CLAVE in visto["html"]


def test_el_vendedor_no_puede_pedir_la_factura_de_un_cliente_ajeno(
        vendedor, monkeypatch):  # noqa: F811
    monkeypatch.setattr(q, "cliente_es_mio", lambda vend, cod: False)
    assert vendedor.get(
        "/mi-cartera/cliente/OTRO/factura/101/hoja").status_code in (403, 404)


# --- la oficina -------------------------------------------------------------

def test_la_oficina_tiene_su_propia_puerta_a_la_misma_hoja(app):
    """La ruta existe y pide el permiso de facturas, no otro."""
    from flask import url_for

    with app.test_request_context():
        assert url_for("facturas.papel", id_factura=1) == "/facturas/1/papel"


def test_la_oficina_sin_permiso_no_la_abre(oficina):  # noqa: F811
    """El rol de la fixture tiene `informes.ver`, no `facturas.ver`."""
    assert oficina.get("/facturas/1/papel").status_code == 404


# --- el cliente -------------------------------------------------------------

def test_el_portal_del_cliente_tiene_su_ruta():
    """El portal corre en OTRO proceso (modo portal) y no registra ni un
    blueprint del ERP, así que su ruta se mira en el archivo."""
    from pathlib import Path

    fuente = Path("modules/portal/views.py").read_text(encoding="utf-8")
    assert '@portal_bp.route("/factura/<int:numf>/papel"' in fuente


def test_el_portal_ENCUENTRA_la_hoja():
    """🚨 El bug que casi se va: la hoja vivía en `modules/facturas/templates`
    y el portal sólo tiene prestadas las carpetas de `informes` y de
    `mi_cartera`. La ruta existía, el permiso estaba bien, y la factura del
    cliente moría con TemplateNotFound en producción.

    Este test levanta el proceso EN MODO PORTAL y le pide la plantilla.
    """
    import os
    from unittest.mock import patch

    from tests.test_routes_smoke import build_app

    entorno = {**os.environ, "MODO": "portal"}
    with patch.dict(os.environ, entorno, clear=True):
        app, deshacer = build_app()
    try:
        assert app.jinja_env.get_template(PLANTILLA) is not None
    finally:
        deshacer()


def test_una_factura_ajena_no_se_abre_desde_el_portal():
    """El scope no es un chequeo aparte: la factura se busca DENTRO del estado
    de cuenta del cliente que está adentro, y si no está, es 404."""
    import inspect

    from modules.portal import views as pv

    fuente = inspect.getsource(pv.factura_papel_cliente)
    assert "_cargar_estado_cuenta(cod)" in fuente
    assert "abort(404)" in fuente


# --- una sola hoja para las tres --------------------------------------------

def test_las_tres_pantallas_dibujan_la_misma_plantilla():
    """Tres hojas distintas divergen a la primera corrección que se le hace a
    una sola. La única forma de que no pase es que sea el mismo archivo."""
    import inspect

    from modules.facturas import views as fv
    from modules.mi_cartera import views as mv
    from modules.portal import views as pv

    assert PLANTILLA in inspect.getsource(fv.papel)
    assert PLANTILLA in inspect.getsource(mv._hoja_html)
    assert PLANTILLA in inspect.getsource(pv.factura_papel_cliente)
