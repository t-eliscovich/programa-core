"""Tests del módulo conciliación — parser + matcher."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from modules.conciliacion.matcher import _monto_matches, _referencia_matches, matchear
from modules.conciliacion.parser import BancoLinea, _parse_fecha, _parse_monto, parse_csv

# ---------------------------------------------------------------------------
# Parser — fechas
# ---------------------------------------------------------------------------

def test_parse_fecha_dd_mm_yyyy():
    assert _parse_fecha("17/04/2026") == date(2026, 4, 17)


def test_parse_fecha_iso():
    assert _parse_fecha("2026-04-17") == date(2026, 4, 17)


def test_parse_fecha_dd_mm_yyyy_con_guion():
    assert _parse_fecha("17-04-2026") == date(2026, 4, 17)


def test_parse_fecha_invalida():
    assert _parse_fecha("no es fecha") is None
    assert _parse_fecha("") is None
    assert _parse_fecha("32/04/2026") is None  # día inválido


# ---------------------------------------------------------------------------
# Parser — montos
# ---------------------------------------------------------------------------

def test_parse_monto_iso():
    assert _parse_monto("1234.56") == Decimal("1234.56")


def test_parse_monto_es_ec():
    assert _parse_monto("1.234,56") == Decimal("1234.56")


def test_parse_monto_coma_decimal_sin_miles():
    assert _parse_monto("1234,56") == Decimal("1234.56")


def test_parse_monto_negativo_parentesis():
    assert _parse_monto("(123.45)") == Decimal("-123.45")


def test_parse_monto_vacio_es_cero():
    assert _parse_monto("") == Decimal(0)
    assert _parse_monto(None) == Decimal(0)


def test_parse_monto_basura_es_cero():
    # Tolerancia: un token basura no rompe el batch.
    assert _parse_monto("abc") == Decimal(0)


# ---------------------------------------------------------------------------
# Parser CSV completo
# ---------------------------------------------------------------------------

_CSV_PICHINCHA = b"""fecha;concepto;referencia;debito;credito
17/04/2026;Cheque pagado;1001;500.00;0.00
18/04/2026;Deposito cheque;1002;0.00;500.00
19/04/2026;Cheque devuelto 1002;1002;500.00;0.00
"""


def test_parse_csv_pichincha_basico():
    lineas = parse_csv(_CSV_PICHINCHA)
    assert len(lineas) == 3
    assert lineas[0].fecha == date(2026, 4, 17)
    assert lineas[0].debito == Decimal("500.00")
    assert lineas[2].concepto == "Cheque devuelto 1002"


def test_parse_csv_con_bom():
    """BOM UTF-8 no rompe el parser."""
    raw = b"\xef\xbb\xbf" + _CSV_PICHINCHA
    lineas = parse_csv(raw)
    assert len(lineas) == 3


def test_parse_csv_encoding_cp1252():
    """Fallback a CP1252 cuando UTF-8 falla."""
    raw = b"fecha;concepto;referencia;debito;credito\n17/04/2026;CARGA \xf1andu;1001;500,00;0,00\n"
    lineas = parse_csv(raw)
    assert len(lineas) == 1
    assert "andu" in lineas[0].concepto.lower()


def test_parse_csv_con_comma_como_separador():
    raw = b"fecha,concepto,referencia,debito,credito\n17/04/2026,x,1001,500.00,0\n"
    lineas = parse_csv(raw)
    assert len(lineas) == 1


def test_parse_csv_lineas_invalidas_se_ignoran():
    raw = b"fecha;concepto;referencia;debito;credito\nbasura;x;1001;500,00;0\n17/04/2026;ok;1002;500,00;0\n"
    lineas = parse_csv(raw)
    assert len(lineas) == 1  # la línea con fecha basura se ignora
    assert lineas[0].referencia == "1002"


def test_parse_csv_vacio():
    assert parse_csv(b"") == []


def test_parse_csv_solo_header():
    assert parse_csv(b"fecha;concepto;debito;credito\n") == []


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

def test_monto_matches_exact():
    assert _monto_matches(500.0, Decimal("500.00"), Decimal("0")) is True


def test_monto_matches_tolera_rounding():
    assert _monto_matches(500.0, Decimal("500.01"), Decimal("0")) is True
    assert _monto_matches(500.0, Decimal("499.99"), Decimal("0")) is True


def test_monto_matches_rechaza_diferencia_significativa():
    assert _monto_matches(500.0, Decimal("501.00"), Decimal("0")) is False


def test_monto_matches_contra_credito():
    """Depósitos vienen como crédito — también debe matchear."""
    assert _monto_matches(500.0, Decimal("0"), Decimal("500.00")) is True


def test_referencia_matches_exacto():
    linea = BancoLinea(date(2026, 4, 17), "x", "1001", Decimal(0), Decimal(0), "test")
    assert _referencia_matches("1001", linea) is True
    assert _referencia_matches("1002", linea) is False


def test_referencia_matches_con_ceros_leading():
    linea = BancoLinea(date(2026, 4, 17), "x", "00001001", Decimal(0), Decimal(0), "test")
    assert _referencia_matches("1001", linea) is True


def test_referencia_matches_en_concepto():
    linea = BancoLinea(date(2026, 4, 17), "CHEQUE 1001 DEVUELTO", "", Decimal(0), Decimal(0), "test")
    assert _referencia_matches("1001", linea) is True


def test_referencia_matches_no_cheque_vacio_no_matchea():
    linea = BancoLinea(date(2026, 4, 17), "CHEQUE 1001 DEVUELTO", "1001", Decimal(0), Decimal(0), "test")
    assert _referencia_matches("", linea) is False


# ---------------------------------------------------------------------------
# matchear() — integración parser + matcher
# ---------------------------------------------------------------------------

def test_matchear_cheque_ok_aparece_en_matches():
    hoy = date(2026, 4, 17)
    lineas = [
        BancoLinea(hoy - timedelta(days=1), "Deposito", "1001", Decimal(0), Decimal("500.00"), "test"),
    ]
    cheques = [{
        "id_cheque": 1, "no_cheque": "1001", "importe": 500.0,
        "fechad": hoy - timedelta(days=1), "codigo_cli": "JTX",
    }]
    result = matchear(lineas, cheques, hoy=hoy)
    assert len(result.matches) == 1
    assert len(result.sospechosos) == 0


def test_matchear_cheque_sin_match_y_viejo_es_sospechoso():
    hoy = date(2026, 4, 17)
    lineas: list[BancoLinea] = []  # el banco no lo registró
    cheques = [{
        "id_cheque": 99, "no_cheque": "9999", "importe": 700.0,
        "fechad": hoy - timedelta(days=10), "codigo_cli": "MOD",
    }]
    result = matchear(lineas, cheques, dias_sospecha=3, hoy=hoy)
    assert len(result.sospechosos) == 1
    assert result.sospechosos[0]["no_cheque"] == "9999"
    assert result.sospechosos[0]["dias_sin_match"] == 10


def test_matchear_cheque_reciente_sin_match_no_es_sospechoso():
    """Cheque recién depositado, sin match todavía — es normal, no sospecha."""
    hoy = date(2026, 4, 17)
    cheques = [{
        "id_cheque": 99, "no_cheque": "9999", "importe": 700.0,
        "fechad": hoy, "codigo_cli": "MOD",
    }]
    result = matchear([], cheques, dias_sospecha=3, hoy=hoy)
    assert len(result.sospechosos) == 0
    assert len(result.matches) == 0


def test_matchear_cheque_sin_fecha_valida_no_es_sospechoso():
    cheques = [{
        "id_cheque": 99, "no_cheque": "9999", "importe": 700.0,
        "fechad": "2026-04-01", "codigo_cli": "MOD",
    }]
    result = matchear([], cheques, dias_sospecha=3, hoy=date(2026, 4, 17))
    assert result.matches == []
    assert result.sospechosos == []


def test_matchear_rebote_marca_es_debito_true():
    """Un match contra débito significa "cheque rebotado en el estado de cuenta"."""
    hoy = date(2026, 4, 17)
    lineas = [
        BancoLinea(hoy, "Cheque devuelto", "1001", Decimal("500.00"), Decimal(0), "test"),
    ]
    cheques = [{
        "id_cheque": 1, "no_cheque": "1001", "importe": 500.0,
        "fechad": hoy - timedelta(days=3), "codigo_cli": "JTX",
    }]
    result = matchear(lineas, cheques, hoy=hoy)
    assert len(result.matches) == 1
    assert result.matches[0]["es_debito"] is True


def test_matchear_cheque_con_importe_distinto_no_matchea():
    """Un cheque con referencia correcta pero monto distinto no matchea —
    podría ser otro cheque con el mismo número (re-uso de la serie)."""
    hoy = date(2026, 4, 17)
    lineas = [
        BancoLinea(hoy, "x", "1001", Decimal(0), Decimal("999.00"), "test"),
    ]
    cheques = [{
        "id_cheque": 1, "no_cheque": "1001", "importe": 500.0,
        "fechad": hoy - timedelta(days=5), "codigo_cli": "JTX",
    }]
    result = matchear(lineas, cheques, hoy=hoy)
    assert len(result.matches) == 0
    assert len(result.sospechosos) == 1


def test_matchear_sin_cheques_devuelve_vacio():
    result = matchear([], [])
    assert result.matches == []
    assert result.sospechosos == []
