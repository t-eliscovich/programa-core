"""La columna del pie suma exactamente lo que se ve.

TMT 2026-08-25, mirando la 182382: la pantalla mostraba 171,69 − 20,00 + 22,75
y abajo decía 174,45. Un centavo, pero el que mira suma con el ojo y un total
que no cierra hace dudar de toda la tabla.
"""
from __future__ import annotations

from modules.asinfo import factura_lineas as fl


def _fila(kg, precio, neto):
    bruto = round(kg * precio, 4)
    return {"tela": "Jersey 3", "codigo": "MAR", "producto": "x",
            "categoria": "TELAS", "color": "MARINO", "calidad": "PRIMERA",
            "cantidad": kg, "precio": precio, "bruto": bruto,
            "descuento": round(bruto - neto, 4), "pct1": 5, "pct2": 7}


def test_bruto_menos_descuento_mas_iva_da_el_total():
    t = fl._agrupar([_fila(21.65, 7.93, 151.69)])["totales"]
    assert round(t["bruto"] - t["descuento"], 2) == t["neto"]
    assert round(t["neto"] + t["iva"], 2) == t["total"]


def test_el_iva_es_el_15_del_neto_que_se_muestra():
    t = fl._agrupar([_fila(10, 10.0, 90.0)])["totales"]
    assert t["neto"] == 90.0
    assert t["iva"] == 13.5
    assert t["total"] == 103.5


def test_la_cache_con_el_total_viejo_no_se_lee():
    from unittest.mock import patch

    with patch("db.fetch_one", return_value={"datos": {"estado": "ok", "formato": 2}}):
        assert fl._de_la_base("001-099-000182382") is None
