"""Los despachos salen del renglón del stock con nombre propio (25/09/2026).

Caso real: 21/09 10:12, "salió de term. −184" eran 820 kg en cuatro guías
(LMM 609,05 · EEU 140 · ELP 48,6 · EEU 21,85) y 636 kg que entraban.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timezone
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.informes import despachos_ventana as dv  # noqa: E402
from modules.informes import traza as t  # noqa: E402

GUIAS = [{"guia": "DES-000098801", "hora": "10:09", "cliente": "LMM", "kg": 609.05, "dia": "2026-09-21"},
         {"guia": "DES-000098802", "hora": "10:10", "cliente": "EEU", "kg": 140.0, "dia": "2026-09-21"},
         {"guia": "DES-000098803", "hora": "10:10", "cliente": "ELP", "kg": 48.6, "dia": "2026-09-21"},
         {"guia": "DES-000098804", "hora": "10:11", "cliente": "EEU", "kg": 21.85, "dia": "2026-09-21"}]
UKG = 5.0


def _movs(aporte_stock):
    return [{"doc_id": "#stock", "componente": "vsto", "aporte": aporte_stock,
             "regla": "Stock", "etiqueta": "salió de term.", "familia": "utilidad"}]


def test_el_despacho_es_su_propio_renglon_y_la_suma_no_cambia():
    kilos = {"terminado_kg": -184.0}
    out = t.resumir(_movs(-920.0), -920.0, {}, venta={"kg": 0, "us": 0, "ukg": UKG},
                    despachos=GUIAS, kilos=kilos, d_ukg=0.0001)
    textos = [g["texto"] for g in out]
    assert textos[0] == "4 DES · LMM, EEU ×2, ELP", textos
    des = out[0]
    assert des["kg"] == {"terminado_kg": -819.5}
    assert des["aporte"] == round(-819.5 * UKG, 2)
    assert des["url"] == "/facturas/dia?fecha=2026-09-21"
    resto = out[1]
    assert resto["texto"] == "entró a term."
    assert resto["kg"] == {"terminado_kg": 635.5}
    assert resto["d_ukg"] == 0.0001            # el $/kg sigue en un renglón
    # El invariante: la suma de lo que se ve es el Δ.
    assert round(sum(g["aporte"] for g in out), 2) == -920.0


def test_una_sola_guia_dice_su_numero():
    out = t.resumir(_movs(-100.0), -100.0, {}, venta={"kg": 0, "us": 0, "ukg": UKG},
                    despachos=GUIAS[3:], kilos={"terminado_kg": -21.85})
    assert out[0]["texto"] == "DES #98804 EEU"
    # El resto no movió kilos ni se ve: se funde si es chico.
    assert round(sum(g["aporte"] for g in out), 2) == -100.0


def test_sin_precio_o_sin_stock_no_toca_nada():
    out = t.resumir(_movs(-920.0), -920.0, {}, venta=None, despachos=GUIAS,
                    kilos={"terminado_kg": -184.0})
    assert [g["texto"] for g in out] == ["salió de term."]
    otros = [{"doc_id": "f1", "componente": "facturas", "aporte": 10.0,
              "regla": "Venta facturada", "etiqueta": "Factura 1 · AAA",
              "familia": "utilidad"}]
    out = t.resumir(otros, 10.0, {}, venta={"kg": 0, "us": 0, "ukg": UKG},
                    despachos=GUIAS, kilos={})
    assert [g["texto"] for g in out] == ["FA #1 AAA"]


def test_la_ventana_toma_las_guias_por_minuto_en_hora_de_ecuador():
    dv._CACHE.clear()
    guias = [{"guia": "A", "hora": "10:06", "kg": 1.0}, {"guia": "B", "hora": "10:07", "kg": 1.0},
             {"guia": "C", "hora": "10:11", "kg": 1.0}, {"guia": "D", "hora": "10:12", "kg": 1.0}]
    with patch.object(dv, "_hoy", return_value="2026-09-25"), \
            patch("modules.facturas.dia_despacho._guias", return_value=guias):
        desde = datetime(2026, 9, 21, 15, 6, 40, tzinfo=UTC)   # 10:06 EC
        hasta = datetime(2026, 9, 21, 15, 12, 30, tzinfo=UTC)  # 10:12 EC
        assert [g["guia"] for g in dv.de_la_ventana(desde, hasta)] == ["A", "B", "C"]


def test_si_asinfo_no_contesta_la_traza_sigue():
    dv._CACHE.clear()
    with patch.object(dv, "_hoy", return_value="2026-09-25"), \
            patch("modules.facturas.dia_despacho._guias", side_effect=RuntimeError("x")):
        assert dv.de_la_ventana("2026-09-21T15:00:00+00:00",
                                "2026-09-21T15:05:00+00:00") == []
