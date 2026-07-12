"""Tests del parser del campo de búsqueda flexible de Compras.

`parse_codigo_compra` separa lo tipeado en código de PROVEEDOR (letras 2-3)
y NÚMERO de compra (dígitos), en cualquier orden y con o sin espacio. Mismo
patrón que /dolares y /anticipos (atajo dueña 2026-07-11).
"""
from __future__ import annotations

import pytest

from modules.compras.views import parse_codigo_compra


@pytest.mark.parametrize(
    "entrada, esperado",
    [
        # Proveedor + número, con espacio.
        ("AC 15", ("AC", 15)),
        # Proveedor + número, sin espacio.
        ("AC15", ("AC", 15)),
        # Número primero, proveedor después.
        ("15 AC", ("AC", 15)),
        # Sólo proveedor.
        ("AC", ("AC", None)),
        # Sólo número.
        ("15", (None, 15)),
        # Código de 3 letras.
        ("ABC 200", ("ABC", 200)),
        # Minúsculas → se normaliza a mayúsculas.
        ("ac 7", ("AC", 7)),
        # Ceros a la izquierda → int limpio.
        ("AC 007", ("AC", 7)),
        # Espacios de más.
        ("  AC   15  ", ("AC", 15)),
        # Vacío / sólo espacios → nada.
        ("", (None, None)),
        ("   ", (None, None)),
        # Sin letras ni dígitos válidos.
        ("--", (None, None)),
    ],
)
def test_parse_codigo_compra(entrada, esperado):
    assert parse_codigo_compra(entrada) == esperado


def test_parse_codigo_compra_none():
    """None de entrada no rompe (se trata como vacío)."""
    assert parse_codigo_compra(None) == (None, None)  # type: ignore[arg-type]
