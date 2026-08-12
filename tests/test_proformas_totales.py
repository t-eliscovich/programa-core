"""Tests de la cascada de totales de una proforma.

`calcular_totales` es una función PURA (no toca la DB): se testea en
aislamiento porque el sandbox no corre contra RDS.

⚠️ El orden es CONTADO primero y volumen después, que es el de la PANTALLA y
el del papel que recibe el cliente. Hasta el 2026-08-11 esta función lo hacía
al revés (volumen → contado, como PROCEDURE FACTURO del dBase) mientras el
formulario ya hacía contado → volumen. No se notaba porque la cascada es
conmutativa — el TOTAL es el mismo — pero el DESGLOSE no lo es, y desde que la
proforma se GUARDA el renglón grabado tiene que decir lo mismo que el papel.
`test_el_orden_no_cambia_el_total` deja esa conmutatividad escrita.
"""
from __future__ import annotations

from modules.proformas.queries import calcular_totales

LINEAS = [
    {"cantidad_kilos": 100, "precio_unitario": 9.12},   # 912.00
    {"cantidad_kilos": 50,  "precio_unitario": 10.00},  # 500.00
]  # subtotal = 1412.00


def test_sin_descuentos():
    t = calcular_totales(LINEAS, pct_volumen=0, aplica_contado=False)
    assert t["subtotal"] == 1412.00
    assert t["monto_descuento_volumen"] == 0
    assert t["subtotal_con_descuento"] == 1412.00
    assert t["monto_descuento_contado"] == 0
    assert t["total_final"] == 1412.00


def test_solo_volumen():
    t = calcular_totales(LINEAS, pct_volumen=10, aplica_contado=False)
    assert t["monto_descuento_volumen"] == 141.20
    assert t["subtotal_con_descuento"] == 1412.00
    assert t["total_final"] == 1270.80


def test_cascada_contado_y_volumen():
    # 5% contado → 1341.40; 10% volumen SOBRE 1341.40 = 134.14 → 1207.26
    t = calcular_totales(LINEAS, pct_volumen=10, aplica_contado=True, pct_contado=5)
    assert t["monto_descuento_contado"] == 70.60
    assert t["subtotal_con_descuento"] == 1341.40
    assert t["monto_descuento_volumen"] == 134.14
    assert t["total_final"] == 1207.26
    assert t["aplica_descuento_contado"] is True


def test_el_orden_no_cambia_el_total():
    """La cascada es conmutativa: 1412 × 0,95 × 0,90 = 1412 × 0,90 × 0,95.

    Por eso el bug del orden vivió meses sin que nadie lo viera. Lo que cambia
    es a QUÉ descuento se le atribuye cada peso.
    """
    t = calcular_totales(LINEAS, pct_volumen=10, aplica_contado=True, pct_contado=5)
    al_reves = round(1412.00 * 0.90 * 0.95, 2)
    assert t["total_final"] == al_reves
    # …pero el desglose NO es el mismo: acá el contado se calcula sobre el
    # subtotal entero (70,60) y no sobre el ya rebajado por volumen (63,54).
    assert t["monto_descuento_contado"] != 63.54


def test_contado_desmarcado_no_resta():
    t = calcular_totales(LINEAS, pct_volumen=0, aplica_contado=False, pct_contado=5)
    assert t["monto_descuento_contado"] == 0
    assert t["total_final"] == 1412.00


def test_flete_se_suma_al_final():
    """El flete (sólo Factura Proforma) va DESPUÉS de los descuentos: no se
    descuenta el transporte."""
    sin = calcular_totales(LINEAS, pct_volumen=10, aplica_contado=True, flete=0)
    con = calcular_totales(LINEAS, pct_volumen=10, aplica_contado=True, flete=25)
    assert con["flete"] == 25.00
    assert con["total_final"] == round(sin["total_final"] + 25, 2)


def test_lineas_vacias_dan_cero():
    t = calcular_totales([], pct_volumen=10, aplica_contado=True)
    assert t["subtotal"] == 0
    assert t["total_final"] == 0
