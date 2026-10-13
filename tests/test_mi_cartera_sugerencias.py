"""WhatsApp directo desde «Sugerencias» (ex «Tu hoja»), 13/10/2026.

Tamara: "que de tu hoja salga whatsapp directo". Cada ficha de cliente abre
el chat del cliente con la oferta ya escrita; sin celular, WhatsApp abre la
lista de chats para elegir. Mismas reglas de texto que las sugerencias del
Inicio: de usted, sin "¿", sin decir qué es de segunda.
"""
from __future__ import annotations

from urllib.parse import unquote

from modules.mi_cartera import sugerencias as s


def test_el_texto_de_la_oferta():
    t = s.mensaje_oferta("GARCIA INTRIAGO CARLOS ANDRES",
                         [("Fleece 102", ["NEGRO", "Gris", "", "Azul", "Rojo"]),
                          ("Jersey 3.00", ["Blanco"]), ("Pique", ["Verde"])])
    assert t == ("Hola Carlos, cómo está? Le cuento que tenemos Fleece 102 en "
                 "negro, gris y azul, y también Jersey 3.00 en blanco, listas "
                 "para entregar. Le separo?")
    assert "¿" not in t and "segunda" not in t.lower()
    uno = s.mensaje_oferta("MODAS YURI CIA LTDA", [("Pique", [])])
    assert uno == "Hola, cómo está? Le cuento que tenemos Pique, lista para entregar. Le separo?"


def test_links_con_y_sin_celular(monkeypatch):
    from modules.analisis import queries as aq
    monkeypatch.setattr(aq, "items", lambda conn=None: [
        {"subcategoria": "Fleece 102", "color": "neg", "color_nombre": "Negro", "stock_kg": 10},
        {"subcategoria": "Fleece 102", "color": "BLA", "color_nombre": "", "stock_kg": 5},
        {"subcategoria": "Pique", "color": "ROJ", "color_nombre": "Rojo", "stock_kg": 0}])
    monkeypatch.setattr(s, "_fichas", lambda cods: {
        "GIC": {"nombre": "GARCIA INTRIAGO CARLOS ANDRES", "celular": None, "telefono": "0987654321"},
        "MYU": {"nombre": "MODAS YURI CIA LTDA", "celular": None, "telefono": "022345678"}})
    clientes = [{"codigo": "GIC", "nombre": "x",
                 "telas": [{"subcategoria": "Fleece 102", "colores_parados": "NEG, BLA"}]},
                {"codigo": "MYU", "nombre": "MODAS YURI CIA LTDA",
                 "telas": [{"subcategoria": "Pique", "colores_parados": "ROJ"}]}]
    s.links_oferta(clientes)
    gic, myu = clientes
    assert gic["wa"].startswith("https://wa.me/593987654321?text=") and gic["wa_directo"]
    # El código sin nombre (BLA) sale como código; el de 0 kg no se nombra.
    assert "Fleece 102 en negro y bla," in unquote(gic["wa"])
    assert myu["wa"].startswith("https://wa.me/?text=") and not myu["wa_directo"]
    assert "Pique, lista" in unquote(myu["wa"])


def test_si_algo_falla_la_hoja_sale_sin_boton(monkeypatch):
    from modules.analisis import queries as aq

    def roto(conn=None):
        raise RuntimeError("sin base")
    monkeypatch.setattr(aq, "items", roto)
    clientes = [{"codigo": "GIC", "telas": []}]
    s.links_oferta(clientes)
    assert "wa" not in clientes[0]
