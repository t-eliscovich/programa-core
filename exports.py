"""CSV export helper compartido entre list views.

Uso:
    from exports import csv_response

    # Forma simple: solo (key, header) — cada valor se formatea con _fmt() por
    # tipo Python (Decimal/float → num_es, date → fecha_es, etc.).
    return csv_response(
        filas,
        columnas=[("codigo_cli", "Código"), ("nombre", "Cliente"), ("saldo_total", "Saldo")],
        filename="clientes.csv",
    )

    # Forma avanzada: (key, header, fmt) donde `fmt` es un callable que recibe
    # el valor crudo de la fila y devuelve str. Útil cuando el default no alcanza:
    #   - monto con 4 decimales en vez de 2
    #   - porcentaje: 0.1425 → "14,25%"
    #   - enum a etiqueta human-friendly: "Y" → "Anulado"
    #   - concatenación de columnas: lambda v, row=row: ...
    from filters import num_es
    return csv_response(
        filas,
        columnas=[
            ("factura", "Factura"),
            ("subtotal", "Subtotal", lambda v: num_es(v, 4)),
            ("retencion_pct", "Ret. %", lambda v: f"{num_es(v, 2)}%" if v is not None else ""),
            ("estado", "Estado", lambda v: "Anulado" if v == "Y" else "Activo"),
        ],
        filename="facturas.csv",
    )

- Usa BOM UTF-8 para que Excel abra con acentos bien.
- Separador: punto y coma ';' (es-EC usa coma como decimal, así que ; evita choque).
- Números con formato es-EC (coma decimal, punto miles) vía filters.num_es.
"""
from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal

from flask import Response

from filters import fecha_es, num_es

# Per-column formatter: recibe el valor crudo, devuelve str. None → "".
Formatter = Callable[[object], str]

# Una columna puede ser (key, header) o (key, header, fmt).
ColumnSpec = tuple[str, str] | tuple[str, str, Formatter]


def _fmt(value) -> str:
    """Formatter por defecto: elige según tipo Python."""
    if value is None:
        return ""
    if isinstance(value, datetime | date):
        return fecha_es(value)
    if isinstance(value, Decimal | float):
        return num_es(value, 2)
    if isinstance(value, int):
        return str(value)
    return str(value)


def _apply_fmt(value, fmt: Formatter | None) -> str:
    """Aplica un formatter custom si lo hay, si no cae al default _fmt.

    Si el formatter custom revienta (ej: recibe None sin chequearlo), cae al
    default en vez de tirar la exportación entera. Mejor un "0,00" mal
    formateado que un 500 en medio de una descarga.
    """
    if fmt is None:
        return _fmt(value)
    try:
        result = fmt(value)
        # Los custom formatters pueden devolver int/Decimal si son perezosos —
        # forzamos str siempre, así el CSV queda consistente.
        return "" if result is None else str(result)
    except Exception:
        return _fmt(value)


def csv_response(
    filas: Iterable[dict],
    columnas: Sequence[ColumnSpec],
    filename: str = "export.csv",
) -> Response:
    """Return a Flask Response with a CSV attachment.

    columnas = [(key, header) | (key, header, fmt), ...]

    `fmt` (opcional) es una función `value -> str` que sobreescribe el
    formatter por defecto para esa columna. Útil para precisión, unidades,
    etiquetas, etc. Ver docstring del módulo para ejemplos.
    """
    # Validar forma ANTES de tocar el buffer — si hay una columna mal formada
    # preferimos el ValueError limpio al 500 a mitad del CSV.
    headers: list[str] = []
    prepared: list[tuple[str, Formatter | None]] = []
    for col in columnas:
        if len(col) == 2:
            key, header = col
            prepared.append((key, None))
        elif len(col) == 3:
            key, header, fmt = col
            prepared.append((key, fmt))
        else:
            raise ValueError(
                f"columnas debe ser (key,header) o (key,header,fmt); recibí {col!r}"
            )
        headers.append(header)

    buf = io.StringIO()
    # BOM for Excel es-EC
    buf.write("\ufeff")
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    w.writerow(headers)
    for row in filas:
        w.writerow([_apply_fmt(row.get(k), fmt) for k, fmt in prepared])
    data = buf.getvalue().encode("utf-8")
    return Response(
        data,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
