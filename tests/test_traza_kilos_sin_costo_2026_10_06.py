"""Los kilos que llegan sin su plata cruzada salen con nombre propio.

Tamara 06/10/2026, MH 71-72 (fotos 15887 → 15888 → 15889):

    08:25  llegó la primera partida, 24.300 kg, sin los anticipos cruzados.
           Se valúan al promedio (3,1822) y la utilidad sube +77.327 "de
           prestado". La traza decía "tej. → term. · entró a hil.".
    08:31  se cruzan los anticipos, entra el lote (71.880 kg, 153.499,23) y
           los 24.300 kg prestados se devuelven. Decía "hil. y tej. → term.".

Ninguna de las dos cosas pasó. Eligió dejar el préstamo y nombrarlo
("A"): los renglones suman lo mismo, sólo deja de esconderse.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.informes import traza as t  # noqa: E402

INS_0820 = {"sin_costo": []}
INS_0825 = {"sin_costo": [{"codigo": "MH 71-72", "kg": 24300.0}]}
F_0820 = {"id_traza": 15887, "compras_kg": 0, "hilado_ukg": 3.1822,
          "hilado_kg": 2123797.62, "tejido_kg": 291322.0, "terminado_kg": 322797.0,
          "kg_sin_costo": 0.0, "hilado_insumos": INS_0820}
F_0825 = {"id_traza": 15888, "compras_kg": 0, "hilado_ukg": 3.1822,
          "hilado_kg": 2148097.62, "tejido_kg": 291243.0, "terminado_kg": 322876.0,
          "kg_sin_costo": 24300.0, "hilado_insumos": INS_0825}
F_0831 = {"id_traza": 15889, "compras_kg": 71880.0, "hilado_ukg": 3.1488,
          "hilado_kg": 2195677.62, "tejido_kg": 291188.0, "terminado_kg": 322931.0,
          "kg_sin_costo": 0.0, "hilado_insumos": INS_0820}

_EV = {d: {"tipo": "bap_anticipo_a_compra", "grupo": "g71",
           "label": "Anticipo → Compra", "id_mov_doble": 7171,
           "dia": "2026-10-06", "concepto": "MH 71 · 3 anticipo(s) → compra #10500",
           "meta": {"codigo_prov": "MH", "n_anticipos": 3}}
       for d in ("d3358", "d3362", "d3492")}


def _stock_row(aporte, etq):
    return {"componente": "vsto", "doc_id": "#stock", "tipo": "cambio",
            "etiqueta": etq, "regla": "Stock", "aporte": aporte,
            "familia": "utilidad"}


def _resumen(movs, d_ut, fila, ant, ev=None):
    stock = t.stock_de_la_ventana(fila, ant)
    with patch.object(t.db, "fetch_all", return_value=[]):
        return t.resumir(movs, d_ut, ev or {}, hasta="2026-10-06T13:31:00",
                         stock=stock)


def test_0825_llegaron_sin_costo_con_nombre():
    # 24.300 kg × 3,1822 = 77.327,46; tej. → term. 79 kg = el resto.
    movs = [_stock_row(77462.0, "tej. → term. · entró a hil.")]
    out = _resumen(movs, 77462.0, F_0825, F_0820)
    textos = [g["texto"] for g in out]
    assert textos[0] == "llegaron 24.300 kg de MH 71-72 sin costo todavía", textos
    assert out[0]["aporte"] == pytest.approx(24300 * 3.1822)
    assert out[0]["kg"] == {"hilado_kg": 24300.0}
    assert "3,1822" in out[0]["nota"]
    resto = out[1]
    assert resto["texto"] == "tej. → term."
    assert "hilado_kg" not in resto["kg"]
    assert "d_ukg" in resto          # el template lo lee sin default
    assert sum(g["aporte"] for g in out) == pytest.approx(77462.0, abs=0.02)


def test_0831_se_devuelve_debajo_del_lote():
    movs = [
        *[{"componente": "antic", "doc_id": d, "tipo": "baja",
           "etiqueta": "Anticipo MH · 71/26", "regla": "Anticipo aplicado",
           "aporte": a, "familia": "traspaso"}
          for d, a in (("d3358", -121569.17), ("d3362", -556.25),
                       ("d3492", -31373.81))],
        # Lo que la foto grabó en Stock: el lote al $/kg viejo, menos los
        # 24.300 kg que "salieron", más tej. → term.
        _stock_row(round(47580 * 3.1822 + 175.0, 2), "tej. → term. · entró a hil."),
        {"componente": "vsto", "doc_id": "#stock:tarifa", "tipo": "cambio",
         "etiqueta": "cambió el $/kg de 3 etapas", "regla": "Revaluación de stock",
         "aporte": -93903.0, "familia": "utilidad"},
    ]
    d_ut = round(sum(m["aporte"] for m in movs), 2)
    out = _resumen(movs, d_ut, F_0831, F_0825, _EV)
    textos = [g["texto"] for g in out]
    assert textos[:3] == ["MH 71 · entró la mercadería de 3 anticipos",
                          "revaluó el stock",
                          "se le puso costo a los 24.300 kg que llegaron antes"], textos
    dev = out[2]
    assert dev["aporte"] == pytest.approx(-24300 * 3.1822)
    assert dev["kg"] == {"hilado_kg": -24300.0}
    # Lo que queda del stock ya no dice que salió hilado.
    assert not any("hil." in (g["texto"] or "") and g is not out[0]
                   and g is not dev for g in out[3:])
    assert sum(g["aporte"] for g in out) == pytest.approx(d_ut, abs=0.02)


def test_sin_kg_sin_costo_no_cambia_nada():
    f0 = {k: v for k, v in F_0820.items() if k != "kg_sin_costo"}
    f1 = {k: v for k, v in F_0825.items() if k != "kg_sin_costo"}
    assert t.stock_de_la_ventana(f1, f0) == {}


def test_sin_nombres_dice_hilado():
    f1 = dict(F_0825, hilado_insumos=None)
    out = _resumen([_stock_row(77462.0, "x")], 77462.0, f1, F_0820)
    assert out[0]["texto"] == "llegaron 24.300 kg de hil. sin costo todavía"


def test_ventana_que_cruza_de_mes_no_se_mira():
    from datetime import datetime
    f0 = dict(F_0820, creado_en=datetime(2026, 9, 30, 23, 58))
    f1 = dict(F_0825, creado_en=datetime(2026, 10, 1, 0, 3))
    assert t._sin_costo_de_la_ventana(f1, f0) == {}


def test_el_costo_del_mes_dice_cuales_esperan_su_plata():
    from modules.importaciones import service as s
    rows = [
        {"recibida": True, "fecha_recepcion": "2026-10-06", "prov": "MH",
         "numero": 71, "fecha": "2026-08-27", "kg": 24300.0,
         "importe_programa": None, "codigo": "MH 71-72"},
        {"recibida": True, "fecha_recepcion": "2026-10-03", "prov": "AC",
         "numero": 80, "fecha": "2026-08-01", "kg": 24000.0,
         "importe_programa": 80000.0, "codigo": "AC 80"},
    ]
    with patch.object(s, "importaciones_con_cruce", return_value=rows):
        r = s.costo_hilado_recibido_mes(2026, 10)
    assert r["sin_costo"] == [{"codigo": "MH 71-72", "kg": 24300.0}]
    assert r["kg"] - r["kg_con_costo"] == 24300.0
