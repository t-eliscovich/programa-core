"""El "% usado" del cupo se calcula sobre el MISMO total que se muestra.

TMT 2026-08-03. Dueña, sobre un cliente con TOTAL (FACTURAS + CHEQUES)
3.159,06 y CUPO $ 3.000 que mostraba "41 % usado": *"acá el porcentaje está
mal, debería ser más del 100%: es 3159/3000, se entiende?"*.

Y sí: el % dividía por `t.saldo_vivo` (sólo las facturas vivas) mientras el
tile de al lado, pegado, mostraba `_saldo_neto + cheques_cartera`. Dos números
distintos uno al lado del otro. Peor que un error de cuenta: el % siempre
salía MENOR que la exposición real, porque los cheques en cartera todavía no
se cobraron y por lo tanto ocupan cupo. Un cliente pasado de cupo podía verse
en verde.

Test de PLANTILLA (no toca Postgres): el cálculo vive en el Jinja.
"""
from __future__ import annotations

import re
from pathlib import Path

_TPL = (
    Path(__file__).resolve().parents[1]
    / "modules" / "informes" / "templates" / "informes" / "estado_cuenta.html"
)
HTML = _TPL.read_text(encoding="utf-8")


def _linea_pct() -> str:
    m = re.search(r"\{%\s*set pct = .*?%\}", HTML)
    assert m, "no encontré el cálculo del % del cupo"
    return m.group(0)


def test_el_pct_usa_el_total_que_se_muestra_arriba():
    linea = _linea_pct()
    assert "_total_fc" in linea, (
        f"el % del cupo tiene que dividir el MISMO total que muestra el tile "
        f"'Total (facturas + cheques)'; hoy dice: {linea}"
    )
    assert "saldo_vivo" not in linea, (
        "saldo_vivo son sólo las facturas: deja afuera los cheques en cartera "
        "y subestima el uso del cupo"
    )


def test_el_total_es_facturas_mas_cheques_y_se_define_antes():
    """`_total_fc` tiene que existir ANTES del tile del cupo, si no Jinja lo
    resuelve como Undefined y el % sale 0 sin romper nada (silencioso)."""
    i_def = HTML.index("{% set _total_fc")
    i_uso = HTML.index("set pct =")
    assert i_def < i_uso, "_total_fc se usa antes de definirse"
    definicion = HTML[i_def:HTML.index("%}", i_def)]
    assert "_saldo_neto" in definicion and "cheques_cartera" in definicion


def test_pasado_de_cupo_se_ve_en_rojo():
    """El umbral es lo único que avisa a simple vista."""
    linea = re.search(r"pct >= 100.*?pct >= 80", HTML, re.S)
    assert linea, "se perdieron los umbrales de color del % de cupo"
    assert "text-red-600" in HTML and "text-amber-600" in HTML


def test_la_cuenta_de_la_dueña_da_mas_de_100():
    """3.159,06 / 3.000 = 105,3 % — el caso exacto del reporte."""
    assert round(3159.06 / 3000 * 100) == 105
