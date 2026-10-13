"""El cuadro MOVIMIENTOS DEL MES (INICIAL ASINFO) muestra los RECARGOS que el
$/kg del hilado suma, y la columna $ cierra (Federico 13/10/2026: "entra hilo
más barato a 3.159 y el promedio sube a 3.185 — o hace mal las cuentas o
faltan kg"). Números de octubre 2026. Sin DB real."""
from __future__ import annotations

from unittest.mock import patch

from modules.informes.views import _build_mov_asinfo

_OPEN = 3.182226859916865
_HI0_VAL = 2136700.738      # saldo al 1° (lo que usa el promedio)
_HI0_CUADRO = 2151188.0     # saldo al corte de las pantallas de producción
_HI1 = 2306316.358
_MAQ = 45813.77
_KG = 300755.44
_US = 950207.21
_REC = 13865.44


def _valuacion(yy, mm, open_ukg):
    compras_us = _US + _REC
    avg = (_HI0_VAL * open_ukg + compras_us) / (_HI0_VAL + _KG)
    act_us = _HI1 * avg + _MAQ * open_ukg
    return {
        "disponible": True, "avg_ukg": avg,
        "stock_act_kg": _HI1 + _MAQ, "stock_act_us": act_us,
        "stock_act_ukg": act_us / (_HI1 + _MAQ),
        "recargos_tardios_us": _REC,
        "insumos": {"al_precio_us": 0.0, "al_precio_n": 0,
                    "recargos_tardios": [{"codigo": c} for c in
                                         ("AI 33", "AI 29", "AC 66", "MH 62", "AI 53")]},
    }


def _mov(id_bodega, corte):
    return {"ingreso": 0.0, "egreso": 0.0}


def _build():
    data = {"header": {"hilado": {}, "tejido": {}, "terminado": {}, "colorantes": {}}}
    inic = {"disponible": True, "hilo": _HI0_CUADRO, "tela_cruda": 0.0,
            "terminada": 0.0, "en_proceso_tc": 0.0, "en_proceso_pt": 0.0}
    act = {"disponible": True, "hilo": _HI1, "tela_cruda": 0.0, "terminada": 0.0,
           "en_proceso_tc": _MAQ, "en_proceso_pt": 0.0}
    with patch("modules.informes.queries.apertura_ukg_hilado", return_value=_OPEN), \
         patch("modules.asinfo.service.mov_hilado_valuacion", side_effect=_valuacion), \
         patch("modules.asinfo.service.hilado_recibido_mes", return_value=_KG), \
         patch("modules.asinfo.service.fabricacion_flujo_mes", return_value={"issued": 0.0, "fab": 0.0}), \
         patch("modules.asinfo.service.movimiento_bodega_mes", side_effect=_mov), \
         patch("modules.asinfo.service.despacho_fisico_mes", return_value=0.0), \
         patch("modules.asinfo.service.ventas_facturado_kg", return_value=0.0), \
         patch("modules.compras_locales.service.hilado_local_recibido_mes", return_value={}), \
         patch("modules.importaciones.service.costo_hilado_recibido_mes",
               return_value={"us": _US, "kg": _KG, "kg_con_costo": _KG}):
        return _build_mov_asinfo(data, inic, act, anio=2026, mes=10)


def test_los_recargos_tienen_renglon_y_nombran_las_importaciones():
    hl = _build()["hilado"]
    assert round(hl["recargos_us"], 2) == _REC
    assert hl["recargos_detalle"] == ["AI 33", "AI 29", "AC 66", "MH 62", "AI 53"]
    # Ingresos sigue siendo SÓLO las recepciones (3,159 US$/kg).
    assert round(hl["ingresos_us"] / hl["ingresos_kg"], 3) == 3.159


def test_la_columna_pesos_cierra_con_los_recargos():
    mov = _build()
    hl, maq = mov["hilado"], mov["maquinas"]
    suma = (hl["stock_inic_us"] + hl["ingresos_us"] + hl["recargos_us"]
            + maq["hilado_us"] - hl["egresos_us"])
    assert abs(suma - hl["stock_act_us"]) < 0.01
    # El egreso sigue al promedio (3,185), no se deforma.
    assert round(hl["egresos_ukg"], 3) == 3.185
