"""El Flujo producción (tabla 'INICIAL ASINFO') muestra el COSTO en dólares del
hilado ingresado, tomado de nuestra base (anticipos + compras de la importación,
atribuidos por año) — antes la columna $ del ingreso estaba en '—'.
Dueña 2026-07-10: "necesitamos ponerle lo que pagamos por esos kg". Sin DB real.
"""
from __future__ import annotations

from unittest.mock import patch

from modules.informes.views import _build_mov_asinfo


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
         patch("modules.importaciones.service.costo_hilado_recibido_mes",
               return_value={"us": 58629.40, "kg": 24494.0, "usd_kg": 2.394}):
        mov = _build_mov_asinfo(_data(), _inv(), _inv(), anio=2026, mes=7)
    hl = mov["hilado"]
    assert hl["ingresos_kg"] == 24494.0
    assert hl["ingresos_us"] == 58629.40                          # ya no es "—"
    assert round(hl["ingresos_ukg"], 4) == round(58629.40 / 24494.0, 4)


def test_ingreso_hilado_sin_costo_queda_en_cero():
    """Fail-soft: si no hay costo (Asinfo/DB caída), ingresos_us=0 → la vista
    muestra '—' como antes, sin romper."""
    with patch("modules.asinfo.service.hilado_recibido_mes", return_value=0.0), \
         patch("modules.asinfo.service.fabricacion_flujo_mes", return_value={"issued": 0.0, "fab": 0.0}), \
         patch("modules.asinfo.service.movimiento_bodega_mes", return_value={"ingreso": 0.0, "egreso": 0.0}), \
         patch("modules.asinfo.service.despacho_fisico_mes", return_value=0.0), \
         patch("modules.asinfo.service.ventas_facturado_kg", return_value=0.0), \
         patch("modules.importaciones.service.costo_hilado_recibido_mes",
               return_value={"us": 0.0, "kg": 0.0, "usd_kg": None}):
        mov = _build_mov_asinfo(_data(), _inv(), _inv(), anio=2026, mes=7)
    hl = mov["hilado"]
    assert hl["ingresos_us"] == 0.0
    assert hl["ingresos_ukg"] == 0.0
