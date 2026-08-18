"""El Origen del anticipo en dólares lleva el número de importación.

TMT 2026-08-18 (dueña, mirando tres filas seguidas del 18/08 en
`/historial`): *"poner el numero de AI"*. Las tres decían `USD AI` en la
columna Origen — la misma celda para tres anticipos DISTINTOS (44/26, 33/26 y
28 SALDO) — y el único lugar donde se veía cuál era cuál era el concepto, al
otro extremo de la fila.

El número sale del MISMO parser que usa /dolares (`parse_ref_anticipo`), así
que las dos pantallas dicen el mismo número. El año se abrevia a dos dígitos
porque así lo escribe la persona que carga el anticipo.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.historial.views import _ref_anticipo  # noqa: E402


def test_el_numero_y_el_anio_van_pegados_a_la_cuenta():
    """Las tres filas del 18/08 dejan de ser la misma celda repetida."""
    assert f"USD AI{_ref_anticipo('44/26')}" == "USD AI 44/26"
    assert f"USD AI{_ref_anticipo('33/26')}" == "USD AI 33/26"


def test_el_anio_se_abrevia_a_dos_digitos():
    """En el concepto se escribe `/26`, no `/2026` — la celda dice lo mismo."""
    assert _ref_anticipo("58 / 2026") == " 58/26"


def test_sin_anio_queda_solo_el_numero():
    """🚨 El anticipo viejo todavía no tiene año y no hay que inventárselo:
    "28 SALDO" es el 28, no el 28 de ningún año en particular."""
    assert _ref_anticipo("28 SALDO") == " 28"
    assert _ref_anticipo("AC 95") == " 95"


def test_un_concepto_sin_numero_deja_la_etiqueta_como_estaba():
    assert _ref_anticipo("SALDO") == ""
    assert _ref_anticipo("") == ""
    assert _ref_anticipo(None) == ""
