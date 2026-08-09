"""El concepto del audit, escrito como lo lee una persona.

TMT 2026-08-09, después de arreglar caja/gastos: *"¿algo más? mostrame
ejemplos"*. Barrido sobre los 19.465 movimientos activos:

  · **15.539** conceptos escriben "#" delante de un número que casi siempre es
    el VISIBLE (`numf`, `no_cheque`): el "#" lo hace pasar por id interno, que
    es exactamente la confusión del día ("¿el número es del cheque real o del
    programa?");
  · **4.508** terminan con el importe entre paréntesis — el MISMO número que
    ya está en la columna Importe, y encima con punto decimal;
  · **5.402** arrancan con "[backfill]", el nombre del proceso que los cargó;
  · **480** dicen "#0": las facturas del dBase que llegaron sin número;
  · **177** dicen "Gasto #1", donde el 1 es la CATEGORÍA V1.

Se limpia al MOSTRAR: `mov_doble.concepto` es el audit y no se toca.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.historial import queries  # noqa: E402
from modules.historial.views import _concepto_legible as limpio  # noqa: E402


@pytest.mark.parametrize("crudo, legible", [
    # El numeral se va: esos números son los que ella lee en las pantallas.
    ("Cheque #100392 → Factura #176812 (155.10)", "Cheque 100392 → Factura 176812"),
    ("Dep. cheque #100007 EDR", "Dep. cheque 100007 EDR"),
    ("DEVOLUCION Factura #10973 DSN", "DEVOLUCION Factura 10973 DSN"),
    # El proceso que la cargó no es información.
    ("[backfill] Factura #0 BED", "Factura s/n · BED"),
    # El 1 es la categoría V1, no un correlativo.
    ("[backfill] Gasto #1 OTR — KK SU CAJA", "Gasto V1 · KK SU CAJA"),
    # Y acá el 368 SÍ era un id interno: se va con la frase entera.
    ("Clasificar caja #368 como gasto V7: CCSU CAJA",
     "Caja → Gasto V7 · CCSU CAJA"),
])
def test_conceptos_reales_del_historial(crudo, legible):
    assert limpio(crudo) == legible


def test_el_importe_repetido_se_va():
    """Es el mismo número de la columna Importe, con punto decimal."""
    assert limpio("Dep. Pich. → Factura #172730 (1283.19)") == \
        "Dep. Pich. → Factura 172730"


def test_no_se_come_un_parentesis_que_no_es_un_importe():
    assert limpio("Cheque 55 (parcial)") == "Cheque 55 (parcial)"


def test_un_concepto_limpio_no_cambia():
    assert limpio("SU ADM QUINTEROS") == "SU ADM QUINTEROS"
    assert limpio("") == ""
    assert limpio(None) == ""


def test_los_reversos_que_faltaban_tienen_castellano():
    assert queries.label("reverso_retiro_op") == "Reverso: retiro OP"
    assert queries.label("reverso_cheque_emitido") == "Reverso: cheque emitido"
    assert queries.label("reverso_retiro_dbase") == "Reverso: retiro"
