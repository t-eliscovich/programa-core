"""El signo de un movimiento bancario: una sola regla, en tres archivos.

TMT 2026-07-29. `bank_helpers._signed_delta` es la regla canónica —
`signo_documento × importe`, sin abs y sin caso especial— y su docstring
documenta el caso que importa: «ND -44091 (reverso): -1 * -44091 = +44091 →
saldo sube ✓». Un ND negativo es la DEVOLUCIÓN de un egreso.

El chequeo de salud y la pantalla de diagnóstico tenían su propia copia, y la
copia había mutado: agregaba `if imp < 0: return imp`. Eso invierte el signo
justo en los reversos. En producción eso se veía como «#10 PICHINCHA: primer
drift, Δ $46,88» — que es 2 × 23,44, el importe de tx#43977, un ND de -23,44
por una comisión devuelta. El sistema le sumó 23,44, el chequeo le restó
23,44, y el chequeo culpó a la data.

Estos tests fijan que las tres copias digan lo mismo, con el caso del reverso
al frente.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import bank_helpers

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_salud_dia.py"


@pytest.fixture(scope="module")
def deltas():
    """Las tres implementaciones, listas para comparar."""
    spec = importlib.util.spec_from_file_location("chk_signo", _SCRIPT)
    chk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chk)
    # check_bancos define _signed_delta adentro; se saca del closure.
    import inspect
    src = inspect.getsource(chk.check_bancos)
    ns: dict = {"DOCS_ENTRADA": {"DE", "TR", "XX", "NC", "IN"}}
    # Ejecutar sólo la definición interna, no el check (que toca la DB).
    inicio = src.index("    def _signed_delta")
    fin = src.index("    bancos = db.fetch_all")
    import textwrap
    exec(textwrap.dedent(src[inicio:fin]), ns)  # noqa: S102

    from modules.admin_dbase import salud_view
    return {
        "bank_helpers": lambda d, i: bank_helpers._signed_delta(d, i),
        "check_salud": ns["_signed_delta"],
        "salud_view": salud_view._signed_delta,
    }


CASOS = [
    # (documento, importe, delta esperado, por qué)
    ("ND", 44091.0, -44091.0, "egreso normal: la plata sale"),
    ("ND", -44091.0, +44091.0, "REVERSO de un egreso: la plata vuelve"),
    ("ND", -23.44, +23.44, "tx#43977 real: comisión de banco devuelta"),
    ("DE", 1500.0, +1500.0, "depósito: la plata entra"),
    ("DE", -700.0, -700.0, "tx#43975 real: depósito reversado, la plata sale"),
    ("CH", 1136.48, -1136.48, "cheque emitido: la plata sale"),
    ("NC", 1136.48, +1136.48, "nota de crédito: la plata entra"),
]


@pytest.mark.parametrize("doc,imp,esperado,porque", CASOS)
def test_las_tres_copias_coinciden(deltas, doc, imp, esperado, porque):
    for nombre, fn in deltas.items():
        got = fn(doc, imp)
        assert abs(got - esperado) < 0.005, (
            f"{nombre}({doc}, {imp}) = {got}, esperaba {esperado} — {porque}"
        )


def test_el_atajo_de_los_negativos_no_vuelve(deltas):
    """`if imp < 0: return imp` es el bug concreto: hace que un ND negativo
    baje el saldo en vez de subirlo. Si alguien lo re-agrega, este test lo
    agarra antes de que el chequeo vuelva a culpar a la data."""
    for nombre, fn in deltas.items():
        assert fn("ND", -100.0) == +100.0, (
            f"{nombre} volvió a tratar un ND negativo como egreso: eso es el "
            f"atajo `if imp < 0: return imp`, y produce un drift falso de "
            f"2 × importe"
        )
