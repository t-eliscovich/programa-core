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


# ─── TMT decisión 2026-06-17: nuevos patterns reportados por Tamara ─────
# Caso: sesion #40 (mov-06-16). El xlsx no mostraba gastos/comisiones a
# pesar de tener ISD-PAG, IMPUESTO, etc. en el extracto.


def test_isd_pag_se_clasifica_como_cargo():
    """ISD-PAG = Impuesto Salida Divisas. Cobrado por el banco al pagar
    al exterior. Es un cargo del banco/gobierno, no un pendiente real."""
    from modules.conciliacion.cargos_banco import clasificar_cargos
    items = [
        {"documento": "24264183", "concepto": "2606090CZZOR-ISD-PAG-ACPI 6763", "monto": -356.68},
        {"documento": "24264386", "concepto": "2606090D0090-ISD-PAG-OFFERTA 261045R1", "monto": -122.99},
        {"documento": "45686796", "concepto": "2606090D0AZD-ISD-PAG-ACPI 6776 6777", "monto": -1940.03},
        {"documento": "1234", "concepto": "DEPOSITO", "monto": 100.0},
    ]
    reales, cargos = clasificar_cargos(items)
    docs_cargo = {c["documento"] for c in cargos}
    assert "24264183" in docs_cargo, "ISD-PAG corto debe ir a cargos"
    assert "24264386" in docs_cargo, "ISD-PAG larguito debe ir a cargos"
    assert "45686796" in docs_cargo, "ISD-PAG con varios refs debe ir a cargos"
    docs_real = {r["documento"] for r in reales}
    assert "1234" in docs_real, "DEPOSITO sigue siendo real"


def test_impuesto_palabra_completa_se_clasifica_como_cargo():
    from modules.conciliacion.cargos_banco import clasificar_cargos
    items = [
        {"documento": "i1", "concepto": "IMPUESTO RETENCION FUENTE", "monto": -50.0},
    ]
    reales, cargos = clasificar_cargos(items)
    docs_cargo = {c["documento"] for c in cargos}
    assert "i1" in docs_cargo


def test_interes_se_clasifica_como_cargo():
    from modules.conciliacion.cargos_banco import clasificar_cargos
    items = [
        {"documento": "int1", "concepto": "INTERES POR SOBREGIRO", "monto": -25.0},
        {"documento": "int2", "concepto": "INTERESES COBRADOS", "monto": -50.0},
        {"documento": "int3", "concepto": "INTERÉS BANCARIO", "monto": -10.0},
    ]
    reales, cargos = clasificar_cargos(items)
    docs_cargo = {c["documento"] for c in cargos}
    assert "int1" in docs_cargo
    assert "int2" in docs_cargo
    assert "int3" in docs_cargo


def test_debito_automatico_se_clasifica_como_cargo():
    from modules.conciliacion.cargos_banco import clasificar_cargos
    items = [
        {"documento": "da1", "concepto": "DEBITO AUTOMATICO POR SERVICIO", "monto": -30.0},
        {"documento": "da2", "concepto": "DÉBITO AUTOMÁTICO", "monto": -15.0},
    ]
    reales, cargos = clasificar_cargos(items)
    docs_cargo = {c["documento"] for c in cargos}
    assert "da1" in docs_cargo
    assert "da2" in docs_cargo


def test_costo_transfer_y_cobro_servicio():
    from modules.conciliacion.cargos_banco import clasificar_cargos
    items = [
        {"documento": "ct1", "concepto": "COSTO TRANSFERENCIA INTERBANCARIA", "monto": -2.0},
        {"documento": "cs1", "concepto": "COBRO SERVICIO PRODUCTO BANCARIO", "monto": -5.0},
    ]
    reales, cargos = clasificar_cargos(items)
    docs_cargo = {c["documento"] for c in cargos}
    assert "ct1" in docs_cargo
    assert "cs1" in docs_cargo


def test_no_falsos_positivos_en_conceptos_legitimos():
    """Anti-regresión: conceptos LEGÍTIMOS de pendientes reales NO deben
    caer a cargos. Casos del xlsx real de Tamara (sesion #40)."""
    from modules.conciliacion.cargos_banco import clasificar_cargos
    items = [
        {"documento": "56379469", "concepto": "TRANSFERENCIA INTERBANCARIA DE LOPEZ CALDERON", "monto": 150.0},
        {"documento": "41508270", "concepto": "DEPOSITO", "monto": 590.27},
        {"documento": "27183123", "concepto": "COBRO INTERBANCARIO RECIBIDO A INTELA", "monto": -83.86},
        {"documento": "168888370", "concepto": "2606010CPWFO-BANCO PI-PAG-1009050517", "monto": 72.3},
        {"documento": "27540119", "concepto": "PAGO CHEQUE - NUMERO DE CHEQUE:15532", "monto": -135.51},
        {"documento": "1969", "concepto": "CJE DEF 99 REG CHQ 1969 130526 MC", "monto": 60.0},
        {"documento": "depnoid", "concepto": "DEPOSITO NO IDENTIFICADO", "monto": 322.72},
    ]
    reales, cargos = clasificar_cargos(items)
    docs_real = {r["documento"] for r in reales}
    for doc in ("56379469", "41508270", "27183123", "168888370", "27540119", "1969", "depnoid"):
        assert doc in docs_real, f"{doc} debería quedar como REAL, no cargo"
