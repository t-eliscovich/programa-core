"""INVARIANTE del reverso bancario: original + reverso = 0 sobre el saldo.

Por qué existe (dueña 2026-07-31: *"¿cómo no había un test para esto?"*).
Había tests del reverso, pero todos con importes POSITIVOS — la única forma en
que se podían cargar movimientos hasta ese día. Al habilitar el importe
NEGATIVO (convención FoxPro) se cambió una punta del invariante y no se revisó
la otra: `reversar_movimiento_simple` seguía haciendo `abs()` + tipo opuesto, y
el reverso pasó a DUPLICAR el error en vez de deshacerlo. $24.138,78 en prod.

La lección: cuando se ensancha el dominio de un valor (acá, el signo del
importe), el test que vale no es otro caso más — es el INVARIANTE recorrido
sobre TODO el dominio nuevo. Este archivo hace eso.
"""
import itertools

import bank_helpers

DOCS = ("DE", "NC", "ND", "TR", "CH", "DB")
IMPORTES = (0.01, 1.0, 84.34, 5274.03, -0.01, -1.0, -84.34, -5274.03)


def _efecto(doc, importe):
    """Lo que el movimiento le hace al saldo."""
    return bank_helpers._signed_delta(doc, importe)


def _reverso(doc_orig, importe_orig):
    """Espejo de reversar_movimiento_simple (misma regla, aislada)."""
    if importe_orig < 0:
        return doc_orig, abs(importe_orig)
    return {"DE": "CH", "NC": "CH", "ND": "NC", "TR": "CH", "CH": "NC",
            "DB": "NC"}[doc_orig], abs(importe_orig)


def test_invariante_sobre_todo_el_dominio():
    """Para CUALQUIER (documento, importe con cualquier signo): efecto + efecto_reverso == 0."""
    fallos = []
    for doc, imp in itertools.product(DOCS, IMPORTES):
        d_rev, i_rev = _reverso(doc, imp)
        neto = round(_efecto(doc, imp) + _efecto(d_rev, i_rev), 2)
        if neto != 0:
            fallos.append(f"{doc} {imp:+} → {d_rev} {i_rev:+} deja {neto:+}")
    assert not fallos, "El reverso no anula:\n" + "\n".join(fallos)


def test_el_signo_del_efecto_no_depende_solo_del_tipo():
    """La trampa que hizo caer el reverso: el tipo NO alcanza para saber el signo."""
    assert _efecto("ND", 100.0) < 0      # cargo → baja
    assert _efecto("ND", -100.0) > 0     # deshacer el cargo → sube
    assert _efecto("DE", 100.0) > 0      # depósito → sube
    assert _efecto("DE", -100.0) < 0     # deshacer el depósito → baja


def test_el_comportamiento_viejo_habria_fallado():
    """Deja constancia del bug: abs() + tipo opuesto sumaba dos veces."""
    viejo_doc, viejo_imp = "NC", abs(-5274.03)
    assert _efecto("ND", -5274.03) == 5274.03
    assert _efecto(viejo_doc, viejo_imp) == 5274.03   # ambos SUMAN
    assert round(_efecto("ND", -5274.03) + _efecto(viejo_doc, viejo_imp), 2) == 10548.06
