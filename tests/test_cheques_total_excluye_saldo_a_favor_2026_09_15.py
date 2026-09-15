"""El total de /cheques no puede netear contra el espejo del saldo a favor.

TMT 2026-09-15 — filtrando por cliente=RUS: 2 devueltos (+400 c/u) y 2
"Saldo a favor" ANTICIPO (-400 c/u, `no_banco=98`) daban un total de $0.
*"Falta el valor en el total"* / *"El de abajo no dice nada"*.

El 98 es el espejo NEGATIVO de un anticipo ya aplicado (ver
`etiqueta_cobro` — "175 filas, TODAS con importe negativo"): no es plata
que se está cobrando ahora, es la contrapartida contable. Sumarlo junto a
los cheques reales netea el total contra sí mismo y el número deja de decir
algo — por eso se excluye de:

1. `total_buscar()` — el KPI de arriba del listado.
2. El checkbox "Depositar lote" — no se puede depositar un espejo contable,
   y dejarlo marcable arrastraba su importe negativo a la barra de abajo.

La fila SIGUE viéndose en el listado (es información real: "este cliente
tiene un saldo a favor de $X aplicado") — sólo deja de entrar en las sumas.
"""
from __future__ import annotations

import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
QUERIES = (RAIZ / "modules/cheques/queries.py").read_text(encoding="utf-8")
LISTA = (RAIZ / "modules/cheques/templates/cheques/lista.html").read_text(encoding="utf-8")


def _cuerpo_total_buscar() -> str:
    _, _, cola = QUERIES.partition("def total_buscar(")
    return cola.split("\ndef ")[0]


def test_total_buscar_excluye_el_espejo_no_banco_98():
    cuerpo = _cuerpo_total_buscar()
    assert re.search(r"no_banco,\s*0\)\s*<>\s*98", cuerpo), (
        "total_buscar() tiene que excluir no_banco=98 (espejo del saldo a "
        "favor) de la SUMA, o el total neta contra sí mismo y queda en $0 "
        "aunque haya cheques reales devueltos"
    )


def test_total_buscar_sigue_excluyendo_los_reversados():
    """El fix de hoy no puede pisar el filtro de stat='X' que ya existía."""
    cuerpo = _cuerpo_total_buscar()
    assert "COALESCE(c.stat, '') <> 'X'" in cuerpo


def test_checkbox_de_depositar_lote_excluye_el_espejo_no_banco_98():
    assert (
        "{% if stat_u in ('Z', 'P', '1', '2') and (c.no_banco or 0)|int != 98 %}"
        in LISTA
    ), (
        "el checkbox de 'Depositar lote' tiene que seguir excluyendo el "
        "espejo del saldo a favor (no_banco=98): no es un cheque "
        "depositable y arrastraba su importe negativo a la barra de abajo"
    )
