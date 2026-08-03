"""La fila TOTAL del pie de /posdat tiene que sumar de verdad.

Dueña 2026-08-01, con una captura del pie de la grilla: *"acá el total no
funciona"*. Daba **0,00** en las dos pestañas, por dos bugs sumados:

1. `modules/posdat/views.py` calculaba los totales sólo `if tab == "yy"`, así
   que en la pestaña POSDATADOS quedaban en 0.
2. `posdat/lista.html` no usaba esos totales: los re-sumaba con
   `{% set x = x + ... %}` DENTRO de un `{% for %}`. En Jinja el `set` adentro
   del loop es local a la vuelta — la variable de afuera nunca se modifica, y
   quedaba en el 0 inicial. Por eso daba 0,00 también en YY.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.posdat.views import _totales_de  # noqa: E402


def test_suma_las_tres_columnas():
    filas = [
        {"importe": 100.0, "cuota_mensual": 10.0, "cuota_diaria": 1.0},
        {"importe": 250.5, "cuota_mensual": 20.5, "cuota_diaria": 2.5},
    ]
    t = _totales_de(filas)

    assert t["importe"] == 350.5
    assert t["cuota_mensual"] == 30.5
    assert t["cuota_diaria"] == 3.5


def test_tolera_null_y_columnas_ausentes():
    """Las filas de POSDATADOS no traen cuota_diaria, y el importe puede venir
    NULL — no puede reventar ni contaminar la suma."""
    filas = [
        {"importe": 100.0},
        {"importe": None, "cuota_mensual": None},
        {"cuota_mensual": 5.0},
    ]
    t = _totales_de(filas)

    assert t["importe"] == 100.0
    assert t["cuota_mensual"] == 5.0
    assert t["cuota_diaria"] == 0.0


def test_sin_filas_da_cero_y_no_rompe():
    assert _totales_de([])["importe"] == 0.0
    assert _totales_de(None)["importe"] == 0.0


def test_los_negativos_restan():
    """Los ajustes y las líneas OP entran con importe negativo — el total es
    el NETO, igual que la columna."""
    filas = [{"importe": 1000.0}, {"importe": -250.0}]

    assert _totales_de(filas)["importe"] == 750.0


def test_el_template_usa_los_totales_de_la_vista_no_un_set_en_el_loop():
    """Clava el bug de Jinja: si alguien vuelve a poner un `{% set %}` que se
    auto-acumula dentro de un `{% for %}`, el total vuelve a 0 en silencio."""
    tpl = (ROOT / "modules/posdat/templates/posdat/lista.html").read_text()

    assert "{% set _suma_importe = total_importe or 0 %}" in tpl
    assert "{% set _suma_cuota = total_cuota_mensual or 0 %}" in tpl
    # el patrón roto no puede volver
    assert "_suma_importe = _suma_importe +" not in tpl
    assert "_suma_cuota = _suma_cuota +" not in tpl
