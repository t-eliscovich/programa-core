"""Tests para `exports.csv_response`.

Cubre:
  - Formato por defecto (columnas de 2 elementos) — se mantiene el comportamiento
    histórico: Decimal → "1.234,56", date → "DD/MM/YYYY", None → "".
  - Formatter custom por columna (3 elementos) — tiene prioridad sobre el default.
  - Resiliencia: si un formatter custom revienta, cae al default en vez de tirar
    toda la exportación.
  - Forma inválida (tupla de 1 o 4 elementos) levanta ValueError claro.
  - BOM UTF-8 y separador ';' siguen intactos.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Flask app context — csv_response devuelve un Response, no necesita app,
# pero la importación de filters sí (no usa current_app). Si en el futuro
# filters necesita contexto, acá es donde habría que crear uno.
from exports import csv_response  # noqa: E402
from filters import num_es  # noqa: E402


def _body(resp) -> str:
    """Decodifica el body del Response para inspección."""
    return resp.get_data(as_text=True)


def test_formato_default_por_tipo():
    filas = [
        {"nombre": "Acme", "saldo": Decimal("1234.56"), "fecha": date(2026, 4, 17)},
        {"nombre": "Beta", "saldo": None, "fecha": None},
    ]
    resp = csv_response(
        filas,
        columnas=[("nombre", "Cliente"), ("saldo", "Saldo"), ("fecha", "Fecha")],
        filename="x.csv",
    )
    body = _body(resp)
    # BOM + header + 2 rows
    assert body.startswith("\ufeff")
    assert "Cliente;Saldo;Fecha" in body
    assert "Acme;1.234,56;17/04/2026" in body
    assert "Beta;;" in body


def test_formatter_custom_por_columna():
    filas = [{"pct": Decimal("0.1425"), "qty": Decimal("3.4567")}]
    resp = csv_response(
        filas,
        columnas=[
            ("pct", "Ret. %", lambda v: f"{num_es(v * 100, 2)}%"),
            ("qty", "Cantidad", lambda v: num_es(v, 4)),
        ],
        filename="x.csv",
    )
    body = _body(resp)
    assert "Ret. %;Cantidad" in body
    # 0.1425 * 100 = 14.25 → "14,25%"
    assert "14,25%;3,4567" in body


def test_formatter_custom_revienta_cae_al_default():
    """Si el formatter recibe un tipo inesperado y explota, no tira toda la exportación."""
    def roto(v):
        return v.upper()  # revienta si v es Decimal o None

    filas = [{"x": Decimal("10.5")}, {"x": None}]
    resp = csv_response(
        filas,
        columnas=[("x", "X", roto)],
        filename="x.csv",
    )
    body = _body(resp)
    # Decimal("10.5") cayó al default _fmt → "10,50"; None → ""
    assert "10,50" in body
    lineas = [ln for ln in body.split("\r\n") if ln]
    # header + 2 filas de data
    assert len(lineas) == 3


def test_formatter_custom_devuelve_none_es_vacio():
    """Un formatter que devuelve None se trata como string vacío, no tira."""
    filas = [{"x": 42}, {"x": 99}]
    resp = csv_response(
        filas,
        columnas=[("label", "Label"), ("x", "X", lambda _v: None)],
        filename="x.csv",
    )
    body = _body(resp)
    # Cada fila debe tener la X vacía (no "None" ni error)
    # "Label;X" header + ";" + ";" para las dos filas con label missing
    # Afirmamos directamente sobre el contenido.
    assert "Label;X" in body
    # No debe aparecer "None" como texto literal — eso sería el bug
    assert "None" not in body


def test_columna_forma_invalida_levanta():
    with pytest.raises(ValueError):
        csv_response(
            [{"x": 1}],
            columnas=[("x",)],  # falta header
        )
    with pytest.raises(ValueError):
        csv_response(
            [{"x": 1}],
            columnas=[("x", "X", lambda v: v, "extra")],  # 4 elementos
        )


def test_header_y_mimetype():
    resp = csv_response(
        [{"a": 1}],
        columnas=[("a", "A")],
        filename="clientes.csv",
    )
    assert resp.mimetype.startswith("text/csv")
    assert 'filename="clientes.csv"' in resp.headers.get("Content-Disposition", "")
