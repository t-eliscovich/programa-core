"""El Flujo producción (tabla 'INICIAL ASINFO') muestra el COSTO en dólares del
hilado ingresado, tomado de nuestra base (anticipos + compras de la importación,
atribuidos por año) — antes la columna $ del ingreso estaba en '—'.
Dueña 2026-07-10: "necesitamos ponerle lo que pagamos por esos kg". Sin DB real.
"""
from __future__ import annotations

from unittest.mock import patch

from modules.informes.views import _build_mov_asinfo

# $/kg de apertura (promedio de compras de PC) mockeado en los tests.
_OPEN = 3.0


def _data():
    return {"header": {"hilado": {}, "tejido": {}, "terminado": {}, "colorantes": {}}}


def _inv(disp=True):
    return {
        "disponible": disp, "hilo": 100000.0, "tela_cruda": 50000.0,
        "terminada": 333315.0, "en_proceso_tc": 0.0, "en_proceso_pt": 0.0,
    }


def test_ingreso_hilado_muestra_costo_de_nuestra_base():
    with patch("modules.asinfo.service.hilado_recibido_mes", return_value=24494.0), \
         patch("modules.asinfo.service.fabricacion_flujo_mes", return_value={"issued": 0.0, "fab": 0.0}), \
         patch("modules.asinfo.service.movimiento_bodega_mes", return_value={"ingreso": 0.0, "egreso": 0.0}), \
         patch("modules.asinfo.service.despacho_fisico_mes", return_value=0.0), \
         patch("modules.asinfo.service.ventas_facturado_kg", return_value=0.0), \
         patch("modules.importaciones.service.promedio_hilado_usd_kg", return_value=_OPEN), \
         patch("modules.importaciones.service.costo_hilado_recibido_mes",
               return_value={"us": 58629.40, "kg": 24494.0, "usd_kg": 2.394}):
        mov = _build_mov_asinfo(_data(), _inv(), _inv(), anio=2026, mes=7)
    hl = mov["hilado"]
    assert hl["ingresos_kg"] == 24494.0
    assert hl["ingresos_us"] == 58629.40                          # ya no es "—"
    assert round(hl["ingresos_ukg"], 4) == round(58629.40 / 24494.0, 4)


def test_hilado_promedio_ponderado_cierra_y_baja():
    """Costeo por promedio ponderado (todo PC): egreso al promedio, EN MÁQUINAS
    (WIP) SUMA al stock actual (dueña 2026-07-10: "es stock nuestro"), valuado al
    $/kg de apertura, y la columna CIERRA exacto por el ajuste (varianza de bodega):
        inicial + ingresos + en máquinas − egresos − ajuste = stock actual
    (en kg y en $), y el ingreso barato BAJA el $/kg."""
    data = {"header": {"hilado": {"stock_inic_ukg": 3.0}, "tejido": {},
                       "terminado": {}, "colorantes": {}}}
    inic = {"disponible": True, "hilo": 1000.0, "tela_cruda": 0.0, "terminada": 0.0,
            "en_proceso_tc": 0.0, "en_proceso_pt": 0.0}
    # bodega actual 1040 (≠ 1000+100−50=1050 derivado → hay varianza de bodega=10),
    # WIP hilado 40 → el ajuste la absorbe y la columna cierra igual.
    act = {"disponible": True, "hilo": 1040.0, "tela_cruda": 0.0, "terminada": 0.0,
           "en_proceso_tc": 40.0, "en_proceso_pt": 0.0}
    with patch("modules.asinfo.service.hilado_recibido_mes", return_value=100.0), \
         patch("modules.asinfo.service.fabricacion_flujo_mes", return_value={"issued": 50.0, "fab": 40.0}), \
         patch("modules.asinfo.service.movimiento_bodega_mes", return_value={"ingreso": 0.0, "egreso": 0.0}), \
         patch("modules.asinfo.service.despacho_fisico_mes", return_value=0.0), \
         patch("modules.asinfo.service.ventas_facturado_kg", return_value=0.0), \
         patch("modules.importaciones.service.promedio_hilado_usd_kg", return_value=_OPEN), \
         patch("modules.importaciones.service.costo_hilado_recibido_mes",
               return_value={"us": 200.0, "kg": 100.0, "usd_kg": 2.0}):
        mov = _build_mov_asinfo(data, inic, act, anio=2026, mes=7)
    hl = mov["hilado"]
    maq = mov["maquinas"]
    assert round(hl["stock_inic_us"], 2) == 3000.0                    # 1000 × 3.0 (apertura)
    # promedio = (3000 + 200) / (1000 + 100) = 2.9091 → egreso a ese promedio, < 3.0
    assert round(hl["egresos_ukg"], 4) == round(3200.0 / 1100.0, 4)
    assert hl["egresos_ukg"] < 3.0
    assert round(maq["hilado_us"], 2) == round(40.0 * 3.0, 2)         # WIP al $/kg de apertura
    assert round(hl["stock_act_kg"], 2) == 1080.0                     # bodega 1040 + WIP 40
    # CIERRE exacto en $: inic + ing + máquinas − egr − ajuste = stock act
    lhs = (hl["stock_inic_us"] + hl["ingresos_us"] + maq["hilado_us"]
           - hl["egresos_us"] - hl["ajuste_us"])
    assert round(lhs, 4) == round(hl["stock_act_us"], 4)
    # CIERRE exacto en kg
    lhs_kg = (hl["stock_inic_kg"] + hl["ingresos_kg"] + maq["hilado"]
              - hl["egresos_kg"] - hl["ajuste_kg"])
    assert round(lhs_kg, 4) == round(hl["stock_act_kg"], 4)


def test_ingreso_hilado_sin_costo_queda_en_cero():
    """Fail-soft: si no hay costo (Asinfo/DB caída), ingresos_us=0 → la vista
    muestra '—' como antes, sin romper."""
    with patch("modules.asinfo.service.hilado_recibido_mes", return_value=0.0), \
         patch("modules.asinfo.service.fabricacion_flujo_mes", return_value={"issued": 0.0, "fab": 0.0}), \
         patch("modules.asinfo.service.movimiento_bodega_mes", return_value={"ingreso": 0.0, "egreso": 0.0}), \
         patch("modules.asinfo.service.despacho_fisico_mes", return_value=0.0), \
         patch("modules.asinfo.service.ventas_facturado_kg", return_value=0.0), \
         patch("modules.importaciones.service.promedio_hilado_usd_kg", return_value=_OPEN), \
         patch("modules.importaciones.service.costo_hilado_recibido_mes",
               return_value={"us": 0.0, "kg": 0.0, "usd_kg": None}):
        mov = _build_mov_asinfo(_data(), _inv(), _inv(), anio=2026, mes=7)
    hl = mov["hilado"]
    assert hl["ingresos_us"] == 0.0
    assert hl["ingresos_ukg"] == 0.0
