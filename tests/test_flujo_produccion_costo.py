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


def test_ingreso_hilado_importacion_es_referencia_costo():
    """La importación (24.494 kg, $58.629) queda como REFERENCIA del ingreso; el
    ingreso KG de la columna es el movimiento real de bodega 51."""
    movs = {51: {"ingreso": 24494.0, "egreso": 0.0}}
    with patch("modules.asinfo.service.hilado_recibido_mes", return_value=24494.0), \
         patch("modules.asinfo.service.fabricacion_flujo_mes", return_value={"issued": 0.0, "fab": 0.0}), \
         patch("modules.asinfo.service.movimiento_bodega_mes", side_effect=_mov_por_bodega(movs)), \
         patch("modules.asinfo.service.despacho_fisico_mes", return_value=0.0), \
         patch("modules.asinfo.service.ventas_facturado_kg", return_value=0.0), \
         patch("modules.importaciones.service.promedio_hilado_usd_kg", return_value=_OPEN), \
         patch("modules.importaciones.service.costo_hilado_recibido_mes",
               return_value={"us": 58629.40, "kg": 24494.0, "usd_kg": 2.394}):
        mov = _build_mov_asinfo(_data(), _inv(), _inv(), anio=2026, mes=7)
    hl = mov["hilado"]
    assert hl["ingresos_kg"] == 24494.0                          # movimiento de bodega
    assert hl["ref_import_kg"] == 24494.0                        # importación (referencia)
    assert hl["ref_import_us"] == 58629.40
    assert round(hl["ref_import_ukg"], 4) == round(58629.40 / 24494.0, 4)


def _mov_por_bodega(mapa):
    """side_effect para movimiento_bodega_mes(id_bodega, corte)."""
    def _f(id_bodega, corte):
        return mapa.get(int(id_bodega), {"ingreso": 0.0, "egreso": 0.0})
    return _f


def test_hilado_cierra_por_movimiento_de_bodega_y_baja_ukg():
    """Dueña 2026-07-10: ingreso/egreso del HILADO salen del MOVIMIENTO REAL de
    bodega 51 (no 'a tejer', que subcuenta) → la columna CIERRA por telescopía en
    kg y en $: inicial + ingreso + en máquinas − egreso = stock actual. En máquinas
    suma (stock nuestro). El ingreso barato (importación) BAJA el $/kg."""
    data = {"header": {"hilado": {"stock_inic_ukg": 3.0}, "tejido": {},
                       "terminado": {}, "colorantes": {}}}
    inic = {"disponible": True, "hilo": 1000.0, "tela_cruda": 0.0, "terminada": 0.0,
            "en_proceso_tc": 0.0, "en_proceso_pt": 0.0}
    # bodega 51: +100 −60 = +40 neto → 1000 → 1040 (hi1); WIP hilado 40.
    act = {"disponible": True, "hilo": 1040.0, "tela_cruda": 0.0, "terminada": 0.0,
           "en_proceso_tc": 40.0, "en_proceso_pt": 0.0}
    movs = {51: {"ingreso": 100.0, "egreso": 60.0}, 52: {"ingreso": 0.0, "egreso": 0.0},
            53: {"ingreso": 0.0, "egreso": 0.0}}
    with patch("modules.asinfo.service.hilado_recibido_mes", return_value=80.0), \
         patch("modules.asinfo.service.fabricacion_flujo_mes", return_value={"issued": 50.0, "fab": 40.0}), \
         patch("modules.asinfo.service.movimiento_bodega_mes", side_effect=_mov_por_bodega(movs)), \
         patch("modules.asinfo.service.despacho_fisico_mes", return_value=0.0), \
         patch("modules.asinfo.service.ventas_facturado_kg", return_value=0.0), \
         patch("modules.importaciones.service.promedio_hilado_usd_kg", return_value=_OPEN), \
         patch("modules.importaciones.service.costo_hilado_recibido_mes",
               return_value={"us": 160.0, "kg": 80.0, "usd_kg": 2.0}):
        mov = _build_mov_asinfo(data, inic, act, anio=2026, mes=7)
    hl = mov["hilado"]
    maq = mov["maquinas"]
    assert hl["ingresos_kg"] == 100.0        # movimiento real de bodega, no las 80 importadas
    assert hl["egresos_kg"] == 60.0          # movimiento real de bodega, no 'a tejer'
    assert hl["ref_import_kg"] == 80.0       # importaciones = referencia
    assert hl["ref_tejer_kg"] == 50.0        # a tejer = referencia
    assert round(hl["stock_inic_us"], 2) == 3000.0
    # promedio = (1000×3 + 160) / (1000 + 80) = 2.9259 → egreso a ese promedio, < 3.0
    assert round(hl["egresos_ukg"], 4) == round(3160.0 / 1080.0, 4)
    assert hl["egresos_ukg"] < 3.0
    assert round(maq["hilado_us"], 2) == round(40.0 * 3.0, 2)   # WIP al $/kg de apertura
    assert round(hl["stock_act_kg"], 2) == 1080.0               # 1040 bodega + 40 máquinas
    # CIERRE exacto en kg: inicial + ingreso + máquinas − egreso = stock act
    assert round(hl["stock_inic_kg"] + hl["ingresos_kg"] + maq["hilado"]
                 - hl["egresos_kg"], 4) == round(hl["stock_act_kg"], 4)
    # CIERRE exacto en $
    assert round(hl["stock_inic_us"] + hl["ingresos_us"] + maq["hilado_us"]
                 - hl["egresos_us"], 4) == round(hl["stock_act_us"], 4)


