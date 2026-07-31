"""Importe NEGATIVO en el alta manual de banco (convención FoxPro).

Dueña 2026-07-31: "dejame cargar con signo negativo o positivo en cualquier
lado porque me parece que el problema es de signo. también igualate al dBase
en signos y que no vuelva a pasar."

El motor (`_signed_delta`) ya calculaba bien el negativo; lo que faltaba era
dejarlo entrar. Estos tests clavan las dos mitades: el delta y la guarda.
"""
import pytest

import bank_helpers


def test_signed_delta_ya_manejaba_el_negativo_como_el_dbase():
    """`ND −44.091` SUBE el saldo, igual que en PICHINCH.DBF."""
    assert bank_helpers._signed_delta("ND", 44091.0) == -44091.0
    assert bank_helpers._signed_delta("ND", -44091.0) == 44091.0
    assert bank_helpers._signed_delta("DE", 1500.0) == 1500.0
    assert bank_helpers._signed_delta("DE", -1500.0) == -1500.0


def test_sin_permitir_signed_el_negativo_sigue_rebotando():
    """La guarda vieja queda viva para todos los callers automáticos."""
    with pytest.raises(ValueError, match="permitir_signed"):
        bank_helpers.insert_movimiento_bancario(
            None, no_banco=10, no_cta=None, fecha=None,
            documento="ND", importe=-100.0, concepto="x",
        )


def test_importe_cero_sigue_siendo_error_con_o_sin_signed():
    for signed in (False, True):
        with pytest.raises(ValueError, match="!= 0"):
            bank_helpers.insert_movimiento_bancario(
                None, no_banco=10, no_cta=None, fecha=None,
                documento="ND", importe=0, concepto="x",
                permitir_signed=signed,
            )


def test_signo_documento_no_cambio():
    assert bank_helpers.signo_documento("DE") == 1
    assert bank_helpers.signo_documento("NC") == 1
    assert bank_helpers.signo_documento("ND") == -1
    assert bank_helpers.signo_documento("CH") == -1
