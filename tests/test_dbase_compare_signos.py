"""El 1 a 1 de banco aparea por IMPACTO FIRMADO, no por |importe|.

Pedido dueña 2026-07-31: "me parece que el problema es de signo... igualate al
dBase en signos". El dBase deshace un movimiento repitiendo la MISMA fila con
importe NEGATIVO; PC lo deshace con un documento del tipo OPUESTO en positivo.
Las dos formas mueven la misma plata y tienen que cancelarse.
"""
from datetime import date

from modules.admin_dbase.dbase_compare_view import (
    diff_movs_banco,
    lineas_cuadre_diff,
    lineas_top_gap,
    val_firmado,
)

DESDE = date(2026, 7, 1)


def _m(fecha, doc, importe, concepto):
    return {"fecha": fecha, "doc": doc, "importe": importe, "concepto": concepto}


def test_dbase_negativo_y_pc_tipo_opuesto_se_cancelan():
    """`DE −611,80` (dBase) y `ND +611,80` (PC) son el MISMO deshacer."""
    dbf = [_m(date(2026, 7, 20), "DE", -611.80, "1 ch.MMM")]
    pc = [_m(date(2026, 7, 20), "ND", 611.80, "ANUL ch100767 err carga")]
    d = diff_movs_banco(dbf, pc, DESDE)
    assert d["solo_dbase"] == []
    assert d["solo_pc"] == []
    assert d["signo_invertido"] == []


def test_el_par_del_dbase_netea_y_queda_lo_que_PC_no_deshizo():
    """El dBase carga el depósito y lo deshace; PC lo dejó vivo.

    Lo que tiene que quedar es UNA sola fila del lado del dBase (el +2.000 que
    PC nunca deshizo), no un 'signo invertido' con impacto inventado de 4.000.
    """
    dbf = [
        _m(date(2026, 7, 14), "DE", 2000.00, "1 ch.BMY"),
        _m(date(2026, 7, 14), "DE", -2000.00, "1 ch.BMY"),
    ]
    pc = [_m(date(2026, 7, 14), "DE", 2000.00, "1 ch.BMY")]
    d = diff_movs_banco(dbf, pc, DESDE)
    assert d["solo_pc"] == []
    assert len(d["solo_dbase"]) == 1
    assert val_firmado(d["solo_dbase"][0]) == -2000.00


def test_val_firmado_respeta_el_negativo_del_dbase():
    assert val_firmado(_m(date(2026, 7, 1), "DE", -330.0, "x")) == -330.0
    assert val_firmado(_m(date(2026, 7, 1), "ND", 330.0, "x")) == -330.0
    assert val_firmado(_m(date(2026, 7, 1), "ND", -330.0, "x")) == 330.0
    assert val_firmado(_m(date(2026, 7, 1), "DE", 330.0, "x")) == 330.0


def test_pase_2_sigue_cancelando_por_fecha_corrida():
    """Mismo impacto y mismo concepto en días distintos = timing, no gap."""
    dbf = [_m(date(2026, 7, 17), "DE", 12.88, "1 ch.FPM")]
    pc = [_m(date(2026, 7, 20), "DE", 12.88, "1 ch.FPM")]
    d = diff_movs_banco(dbf, pc, DESDE)
    assert d["solo_dbase"] == []
    assert d["solo_pc"] == []


def test_cuadre_explica_el_gap_sin_bucket_de_signos():
    dbf = [_m(date(2026, 7, 10), "DE", 1000.00, "1 ch.AAA")]
    pc = [_m(date(2026, 7, 10), "DE", 1500.00, "1 ch.BBB")]
    d = diff_movs_banco(dbf, pc, DESDE)
    dias = [
        {"fecha": date(2026, 7, 1), "delta": 0.0},
        {"fecha": date(2026, 7, 10), "delta": 500.0},
    ]
    txt = "".join(lineas_cuadre_diff(d, dias, "BANCO"))
    assert "signo invertido" not in txt
    assert "RESIDUO SIN EXPLICAR:        +0.00" in txt


def test_top_gap_ordena_por_impacto_y_agrupa_por_concepto():
    d = {
        "solo_pc": [
            _m(date(2026, 7, 10), "DE", 100.0, "1 ch.CHICO"),
            _m(date(2026, 7, 11), "DE", 50000.0, "1 ch.GRANDE"),
        ],
        "solo_dbase": [_m(date(2026, 7, 12), "DE", 300.0, "1 ch.CHICO")],
    }
    lineas = list(lineas_top_gap(d, "BANCO"))
    cuerpo = "".join(lineas)
    assert "QUIÉN SE COME EL GAP DE BANCO" in cuerpo
    assert lineas[1].index("1 ch.GRANDE") > 0        # el más grande, primero
    assert "-200.00" in lineas[2]                    # 100 (PC) − 300 (dBase)


def test_top_gap_no_imprime_nada_si_no_hay_sobrantes():
    assert list(lineas_top_gap({"solo_pc": [], "solo_dbase": []}, "CAJA")) == []
