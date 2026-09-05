"""El recargo de una importación recibida el mes anterior entra al $/kg solo.

Tamara 2026-09-05. Andrés cargó (anticipo USD → compra) los recargos de tres
importaciones ya recibidas: AC 36 (recibida 01/09), AC 39 y MD 1 (recibidas
31/08). `costo_hilado_recibido_mes` atribuye el costo al MES DE RECEPCIÓN, así
que el de AC 36 entró al $/kg de septiembre a las 10:45 y los de AC 39 y MD 1
(6.849,51) no entraron a ningún lado: agosto ya cerró. Salieron del banco y el
costo del hilo nunca los veía. *"Debería ser automático, no tener que
apretar"* (el botón "Entrar al precio del hilo"). *"No toques nada de julio y
agosto, solo septiembre."*
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import db  # noqa: E402
from modules.asinfo import service as sv  # noqa: E402
from modules.compras import queries as cq  # noqa: E402
from modules.importaciones import service as isv  # noqa: E402


def _imp(prov, num, recibida, recepcion, items):
    return {"prov": prov, "numero": num, "recibida": recibida,
            "fecha_recepcion": recepcion, "fecha": "2026-08-20",
            "compra": {"ids": [i["id_compra"] for i in items], "items": items}}


# El cruce con Asinfo tal como estaba el 05/09 a las 10:45.
CRUCE = [
    _imp("AC", 36, True, "2026-09-01", [
        {"id_compra": 291, "fecha": "2026-09-01", "importe": 60_573.72},
        {"id_compra": 309, "fecha": "2026-09-05", "importe": 2_606.88},
    ]),
    _imp("AC", 39, True, "2026-08-31", [
        {"id_compra": 287, "fecha": "2026-08-31", "importe": 82_369.06},
        {"id_compra": 310, "fecha": "2026-09-05", "importe": 2_659.16},
    ]),
    _imp("MD", 1, True, "2026-08-31", [
        {"id_compra": 286, "fecha": "2026-08-31", "importe": 47_018.66},
        {"id_compra": 289, "fecha": "2026-08-31", "importe": 10_146.55},
        {"id_compra": 307, "fecha": "2026-09-05", "importe": 1_340.35},
        {"id_compra": 308, "fecha": "2026-09-05", "importe": 2_850.00},
    ]),
    # Recargo de julio de una importación de junio: fuera del corte.
    _imp("AC", 16, True, "2026-06-28", [
        {"id_compra": 100, "fecha": "2026-07-03", "importe": 2_813.87},
    ]),
    # Todavía no recibida: su plata entra cuando se reciba, no acá.
    _imp("AI", 30, False, None, [
        {"id_compra": 400, "fecha": "2026-09-04", "importe": 71_145.01},
    ]),
]


def _vivas_todas(sql, params, **kw):
    return [{"id_compra": i} for i in params[0]]


def test_septiembre_suma_los_tres_recargos_de_agosto():
    with patch.object(isv, "importaciones_con_cruce", return_value=CRUCE), \
         patch.object(db, "fetch_all", side_effect=_vivas_todas):
        out = isv.recargos_tardios_mes(2026, 9)
    assert out["ids"] == [307, 308, 310]
    assert out["n"] == 3
    assert out["us"] == pytest.approx(6_849.51)


def test_el_recargo_del_mes_de_recepcion_no_es_tardio():
    """AC 36 se recibió el 01/09: su recargo del 05/09 ya lo cuenta
    `costo_hilado_recibido_mes`. Contarlo acá sería sumarlo dos veces."""
    with patch.object(isv, "importaciones_con_cruce", return_value=CRUCE), \
         patch.object(db, "fetch_all", side_effect=_vivas_todas):
        out = isv.recargos_tardios_mes(2026, 9)
    assert 309 not in out["ids"] and 291 not in out["ids"]


def test_julio_y_agosto_no_se_tocan():
    with patch.object(isv, "importaciones_con_cruce", return_value=CRUCE), \
         patch.object(db, "fetch_all", side_effect=_vivas_todas):
        assert isv.recargos_tardios_mes(2026, 7)["us"] == 0.0
        assert isv.recargos_tardios_mes(2026, 8)["ids"] == []


def test_la_marcada_con_el_boton_y_la_anulada_no_cuentan():
    """La base decide: el cruce no trae `stat` ni `al_precio_hilo`."""
    def _solo_vivas(sql, params, **kw):
        return [{"id_compra": i} for i in params[0] if i not in (308, 310)]
    with patch.object(isv, "importaciones_con_cruce", return_value=CRUCE), \
         patch.object(db, "fetch_all", side_effect=_solo_vivas):
        out = isv.recargos_tardios_mes(2026, 9)
    assert out["ids"] == [307]
    assert out["us"] == pytest.approx(1_340.35)


def test_sin_asinfo_o_sin_base_no_inventa_nada():
    with patch.object(isv, "importaciones_con_cruce", side_effect=RuntimeError("metabase")):
        assert isv.recargos_tardios_mes(2026, 9)["us"] == 0.0
    with patch.object(isv, "importaciones_con_cruce", return_value=CRUCE), \
         patch.object(db, "fetch_all", side_effect=RuntimeError("sin base")):
        assert isv.recargos_tardios_mes(2026, 9)["us"] == 0.0
    with patch.object(isv, "importaciones_con_cruce", return_value=[]):
        assert isv.recargos_tardios_mes(2026, 9)["n"] == 0


# ── y entra al $/kg del mes como plata sin kilos ───────────────────────────
HI0, OPEN = 1_960_000.0, 3.0437
INV_INIC = {"hilo": HI0}
INV_ACT = {"hilo": 1_900_000.0, "en_proceso_tc": 62_000.0}


def _stock(recargos):
    rec = {"us": 60_573.72, "kg": 19_812.48, "kg_con_costo": 19_812.48, "usd_kg": None}
    with patch.object(sv, "inventario_por_etapa_a_fecha", return_value=INV_INIC), \
         patch.object(sv, "inventario_por_etapa", return_value=INV_ACT), \
         patch.object(sv, "hilado_recibido_mes", return_value=19_812.48), \
         patch.object(isv, "costo_hilado_recibido_mes", return_value=rec), \
         patch("modules.compras_locales.service.hilado_local_recibido_mes",
               return_value={"kg": 0.0, "us": 0.0}), \
         patch.object(cq, "hilo_al_precio_mes", return_value={"us": 0.0, "kg": 0.0, "n": 0}), \
         patch.object(isv, "recargos_tardios_mes", **recargos):
        return sv.mov_hilado_valuacion(2026, 9, OPEN)


def test_los_recargos_tardios_suben_el_stock_casi_lo_que_valen():
    sin = _stock({"return_value": {"us": 0.0, "n": 0, "ids": []}})
    con = _stock({"return_value": {"us": 6_849.51, "n": 3, "ids": [307, 308, 310]}})
    salto = con["stock_act_us"] - sin["stock_act_us"]
    assert salto == pytest.approx(6_849.51 * INV_ACT["hilo"] / (HI0 + 19_812.48), rel=1e-6)
    assert con["compras_us"] == pytest.approx(60_573.72 + 6_849.51)
    assert con["recargos_tardios_us"] == pytest.approx(6_849.51)
    assert con["asimetrico"] is False          # plata sin kilos a propósito
    assert con["tarifa_congelada"] is False


def test_si_el_cruce_falla_la_valuacion_sigue():
    out = _stock({"side_effect": RuntimeError("metabase caído")})
    assert out["disponible"] is True
    assert out["compras_us"] == pytest.approx(60_573.72)
    assert out["recargos_tardios_us"] == 0.0