def test_crudo_cierra_por_movimiento_de_bodega():
    """El CRUDO también cierra por el movimiento real de bodega 52 (+ en máquinas)."""
    data = {"header": {"hilado": {}, "tejido": {}, "terminado": {}, "colorantes": {}}}
    inic = {"disponible": True, "hilo": 0.0, "tela_cruda": 500.0, "terminada": 0.0,
            "en_proceso_tc": 0.0, "en_proceso_pt": 0.0}
    # bodega 52: +80 −30 = +50 → 500 → 550 (tc1); WIP crudo (en_proceso_pt) 20.
    act = {"disponible": True, "hilo": 0.0, "tela_cruda": 550.0, "terminada": 0.0,
           "en_proceso_tc": 0.0, "en_proceso_pt": 20.0}
    movs = {51: {"ingreso": 0.0, "egreso": 0.0}, 52: {"ingreso": 80.0, "egreso": 30.0},
            53: {"ingreso": 0.0, "egreso": 0.0}}
    with patch("modules.asinfo.service.hilado_recibido_mes", return_value=0.0), \
         patch("modules.asinfo.service.fabricacion_flujo_mes", return_value={"issued": 40.0, "fab": 35.0}), \
         patch("modules.asinfo.service.movimiento_bodega_mes", side_effect=_mov_por_bodega(movs)), \
         patch("modules.asinfo.service.despacho_fisico_mes", return_value=0.0), \
         patch("modules.asinfo.service.ventas_facturado_kg", return_value=0.0), \
         patch("modules.importaciones.service.promedio_hilado_usd_kg", return_value=_OPEN), \
         patch("modules.importaciones.service.costo_hilado_recibido_mes",
               return_value={"us": 0.0, "kg": 0.0, "usd_kg": None}):
        mov = _build_mov_asinfo(data, inic, act, anio=2026, mes=7)
    tj = mov["tejido"]
    maq = mov["maquinas"]
    assert tj["ingresos_kg"] == 80.0
    assert tj["egresos_kg"] == 30.0
    assert tj["ref_prod_kg"] == 35.0
    # CIERRE en kg: inicial + ingreso + en máquinas − egreso = stock act
    assert round(500.0 + 80.0 + maq["crudo"] - 30.0, 4) == round(tj["stock_act_kg"], 4)


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
