"""Tests de la cascada de totales de una cotización (proformas).

`calcular_totales` es una función PURA (no toca la DB) — réplica de
PROCEDURE FACTURO del dBase: descuento por volumen y después por contado, EN
CASCADA (el contado se calcula sobre el subtotal YA rebajado por volumen).

Se testea acá porque el sandbox no corre contra RDS; la matemática se verifica
en aislamiento. La cotización es solo para cotizar (no factura, no stock).
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
    assert t["subtotal_con_descuento"] == 1270.80
    assert t["total_final"] == 1270.80


def test_cascada_volumen_y_contado():
    # 10% volumen → 1270.80; 5% contado SOBRE 1270.80 = 63.54 → 1207.26
    t = calcular_totales(LINEAS, pct_volumen=10, aplica_contado=True, pct_contado=5)
    assert t["subtotal_con_descuento"] == 1270.80
    assert t["monto_descuento_contado"] == 63.54
    assert t["total_final"] == 1207.26
    assert t["aplica_descuento_contado"] is True


def test_contado_desmarcado_no_resta():
    # aplica_contado=False → el % de contado se ignora aunque venga cargado.
    t = calcular_totales(LINEAS, pct_volumen=0, aplica_contado=False, pct_contado=5)
    assert t["monto_descuento_contado"] == 0
    assert t["total_final"] == 1412.00


def test_lineas_vacias_dan_cero():
    t = calcular_totales([], pct_volumen=10, aplica_contado=True)
    assert t["subtotal"] == 0
    assert t["total_final"] == 0
