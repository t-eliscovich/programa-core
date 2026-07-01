"""Tests del conversor de importe a letras (modules/bancos/letras.py)."""
from modules.bancos.letras import numero_a_letras


def _t(monto):
    return numero_a_letras(monto)["texto"]


def test_cero():
    assert _t(0) == "CERO 00/100 DOLARES"


def test_unidades_y_centavos():
    assert _t(1) == "UNO 00/100 DOLARES"
    assert _t(1.50) == "UNO 50/100 DOLARES"
    assert _t(15.05) == "QUINCE 05/100 DOLARES"


def test_decenas_compuestas():
    assert _t(21) == "VEINTIUNO 00/100 DOLARES"
    assert _t(31) == "TREINTA Y UNO 00/100 DOLARES"
    assert _t(99.99) == "NOVENTA Y NUEVE 99/100 DOLARES"


def test_cien_vs_ciento():
    assert _t(100) == "CIEN 00/100 DOLARES"
    assert _t(101) == "CIENTO UNO 00/100 DOLARES"
    assert _t(115) == "CIENTO QUINCE 00/100 DOLARES"


def test_miles_un():
    # "uno" -> "un" antes de mil (paridad IIF(CIF='uno','un') del PRG).
    assert _t(1000) == "MIL 00/100 DOLARES"
    assert _t(1001) == "MIL UNO 00/100 DOLARES"
    assert _t(21000) == "VEINTIUN MIL 00/100 DOLARES"
    assert _t(1230.50) == "MIL DOSCIENTOS TREINTA 50/100 DOLARES"


def test_millones():
    assert _t(1_000_000) == "UN MILLON 00/100 DOLARES"
    assert _t(2_000_000) == "DOS MILLONES 00/100 DOLARES"
    assert _t(1_234_567.89) == (
        "UN MILLON DOSCIENTOS TREINTA Y CUATRO MIL QUINIENTOS SESENTA Y SIETE 89/100 DOLARES"
    )


def test_redondeo_centavos():
    assert numero_a_letras(10.005)["centavos"] == "01/100"  # ROUND_HALF_UP
    assert numero_a_letras(10.004)["centavos"] == "00/100"


def test_formato_numeros_ecuador():
    assert numero_a_letras(1230.50)["en_numeros"] == "1.230,50"
    assert numero_a_letras(1_234_567.89)["en_numeros"] == "1.234.567,89"


def test_negativo():
    r = numero_a_letras(-50.25)
    assert r["texto"].startswith("MENOS ")
    assert r["en_numeros"] == "-50,25"
