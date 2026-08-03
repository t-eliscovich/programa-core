"""Dos bugs del 03/08 que la dueña marcó mirando la pantalla.

1. El KPI de /cheques contaba 94 cheques dos veces (1459 en vez de 1365).
2. `csv_upload.parse_monto` era una segunda implementación peor que la de
   `parsers`: "1,234.56" entraba como 1,23.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

import csv_upload
import parsers

RAIZ = Path(__file__).resolve().parent.parent
LISTA = (RAIZ / "modules/cheques/templates/cheques/lista.html").read_text(encoding="utf-8")


def test_kpi_usa_cartera_total_y_no_suma_buckets_solapados():
    """`cartera` YA es Z+P+1+2 — sumarle postergados y devueltos los duplica.

    Números reales del 03/08: 1363 + 73 + 21 + 2 = 1459, contra 1365 que
    muestra la pestaña "Cartera total". El KPI y la pestaña salen del MISMO
    conteo o vuelven a divergir.
    """
    assert "conteos.get('cartera_total', {})" in LISTA
    for k in ("devueltos_en_gestion", "postergados", "daniela"):
        assert f"c_{k[:9]}" not in LISTA or "cartera_total" in LISTA
    # La suma de buckets no puede volver: si alguien la reescribe, esto falla.
    assert "+ (c_postergados.get('total', 0) | float)" not in LISTA
    assert "+ (c_dev_gest.get('total', 0) | float)" not in LISTA


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("1,234.56", Decimal("1234.56")),   # US con miles — el que se rompía
        ("1.234,56", Decimal("1234.56")),   # ES con miles
        ("1234,56", Decimal("1234.56")),    # ES sin miles
        ("-3.75", Decimal("-3.75")),        # punto decimal, negativo
        ("500", Decimal("500")),
    ],
)
def test_los_dos_parsers_de_monto_dan_LO_MISMO(texto, esperado):
    assert parsers.parse_monto(texto) == esperado
    assert csv_upload.parse_monto(texto) == esperado


def test_csv_upload_no_reimplementa_el_parser():
    fuente = (RAIZ / "csv_upload.py").read_text(encoding="utf-8")
    assert "from parsers import parse_monto" in fuente
    assert 's.replace(".", "").replace(",", ".")' not in fuente


def test_monto_basura_sigue_siendo_error():
    for basura in ("abc", "xyz", "$$"):
        with pytest.raises(ValueError):
            csv_upload.parse_monto(basura)
    assert csv_upload.parse_monto("") is None
    assert csv_upload.parse_monto(None) is None
