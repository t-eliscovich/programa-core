"""Tests del parser del extracto bancario (parser_banco.parse_banco_xlsx).

TMT 2026-06-17 (dueña Tamara): el extracto del Pichincha imprime la FECHA
sólo en la 1ra fila de cada día; las filas siguientes vienen en blanco. ANTES
el parser descartaba esas filas (fecha is None) y se perdían movimientos del
extracto, mientras su importe seguía en el saldo running del banco → descuadre
exacto en la conciliación (el caso del −319,55 que aparecía antes de cruzar
nada). El fix arrastra la última fecha vista (carry-forward).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _xlsx(rows: list[list]) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Fecha", "Concepto", "Documento", "Monto", "Saldo", "Codigo", "Tipo", "Oficina"])
    for r in rows:
        ws.append(r)
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def test_fila_sin_fecha_hereda_la_ultima_no_se_descarta():
    """Una comisión/movimiento en una fila sin fecha (porque el banco la
    imprime sólo en la 1ra fila del día) NO debe perderse: hereda la fecha."""
    from modules.conciliacion.parser_banco import parse_banco_xlsx
    raw = _xlsx([
        ["12/06/2026", "DEPOSITO", "111", 1000, "1000", "001", "C", "NORTE"],
        [None, "COMISION TRANSFERENCIA", "222", 319.55, "680.45", "001", "D", "NORTE"],
        [None, "DEPOSITO", "333", 200, "880.45", "001", "C", "NORTE"],
        ["13/06/2026", "PAGO", "444", 80, "800.45", "001", "D", "NORTE"],
    ])
    movs = parse_banco_xlsx(raw)
    assert len(movs) == 4, "las filas sin fecha NO se descartan (carry-forward)"
    assert str(movs[1].fecha) == "2026-06-12" and movs[1].concepto == "COMISION TRANSFERENCIA"
    assert str(movs[2].fecha) == "2026-06-12"
    assert str(movs[3].fecha) == "2026-06-13"


def test_fila_monto_cero_se_sigue_descartando():
    """Filas vacías reales (monto 0) se ignoran como siempre."""
    from modules.conciliacion.parser_banco import parse_banco_xlsx
    raw = _xlsx([
        ["12/06/2026", "DEPOSITO", "111", 1000, "1000", "001", "C", "NORTE"],
        [None, "SUBTOTAL", "", 0, "1000", "", "", ""],
    ])
    movs = parse_banco_xlsx(raw)
    assert len(movs) == 1


def test_carry_forward_resetea_por_dia_correctamente():
    """El carry-forward usa la última fecha vista; al cambiar de día,
    las filas sin fecha heredan el nuevo día, no el anterior."""
    from modules.conciliacion.parser_banco import parse_banco_xlsx
    raw = _xlsx([
        ["12/06/2026", "A", "1", 10, "10", "001", "C", "N"],
        [None, "B", "2", 20, "30", "001", "C", "N"],
        ["15/06/2026", "C", "3", 30, "60", "001", "C", "N"],
        [None, "D", "4", 40, "100", "001", "C", "N"],
    ])
    movs = parse_banco_xlsx(raw)
    fechas = [str(m.fecha) for m in movs]
    assert fechas == ["2026-06-12", "2026-06-12", "2026-06-15", "2026-06-15"]
