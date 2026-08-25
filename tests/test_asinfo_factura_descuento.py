"""El renglón del descuento siempre dice un porcentaje.

TMT 2026-08-25 (dueña), mirando una factura con flete: *"poner % de
descuento"*. El renglón decía "Descuento" y el importe, sin el porcentaje.
"""
from __future__ import annotations

from modules.asinfo import factura_lineas as fl


def _fila(kg, precio, neto, pct1=5, pct2=12, categoria="TELAS"):
    bruto = round(kg * precio, 4)
    return {"numero": "001-099-000182419", "tela": "Jersey 3.5", "codigo": "VIN",
            "producto": "Jersey 3.5 VINO", "categoria": categoria, "color": "VINO",
            "calidad": "PRIMERA", "cantidad": kg, "precio": precio, "bruto": bruto,
            "descuento": round(bruto - neto, 4), "pct1": pct1, "pct2": pct2}


def test_el_flete_ya_no_calla_los_dos_tramos():
    """El flete entra como un renglón más y NO lleva el descuento del cliente.

    Con él adentro de la cuenta había dos pares distintos, la regla se callaba
    y el renglón quedaba sin porcentaje. Es la factura de la captura.
    """
    filas = [
        _fila(64.70, 9.82, 531.16),
        _fila(4.05, 10.51, 35.58),
        _fila(1, 4.70, 4.70, pct1=0, pct2=0, categoria="SERVICIOS"),
    ]
    t = fl._agrupar(filas)["totales"]
    assert t["pct1"] == 5
    assert t["pct2"] == 12


def test_con_dos_escalones_distintos_se_dice_el_que_dio():
    """Sin un par que nombrar, el porcentaje efectivo siempre se puede decir."""
    filas = [_fila(10, 10.0, 90.0, pct2=10), _fila(10, 10.0, 80.0, pct2=20)]
    t = fl._agrupar(filas)["totales"]
    assert t["pct1"] is None and t["pct2"] is None
    assert t["pct_efectivo"] == 15.0     # 30 de descuento sobre 200 de bruto


def test_el_efectivo_no_se_rompe_con_una_factura_en_cero():
    assert fl._agrupar([])["totales"]["pct_efectivo"] == 0.0


def test_la_cache_vieja_no_se_lee():
    """Al cambiar la forma de `datos`, las filas viejas se ignoran solas."""
    from unittest.mock import patch

    viejo = {"estado": "ok", "formato": 1, "totales": {}}
    with patch("db.fetch_one", return_value={"datos": viejo}):
        assert fl._de_la_base("001-099-000182419") is None

    nuevo = dict(viejo, formato=fl.FORMATO)
    with patch("db.fetch_one", return_value={"datos": nuevo}):
        assert fl._de_la_base("001-099-000182419") == nuevo
