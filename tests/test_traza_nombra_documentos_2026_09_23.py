"""Auditoría de la traza del 23/09/2026: cada renglón dice QUÉ documento.

Tamara, sobre 487 ventanas de producción: el tooltip sabía el número y el
renglón no lo decía. Los textos esperados salen de los casos reales.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.informes import eventos as ev  # noqa: E402
from modules.informes import traza as t  # noqa: E402
from tests.test_traza_eventos import _con_label, _ev  # noqa: E402


def _uno(filas_ev, movs, d):
    idx = ev.indice(_con_label(filas_ev))
    return [g["texto"] for g in t.resumir(movs, d, idx)]


def test_deuda_corregida_dice_proveedor_y_antes_despues():
    textos = _uno(
        [_ev("posdat_edit_importe", "posdat", 808, "posdat", 808, -64749.0,
             concepto="Edit importe de C2 · Colourtex: 150.749,00 → 86.000,00",
             metadata={"importe_nuevo": 86000, "importe_prev": 150749, "prov": "C2"})],
        [{"doc_id": "p808", "componente": "totp", "aporte": 64749.0,
          "regla": "Deuda corregida", "etiqueta": "Deuda C2 0 · COLOURTEX",
          "familia": "utilidad"}], 64749.0)
    assert textos == ["deuda C2 corregida: 150.749 → 86.000"], textos


def test_neteo_dice_el_cliente():
    textos = _uno(
        [_ev("neteo_estado_cuenta", "cheque", 104356, "cheque", 100055, 4871.53,
             metadata={"codigo_cli": "VGA", "ids_cheques": [100055, 101520],
                       "ids_anticipos": [104356]})],
        [{"doc_id": "c104356", "componente": "cheques", "aporte": -663.0,
          "regla": "Cheque corregido", "etiqueta": "Cheque 0 · VGA",
          "familia": "traspaso"}], -663.0)
    assert textos == ["neteo VGA · 2 CH ↔ 1 AN"], textos


def test_cambio_de_estado_dice_numero_y_pase():
    textos = _uno(
        [_ev("factura_stat_cambio", "factura", 1, "factura", 1, -250.02,
             concepto="CAMBIO stat factura 001-099-000181549 STP Z→T",
             metadata={"codigo_cli": "STP", "stat_previo": "Z", "stat_nuevo": "T"})],
        [{"doc_id": "f1", "componente": "facturas", "aporte": -250.02,
          "regla": "Factura cancelada del todo",
          "etiqueta": "Factura 001-099-000181549 · STP", "familia": "traspaso"}],
        -250.02)
    assert textos == ["FA #181549 STP Z→T"], textos


def test_varios_cambios_de_estado_iguales_dicen_el_pase():
    filas = [_ev("factura_stat_cambio", "factura", i, "factura", i, -10.0,
                 metadata={"codigo_cli": c, "stat_previo": "A", "stat_nuevo": "T"})
             for i, c in ((1, "JUT"), (2, "AJT"), (3, "GLI"))]
    movs = [{"doc_id": f"f{i}", "componente": "facturas", "aporte": -10.0 * i,
             "regla": "Factura cancelada del todo",
             "etiqueta": f"Factura 00{i} · {c}", "familia": "traspaso"}
            for i, c in ((1, "JUT"), (2, "AJT"), (3, "GLI"))]
    assert _uno(filas, movs, -60.0)[0] == "3 FA A→T · GLI, AJT, JUT"


def test_factura_anulada_dice_numero_y_cliente():
    textos = _uno(
        [_ev("reverso_factura_anulada", "factura", 7, "factura", 7, -199.0,
             metadata={"numf": 184644})],
        [{"doc_id": "f7", "componente": "facturas", "aporte": -199.0,
          "regla": "Factura cancelada del todo",
          "etiqueta": "Factura 001-099-000184644 · OEG", "familia": "utilidad"}],
        -199.0)
    assert textos == ["FA #184644 OEG anulada"], textos


def test_cheque_anulado_por_error_de_carga_dice_numero():
    textos = _uno(
        [_ev("reverso_cheque_administrativo", "cheque", 9, "cheque", 9, 25.0,
             concepto="ANULADO error de carga ch 3175 Z→X")],
        [{"doc_id": "c9", "componente": "cheques", "aporte": -25.0,
          "regla": "Cheque depositado o dado de baja",
          "etiqueta": "Cheque 3175 · CJE", "familia": "utilidad"}], -25.0)
    assert textos == ["CH #3175 CJE anulado (error de carga)"], textos


def test_la_pata_opuesta_no_le_cambia_el_nombre_al_reverso():
    """"FA → CH · NUN, IZA" era un cheque anulado por error de carga."""
    textos = _uno(
        [_ev("reverso_cheque_administrativo", "cheque", 102782, "cheque", 102782,
             -1080.48, concepto="ANULADO error de carga ch 102782 P→X")],
        [{"doc_id": "c102782", "componente": "cheques", "aporte": 1080.48,
          "regla": "Cheque corregido", "etiqueta": "Cheque 2782 · NUN",
          "familia": "traspaso"},
         {"doc_id": "f55", "componente": "facturas", "aporte": -1080.48,
          "regla": "Abono a factura", "etiqueta": "Factura 177 · IZA",
          "familia": "traspaso"}], 0.0)
    assert textos == ["CH #2782 NUN anulado (error de carga)"], textos


def test_cheque_desaplicado_dice_cheque_y_factura():
    textos = _uno(
        [_ev("reverso_cheque_aplicacion", "cheque", 105066, "factura", 3, 1500.0,
             metadata={"numf": 10771})],
        [{"doc_id": "c105066", "componente": "cheques", "aporte": -1500.0,
          "regla": "Cheque corregido", "etiqueta": "Cheque 551 · BAN",
          "familia": "traspaso"},
         {"doc_id": "f3", "componente": "facturas", "aporte": 1500.0,
          "regla": "Abono a factura", "etiqueta": "Factura 10771 · BAN",
          "familia": "traspaso"}], 0.0)
    assert textos == ["↩ CH #551 BAN ✗ FA #10771"], textos


def test_cheque_a_caja_dice_el_cliente():
    textos = _uno(
        [_ev("cheque_efectivo_to_caja", "cheque", 5, "caja", 6, -201.0,
             metadata={"codigo_cli": "WF2"})],
        [{"doc_id": "k6", "componente": "caja", "aporte": -201.0,
          "regla": "Gasto de caja", "etiqueta": "Caja S · DEVOLUCION",
          "familia": "utilidad"}], -201.0)
    assert textos == ["CH WF2 → caja"], textos


def test_numero_sri_largo_se_acorta():
    movs = [{"doc_id": "f1", "componente": "facturas", "aporte": -143.0,
             "regla": "Abono a factura",
             "etiqueta": "Factura 001-099-000175928 · CA2", "familia": "traspaso"}]
    assert t.resumir(movs, -143.0, {})[0]["texto"] == "FA #175928 CA2"


def test_stock_sin_kilos_lo_dice():
    movs = [{"doc_id": "#stock", "componente": "vsto", "aporte": -56.76,
             "regla": "Stock", "etiqueta": "movimiento de stock",
             "familia": "utilidad"}]
    assert (t.resumir(movs, -56.76, {})[0]["texto"]
            == "cambió el valor del stock sin mover kilos")


def test_anticipo_de_mercaderia_ya_recibida_es_un_renglon():
    filas = [_ev("dolares_anticipo", "dolares", i, "transacciones_bancarias", 90 + i,
                 imp, metadata={"cta": c, "concepto": n})
             for i, c, n, imp in ((1, "AI", "20/26", 2170.92), (2, "AC", "48/26", 1437.77))]
    movs = [{"doc_id": f"d{i}", "componente": "antic", "aporte": imp,
             "regla": "Anticipo entregado", "etiqueta": f"Anticipo {c} · {n}",
             "familia": "traspaso"}
            for i, c, n, imp in ((1, "AI", "20/26", 2170.92), (2, "AC", "48/26", 1437.77))]
    movs.append({"doc_id": "#recibidos", "componente": "antic", "aporte": -3608.69,
                 "regla": "Anticipos cuya mercadería ya entró al stock",
                 "etiqueta": t.TXT_ANTICIPO_RECIBIDO, "familia": "traspaso"})
    idx = ev.indice(_con_label(filas))
    out = t.resumir(movs, 0.0, idx)
    assert [g["texto"] for g in out] == ["2 AN entregado · AI 20/26, AC 48/26"]
    assert out[0]["nota"] == "la mercadería ya estaba en stock: no mueve plata"
    assert out[0]["aporte"] == 0.0


def test_dos_importaciones_en_la_misma_ventana_van_juntas():
    """Ventana 10:13 del 23/09: AI 44 por el sintético, AC 37 convertida."""
    filas = [_ev("bap_anticipo_a_compra", "dolares", 3155, "compra", 773, 62631.5,
                 concepto="AC 37 · 3 anticipo(s) → compra #10418",
                 metadata={"ids_anticipos": [3155, 3221], "n_anticipos": 2,
                           "codigo_prov": "AC"})]
    movs = [
        {"doc_id": "d3155", "componente": "antic", "aporte": -40000.0,
         "regla": "Anticipo aplicado", "etiqueta": "Anticipo AC · 37 MAPFRE",
         "familia": "traspaso"},
        {"doc_id": "d3221", "componente": "antic", "aporte": -22631.5,
         "regla": "Anticipo aplicado", "etiqueta": "Anticipo AC · 37/26",
         "familia": "traspaso"},
        {"doc_id": "#recibidos", "componente": "antic", "aporte": -69443.56,
         "regla": "Anticipos cuya mercadería ya entró al stock",
         "etiqueta": t.TXT_ANTICIPO_RECIBIDO, "familia": "traspaso"},
        {"doc_id": "#stock", "componente": "vsto", "aporte": 132075.06,
         "regla": "Stock", "etiqueta": "entró a hil.", "familia": "utilidad"},
    ]
    idx = ev.indice(_con_label(filas))
    with patch.object(t.db, "fetch_all", return_value=[
            {"codigo_prov": "AI", "concepto": "44", "despues": True}]):
        out = t.resumir(movs, 0.0, idx, hasta="2026-09-23 15:13:54+00")
    assert [g["texto"] for g in out] == [
        "AI 44 y AC 37 · entró la mercadería de 2 importaciones"]
    assert out[0]["aporte"] == 0.0


def test_entre_importes_iguales_gana_la_primera_compra_despues():
    filas = [{"codigo_prov": "AI", "concepto": "45", "despues": False},
             {"codigo_prov": "AI", "concepto": "44", "despues": True},
             {"codigo_prov": "AI", "concepto": "43", "despues": True}]
    with patch.object(t.db, "fetch_all", return_value=filas):
        assert t._importacion_del_anticipo(-69443.56, "2026-09-23") == "AI 44"
    with patch.object(t.db, "fetch_all", return_value=filas[:1] + filas[:1]):
        assert t._importacion_del_anticipo(-69443.56, "2026-09-23") == ""


def test_el_banco_con_anticipos_dice_sus_numeros():
    txt = ev._varios(["ANTICIPO AI 44/26", "ANTICIPO AI 20/26", "ANTICIPO AI 20/26",
                      "CLEMENTE"], 4)
    assert txt == "BC · ANTICIPO AI 44/26, 20/26 ×2, CLEMENTE"
    # Si lo que sigue no es un número, se sigue contando como antes.
    assert ev._varios(["ANTICIPO MD", "ANTICIPO MD DEL SALTO"], 2) == "BC · 2 × ANTICIPO MD"
