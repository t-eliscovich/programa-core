"""AC 83A — 21/09/2026.

La Nota de IM-0000663 en Asinfo decía "ACMT/EXP/2026-27/8586 AC 83A)": sin el
paréntesis de apertura y con una letra pegada al número (el AC 83 ya existía
en la campaña 2025-26 y el proveedor le puso la A para distinguirlo). El parser
la daba por "sin código" y el anticipo no encontraba la importación.

Regla: la letra se tolera y NO cambia el código (sigue siendo AC 83; el año de
la campaña es lo que separa las dos), en la Nota, en el concepto del anticipo
y en el SQL que saca el número del concepto.
"""
import inspect

from concepto_parser import (
    anio_importacion,
    parse_nota_importacion,
    parse_ref_anticipo,
)


def test_nota_sin_parentesis_y_con_letra_parsea_ac_83():
    code = parse_nota_importacion("ACMT/EXP/2026-27/8586 AC 83A)")
    assert code["prov"] == "AC"
    assert code["numero"] == 83
    assert code["codigo"] == "AC 83"
    assert code["sufijo"] == "A"
    assert code["numero_hasta"] is None
    assert anio_importacion(code["raw"])["anio"] == 2026


def test_la_letra_tambien_entre_parentesis_y_no_rompe_el_rango():
    assert parse_nota_importacion("X ( AC 83A )")["codigo"] == "AC 83"
    assert parse_nota_importacion("X ( AC 83A )")["sufijo"] == "A"
    rango = parse_nota_importacion("INVHY5464-26-2 ( MH 74-75 )")
    assert (rango["codigo"], rango["sufijo"]) == ("MH 74-75", None)
    assert parse_nota_importacion("AYF02823 ( AI 46 )")["sufijo"] is None


def test_el_homonimo_viejo_queda_en_su_campana():
    viejo = parse_nota_importacion("ACMT/EXP/2025-26/7500 (AC 83)")
    assert viejo["codigo"] == "AC 83" and viejo["sufijo"] is None
    assert anio_importacion(viejo["raw"])["anio"] == 2025


def test_concepto_del_anticipo_tolera_la_letra():
    assert parse_ref_anticipo("83A") == {"numero": 83, "anio": None}
    assert parse_ref_anticipo("83A/26") == {"numero": 83, "anio": 2026}
    # lo de siempre no se mueve
    assert parse_ref_anticipo("58/26 SALDO") == {"numero": 58, "anio": 2026}
    assert parse_ref_anticipo("31 SALDO")["numero"] == 31
    assert parse_ref_anticipo("AC 95")["numero"] == 95
    # un número pegado a letras adelante NO es el nº de importación
    assert parse_ref_anticipo("INV2026 x 12")["numero"] == 12


def test_el_sql_saca_el_numero_aunque_lleve_letra():
    """Sin Postgres en CI: el patrón se verifica en el fuente (mismo regex,
    probado contra Postgres 16 el 21/09/2026: '83A'→83, '83A/26'→83,
    '31 SALDO'→31, 'AC 95'→95, 'INV2026 x 12'→12)."""
    from modules.dolares import anio
    from modules.importaciones import service

    patron = r"(?:^|[^0-9A-Za-z])0*(\d{1,6})(?![0-9])"
    for mod in (service, anio):
        src = inspect.getsource(mod)
        assert patron in src, mod.__name__
        assert r"'\y(\d{1,6})\y'" not in src, mod.__name__
