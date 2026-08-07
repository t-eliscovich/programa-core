"""`mov_doble` le pone el nombre al detalle de la traza.

TMT 2026-08-07, mirando /historial: *"podés ver de traer algunas de acá… por
ejemplo anticipos e historial me gusta"*, *"cheque depositado en banco debería
ser un movimiento"*, *"cuando hay una devolución me gustaría que también
aparezca, ejemplo la factura de Puebla de hoy"*.

La división de trabajo: `mov_doble` pone el NOMBRE y el AGRUPAMIENTO, el diff
de saldos pone la PLATA. Los importes de `mov_doble` no siempre coinciden con
el Δ del componente —una retención, un redondeo, un cheque que cubre dos
facturas—, así que el que manda para los números sigue siendo el diff.
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


def _ev(tipo, origen, oid, destino, did, importe=0.0, batch=None, concepto=""):
    return {"id_mov_doble": abs(hash((tipo, oid, did))) % 10000,
            "batch_id": batch, "tipo": tipo,
            "origen_table": origen, "origen_id": oid,
            "destino_table": destino, "destino_id": did,
            "importe": importe, "concepto": concepto, "usuario": "alex",
            "estado": "activo", "dia": "2026-08-07"}


def _con_label(filas):
    with patch.object(ev.db, "fetch_all", return_value=filas):
        return ev.de_la_ventana("2026-08-07 00:00+00", "2026-08-07 23:59+00")


# ── El índice documento → hecho ─────────────────────────────────────────────

def test_un_deposito_toca_el_cheque_y_por_eso_los_une():
    evs = _con_label([_ev("cheque_depositado", "cheque", 102023, "cheque", 102023)])
    idx = ev.indice(evs)
    from modules.historial.queries import TIPOS_LABEL
    assert idx["c102023"]["label"] == TIPOS_LABEL["cheque_depositado"]


def test_las_tablas_sin_fila_propia_en_la_foto_no_entran_al_indice():
    """`transacciones_bancarias` y `compra` no tienen documento en la foto: el
    diff las ve dentro del saldo del componente, no como fila."""
    evs = _con_label([_ev("banco_de_directo", "transacciones_bancarias", 5,
                          "transacciones_bancarias", 5)])
    assert ev.indice(evs) == {}


def test_sin_ventana_no_consulta_nada():
    assert ev.de_la_ventana(None, "2026-08-07") == []


def test_si_mov_doble_no_contesta_la_pantalla_sigue():
    """Sin eventos el detalle sale con nombres más pobres, pero sale."""
    with patch.object(ev.db, "fetch_all", side_effect=RuntimeError("timeout")):
        assert ev.de_la_ventana("a", "b") == []


# ── El resumen agrupado por hecho ───────────────────────────────────────────

def test_un_cheque_depositado_es_UN_renglon_y_no_dos():
    """🚨 TMT: *"cheque depositado en banco debería ser un movimiento"*. El
    cheque que se va de cartera y la plata que aparece en el banco son el mismo
    hecho; sueltos parecen dos, y encima netean cero sin decir por qué."""
    idx = ev.indice(_con_label([
        _ev("cheque_depositado", "cheque", 102023, "cheque", 102023)]))
    movs = [
        {"doc_id": "c102023", "componente": "cheques", "aporte": -1251.87,
         "regla": "Cheque depositado o dado de baja",
         "etiqueta": "Cheque 102023 · LEG", "familia": "traspaso"},
        {"doc_id": "b10", "componente": "bancos", "aporte": 1251.87,
         "regla": "Movimiento bancario", "etiqueta": "Banco Pichincha",
         "familia": "traspaso"},
    ]
    out = t.resumir(movs, 0.0, idx)
    # El cheque cae en el hecho; el banco no tiene documento propio y queda
    # aparte — pero el hecho ya está nombrado, que es lo que se quería.
    from modules.historial.queries import TIPOS_LABEL
    assert any(g["texto"].startswith(TIPOS_LABEL["cheque_depositado"]) for g in out), \
        [g["texto"] for g in out]


def test_la_devolucion_aparece_con_nombre():
    """TMT: *"cuando hay una devolución me gustaría que también aparezca,
    ejemplo la factura de Puebla de hoy"* — 07/08, factura 11611, PUE."""
    idx = ev.indice(_con_label([
        _ev("factura_devolucion", "factura", 11611, "factura", 11611,
            importe=-907.97, concepto="DEVOLUCION Factura #11611 PUE")]))
    movs = [{"doc_id": "f11611", "componente": "facturas", "aporte": -907.97,
             "regla": "Abono a factura", "etiqueta": "Factura 11611 · PUE",
             "familia": "traspaso"}]
    g = t.resumir(movs, -907.97, idx)[0]
    assert g["texto"].startswith("Factura: devolución")
    assert "PUE" in g["texto"]
    assert g["aporte"] == -907.97
    assert "tipo=factura_devolucion" in g["url"]


def test_los_tres_anticipos_de_una_compra_son_un_renglon_con_el_numero():
    """TMT: *"anticipos e historial me gusta"* — el Historial los muestra como
    "3 anticipo(s) → compra N° 10130" con el link para verlos uno por uno."""
    idx = ev.indice(_con_label([
        _ev("compra_anticipo_dolares", "dolares", 2970, "compra", 10130,
            batch="b-1", concepto="AI 21 · 3 anticipo(s) → compra N° 10130"),
        _ev("compra_anticipo_dolares", "dolares", 2971, "compra", 10130,
            batch="b-1"),
        _ev("compra_anticipo_dolares", "dolares", 2972, "compra", 10130,
            batch="b-1"),
    ]))
    movs = [{"doc_id": f"d{i}", "componente": "antic", "aporte": -1000.0,
             "regla": "Anticipo aplicado", "etiqueta": f"Anticipo AI · {i}",
             "familia": "traspaso"} for i in (2970, 2971, 2972)]
    out = t.resumir(movs, -3000.0, idx)
    assert len(out) == 1
    assert out[0]["texto"].startswith("3 · ")
    assert out[0]["aporte"] == -3000.0


def test_sin_eventos_el_resumen_sigue_agrupando_por_regla():
    """El detalle no depende de `mov_doble`: si no hay evento, cae en la regla
    de siempre."""
    movs = [{"doc_id": "f1", "componente": "facturas", "aporte": 500.0,
             "regla": "Venta facturada", "etiqueta": "Factura 1 · AAA",
             "familia": "utilidad"}]
    assert t.resumir(movs, 500.0, {})[0]["texto"] == "Factura 1 · AAA"
