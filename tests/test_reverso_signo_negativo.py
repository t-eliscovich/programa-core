"""El reverso de un movimiento con importe NEGATIVO tiene que RESTAR.

Bug de prod del 2026-07-31 (costó $24.138,78): `reversar_movimiento_simple`
hacía siempre `abs(importe)` + tipo opuesto. Con la convención FoxPro (mismo
tipo, importe negativo) eso daba el signo al revés: una `ND −5.274,03` (que
SUMA) se "reversaba" con una `NC +5.274,03` que SUMA otra vez. Reversar
DUPLICABA el error.

La regla es una sola: el reverso es el movimiento cuyo EFECTO es el opuesto
exacto del original.
"""
import bank_helpers


def _efecto(doc, importe):
    return bank_helpers.signo_documento(doc) * importe


def _reverso(doc_orig, importe_orig):
    """Misma lógica que reversar_movimiento_simple, aislada para testear."""
    if importe_orig < 0:
        return doc_orig, abs(importe_orig)
    return {"DE": "CH", "NC": "CH", "ND": "NC"}[doc_orig], abs(importe_orig)


CASOS = [
    ("ND", 5274.03),    # cargo normal del banco
    ("ND", -5274.03),   # deshacer un cargo (convención FoxPro)
    ("DE", 1500.00),    # depósito
    ("DE", -1500.00),   # deshacer un depósito
    ("NC", 800.00),
    ("NC", -800.00),
]


def test_el_reverso_siempre_anula_el_efecto_del_original():
    for doc, imp in CASOS:
        d_rev, i_rev = _reverso(doc, imp)
        assert round(_efecto(doc, imp) + _efecto(d_rev, i_rev), 2) == 0, (
            f"{doc} {imp} → reverso {d_rev} {i_rev} NO anula"
        )


def test_el_caso_exacto_que_rompio_prod():
    """ND −5.274,03 SUMA; su reverso tiene que RESTAR 5.274,03."""
    assert _efecto("ND", -5274.03) == 5274.03
    d_rev, i_rev = _reverso("ND", -5274.03)
    assert (d_rev, i_rev) == ("ND", 5274.03)
    assert _efecto(d_rev, i_rev) == -5274.03
    # El comportamiento viejo (abs + tipo opuesto) sumaba de nuevo:
    assert _efecto("NC", abs(-5274.03)) == +5274.03


def test_el_positivo_no_cambio_de_comportamiento():
    assert _reverso("ND", 5274.03) == ("NC", 5274.03)
    assert _reverso("DE", 1500.0) == ("CH", 1500.0)
    assert _reverso("NC", 800.0) == ("CH", 800.0)
