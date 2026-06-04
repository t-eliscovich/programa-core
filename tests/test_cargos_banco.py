"""Tests del clasificador cargos_banco — separa pendientes reales de cargos.

Caso de origen (conciliación de Alex, 2026-06-04): 150 pendientes →
146 reales (164.247,95) + 4 cargos (−99,84). Validado contra su hoja.
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _items():
    # subconjunto representativo (signed: + crédito, − débito)
    return [
        {"documento": "56379469", "concepto": "TRANSFERENCIA INTERBANCARIA DE X", "monto": 150.0},
        {"documento": "41508270", "concepto": "DEPOSITO", "monto": 590.27},
        {"documento": "AC97", "concepto": "", "monto": 15835.60},                 # acreditación
        {"documento": "43158599", "concepto": "PAGO SENAE 51436622", "monto": -15930.46},  # par
        {"documento": "11501043", "concepto": "PAGO SENAE 51500492", "monto": -14516.42},  # suelto -> real
        {"documento": "53243935", "concepto": "COST CHEQUE DEVUELTO", "monto": -2.49},
        {"documento": "53258781", "concepto": "COST CHEQUE DEVUELTO", "monto": -2.49},
        {"documento": "2030", "concepto": "CHEQUE DEVUELTO", "monto": -525.0},     # real (negativo)
        {"documento": "85", "concepto": "CHEQUE DEVUELTO", "monto": -400.0},       # real (negativo)
        {"documento": "73946906", "concepto": "2606020CR1SI-COMISION-PAG", "monto": -49.54},
        {"documento": "73946906b", "concepto": "IVA COBRADO", "monto": -7.43},
    ]


def test_cargos_son_solo_fees_y_par_aduana():
    from modules.conciliacion.cargos_banco import clasificar_cargos
    reales, cargos = clasificar_cargos(_items())
    docs_cargo = {c["documento"] for c in cargos}
    # El par de aduana + costos de cheque + comisión/IVA
    assert "AC97" in docs_cargo
    assert "43158599" in docs_cargo
    assert "53243935" in docs_cargo and "53258781" in docs_cargo
    assert "73946906" in docs_cargo and "73946906b" in docs_cargo


def test_cheque_devuelto_real_queda_pendiente_negativo():
    """Alex: 'el cheque sí debe quedar como negativo, no con las comisiones'."""
    from modules.conciliacion.cargos_banco import clasificar_cargos
    reales, cargos = clasificar_cargos(_items())
    docs_real = {r["documento"]: r["monto"] for r in reales}
    assert "2030" in docs_real and docs_real["2030"] == -525.0
    assert "85" in docs_real and docs_real["85"] == -400.0
    # COST CHEQUE no debe estar en reales
    assert "53243935" not in docs_real


def test_pago_senae_suelto_sin_acreditacion_queda_real():
    from modules.conciliacion.cargos_banco import clasificar_cargos
    reales, cargos = clasificar_cargos(_items())
    docs_real = {r["documento"] for r in reales}
    assert "11501043" in docs_real  # SENAE 51500492 sin AC par -> real


def test_par_aduana_netea_la_comision():
    from modules.conciliacion.cargos_banco import resumen
    R = resumen(_items())
    # AC97 (+15835.60) + SENAE 43158599 (−15930.46) = −94.86
    # + 2 cost cheque (−4.98) + comision 49.54 + iva 7.43 = −156.81
    assert abs(R["cargos"]["neto"] - (-156.81)) < 0.01


def test_sin_cargos_no_rompe():
    from modules.conciliacion.cargos_banco import clasificar_cargos
    items = [{"documento": "1", "concepto": "DEPOSITO", "monto": 100.0},
             {"documento": "2", "concepto": "TRANSFERENCIA DIRECTA DE X", "monto": 50.0}]
    reales, cargos = clasificar_cargos(items)
    assert len(cargos) == 0 and len(reales) == 2
