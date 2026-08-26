"""Qué salió por una guía, y los links de Guías del día.

TMT 2026-08-26 (dueña): *"desde acá quiero links a facturas y a despachos"*.

Lo que protegen, en orden de gravedad:
  1. Que los kilos de la ficha den los MISMOS que el renglón que se clickeó.
     Si dieran otra cosa, la pantalla sería peor que no tenerla.
  2. Que el rollo cuyo peso no coincide con el de la factura salga marcado:
     es la pregunta con la que se entra desde una fila en rojo.
  3. Que no se linkee lo que no existe todavía, ni una nota de entrega por su
     número (el `numf` de una NTEN se repite con el de una factura vieja).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from flask import g, render_template

from modules.asinfo import despacho_lineas as dl

GUIA = "DES-000096542"
FACTURA = "001-099-000182675"


def _fila(tela, producto, lote, kg, factura=FACTURA, kg_factura=None, codigo="BLA"):
    return {"guia": GUIA, "fecha": "2026-08-26", "cliente": "RRV",
            "cliente_fiscal": "VERA VARGAS RAMON RODOLFO",
            "tela": tela, "producto": producto, "codigo": codigo, "lote": lote,
            "kg": kg, "factura": factura,
            "kg_factura": kg if kg_factura is None else kg_factura}


def _guia_normal():
    return [_fila("Cuellos T28", "Cuellos T28 BLANCO", "44/1-0004212199", 1.90),
            _fila("Cuellos T30", "Cuellos T30 BLANCO", "0004151011", 2.55),
            _fila("Jersey 3", "Jersey 3 CELESTE", "0004212301", 21.80, codigo="CEL")]


# --- los números ------------------------------------------------------------

def test_los_kilos_son_los_del_renglon_que_se_clickeo():
    """26,25 kg y 3 rollos: lo mismo que dice Guías del día para esa guía."""
    t = dl.armar(_guia_normal())["totales"]
    assert t["kg"] == 26.25
    assert t["rollos"] == 3
    assert t["kg_factura"] == 26.25


def test_el_rollo_que_no_coincide_sale_marcado():
    """La fila roja de Guías del día, abierta: acá se ve CUÁL rollo es."""
    filas = _guia_normal()
    filas[2]["kg_factura"] = 21.30          # salió 21,80 y se facturó 21,30
    hoja = dl.armar(filas)
    marcados = [ln for ln in hoja["lineas"] if ln["difiere"]]
    assert len(marcados) == 1
    assert marcados[0]["lote"] == "0004212301"
    assert (marcados[0]["kg"], marcados[0]["kg_factura"]) == (21.80, 21.30)


def test_medio_kilo_no_es_una_diferencia():
    """El mismo umbral que la pantalla del día: 0,05 kg."""
    filas = _guia_normal()
    filas[0]["kg_factura"] = 1.94
    assert not dl.armar(filas)["lineas"][0]["difiere"]


def test_el_rollo_sin_factura_no_finge_kilos_facturados():
    filas = _guia_normal()
    filas[1]["factura"] = ""
    filas[1]["kg_factura"] = 0
    hoja = dl.armar(filas)
    sin = [ln for ln in hoja["lineas"] if not ln["factura"]][0]
    assert sin["kg_factura"] is None
    assert sin["difiere"] is False
    assert hoja["totales"]["sin_factura"] == 1
    assert hoja["totales"]["kg_factura"] == 23.70    # sin contar el que falta


def test_una_guia_puede_terminar_en_dos_facturas():
    """La factura cuelga del RENGLÓN, no de la guía."""
    filas = _guia_normal()
    filas[2]["factura"] = "001-099-000182699"
    assert dl.armar(filas)["cabecera"]["facturas"] == [FACTURA, "001-099-000182699"]


# --- el color, que el despacho no guarda ------------------------------------

def test_el_color_sale_del_nombre_del_producto():
    """El renglón del despacho NO tiene atributos: *Jersey 3 CELESTE* menos
    *Jersey 3* es CELESTE."""
    assert dl._color("Jersey 3 CELESTE", "Jersey 3") == "CELESTE"
    assert dl._color("Rib BLANCO-AZU", "Rib") == "BLANCO-AZU"


def test_si_el_nombre_no_empieza_con_la_tela_no_se_corta_a_ciegas():
    assert dl._color("Otra cosa", "Jersey 3") == ""
    assert dl._color("", "") == ""


def test_la_calidad_no_se_inventa():
    """El despacho no la guarda en ninguna parte: no se muestra, en vez de
    ponerle Primera a todo."""
    assert "calidad" not in dl.armar(_guia_normal())["lineas"][0]


# --- la consulta ------------------------------------------------------------

@pytest.mark.parametrize("numero", [None, "", "  ", "96542", "DES-96542",
                                    "DES-000096542'; DROP TABLE x--"])
def test_un_numero_que_no_es_una_guia_no_pregunta_nada(numero):
    with patch("modules._lib.metabase_client.disponible") as m:
        res = dl.que_salio(numero)
    assert res["estado"] == "sin-numero"
    m.assert_not_called()


def test_la_consulta_mide_con_la_misma_vara_que_la_pantalla_del_dia():
    """Bodega de producto terminado y guías sin anular. Con otro filtro, los
    kilos de la ficha no darían los del renglón que se clickeó."""
    from modules.facturas import dia_despacho

    sql = dl._sql(GUIA)
    assert f"ddc.id_bodega = {dia_despacho.BODEGA_PT}" in sql
    assert "dc.fecha_anulacion IS NULL" in sql
    assert dl.UMBRAL_KG == dia_despacho.UMBRAL_KG


def test_la_factura_se_engancha_por_el_renglon_del_despacho():
    assert ("dfc.id_detalle_despacho_cliente = ddc.id_detalle_despacho_cliente"
            in dl._sql(GUIA))


def test_sin_puente_no_es_sin_datos():
    with patch("modules._lib.metabase_client.disponible", return_value=False):
        assert dl.que_salio(GUIA)["estado"] == "sin-puente"


def test_asinfo_no_contesta_es_error_no_guia_vacia():
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([], False)):
        assert dl.que_salio(GUIA)["estado"] == "error"


def test_asinfo_contesta_y_no_conoce_la_guia():
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               return_value=([], True)):
        assert dl.que_salio(GUIA)["estado"] == "sin-datos"


def test_la_excepcion_del_puente_no_sube():
    with patch("modules._lib.metabase_client.disponible", return_value=True), \
         patch("modules._lib.metabase_client.fetch_dataset_estado",
               side_effect=RuntimeError("boom")):
        assert dl.que_salio(GUIA)["estado"] == "error"


# --- la pantalla ------------------------------------------------------------

def _render(app, plantilla, **ctx):
    with app.test_request_context("/despachos/" + GUIA):
        g.user = {"username": "test", "nombre_rol": "Accionista", "rol": 1}
        g.permisos = {"*"}
        return render_template(plantilla, **ctx)


def test_la_ficha_dice_los_kilos_y_el_rollo(app):
    filas = _guia_normal()
    filas[2]["kg_factura"] = 21.30
    html = _render(app, "facturas/despacho.html",
                   d={"estado": "ok", **dl.armar(filas)}, numero=GUIA)
    assert GUIA in html
    assert "26,25" in html                      # los kilos que salieron
    assert "0004212301" in html                 # el número del rollo
    assert "bg-red-50" in html                  # el rollo que no coincide
    assert FACTURA in html


def test_la_ficha_vuelve_a_las_guias_del_dia(app):
    html = _render(app, "facturas/despacho.html",
                   d={"estado": "ok", **dl.armar(_guia_normal())}, numero=GUIA)
    assert "/facturas/dia?fecha=2026-08-26" in html


@pytest.mark.parametrize("estado,dice", [
    ("sin-numero", "no tiene la forma de una guía"),
    ("sin-datos", "no tiene esta guía"),
    ("sin-puente", "no hay conexión con el ERP"),
    ("error", "Volvé a entrar en un rato"),
])
def test_cuando_no_hay_guia_la_ficha_lo_dice(app, estado, dice):
    html = _render(app, "facturas/despacho.html", numero=GUIA,
                   d={"estado": estado, "cabecera": {}, "lineas": [], "totales": {}})
    assert dice in html


def test_la_ruta_de_la_guia_existe_y_pide_el_permiso_de_facturas(app):
    from flask import url_for

    with app.test_request_context():
        assert url_for("facturas.despacho", numero=GUIA) == "/despachos/" + GUIA
    import inspect

    from modules.facturas import views
    assert "facturas.ver" in inspect.getsource(views.despacho)


# --- los links de Guías del día ---------------------------------------------

def _dia(**cambios):
    base = {
        "fecha": "2026-08-26", "asinfo_ok": True,
        "despachado": {"kg": 100.0, "n": 2}, "facturado": {"kg": 100.0, "n": 2,
                                                           "importe": 1000.0},
        "diferencia": 0, "residuo": 0,
        "sin_guia": {"kg": 0, "items": []}, "sin_factura": {"kg": 0, "items": []},
        "sin_cargar": {"kg": 0, "items": [], "kg_nten": 0},
        "sin_autorizar": {"kg": 0, "items": []},
        "numf_por_doc": {FACTURA: 182675, "NTEN-10909": 10909},
        "guias": [
            {"guia": GUIA, "hora": "09:30", "cliente": "RRV", "kg": 278.40,
             "docs": [FACTURA], "en_pc": [FACTURA], "sin_autorizar": [],
             "kg_fact": 278.40, "difiere": False, "facturada": True},
            {"guia": "DES-000096541", "hora": "09:17", "cliente": "BED",
             "kg": 230.50, "docs": ["NTEN-10909"], "en_pc": ["NTEN-10909"],
             "sin_autorizar": [], "kg_fact": 230.50, "difiere": False,
             "facturada": True},
        ],
    }
    base.update(cambios)
    return base


def test_la_guia_linkea_a_lo_que_salio(app):
    html = _render(app, "facturas/dia_despacho.html", d=_dia())
    assert f'href="/despachos/{GUIA}"' in html


def test_la_factura_linkea_a_su_ficha_por_el_numf(app):
    html = _render(app, "facturas/dia_despacho.html", d=_dia())
    assert 'href="/facturas/182675"' in html


def test_la_nota_de_entrega_NO_linkea_por_su_numero(app):
    """🚨 El `numf` de una NTEN se repite con el de una factura vieja de otro
    cliente: `/facturas/10909` abriría la que no es. Va al listado del día
    filtrado por su cliente, que siempre da bien."""
    html = _render(app, "facturas/dia_despacho.html", d=_dia())
    assert 'href="/facturas/10909"' not in html
    assert "cliente=BED" in html


def test_lo_que_todavia_no_esta_cargado_no_se_linkea(app):
    """Un link a una factura que no existe es un 404 servido."""
    d = _dia(numf_por_doc={})
    d["guias"][0]["en_pc"] = []
    html = _render(app, "facturas/dia_despacho.html", d=d)
    assert 'href="/facturas/182675"' not in html
    assert FACTURA in html          # el número se sigue viendo, sin link


def test_el_cuadre_publica_el_numf_de_cada_documento():
    """Sin esto la tabla no puede linkear."""
    import inspect

    from modules.facturas import dia_despacho
    assert '"numf_por_doc"' in inspect.getsource(dia_despacho.cuadre)
