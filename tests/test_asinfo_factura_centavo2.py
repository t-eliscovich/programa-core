"""El pie cierra leyéndolo de arriba hacia abajo, con las cifras a la vista.

Las dos facturas que lo destaparon el 25/08, con sus números de verdad:
  · 182382 — 171,69 − 20,00 + 22,75 = 174,44
  · 182574 — 210,15 − 34,46 + 26,35 = 202,04 (y 202,04 es el importe que ya
    tenía cargado Programa Core)
"""
from __future__ import annotations

import pytest

from modules.asinfo import factura_lineas as fl


def _fila(bruto, descuento):
    return {"tela": "Jersey 3", "codigo": "MAR", "producto": "x",
            "categoria": "TELAS", "color": "MARINO", "calidad": "PRIMERA",
            "cantidad": 21.4, "precio": 9.82, "bruto": bruto,
            "descuento": descuento, "pct1": 5, "pct2": 12}


@pytest.mark.parametrize("bruto,descuento,esperado", [
    (171.6845, 19.9945, (171.68, 19.99, 151.69, 22.75, 174.44)),
    (210.1480, 34.4643, (210.15, 34.46, 175.69, 26.35, 202.04)),
])
def test_el_pie_cierra_con_las_cifras_que_se_ven(bruto, descuento, esperado):
    t = fl._agrupar([_fila(bruto, descuento)])["totales"]
    assert (t["bruto"], t["descuento"], t["neto"], t["iva"], t["total"]) == esperado
    # y la comprobación que hace el ojo, paso por paso
    assert round(t["bruto"] - t["descuento"], 2) == t["neto"]
    assert round(t["neto"] + t["iva"], 2) == t["total"]
