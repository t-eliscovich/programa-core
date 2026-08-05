"""La Factura Proforma cotiza CON IVA.

Dueña 2026-08-05: *"en la proforma está reflejando los valores sin IVA"* →
*"con IVA siempre"*. En `scintela.precios` / `scintela.precio_plano` los
precios están guardados NETOS (eso no cambia: la lista de precios en pantalla
se sigue editando neta), pero el formulario de la proforma es el papel que ve
el CLIENTE y tiene que hablar el mismo idioma que la hoja de precios que se le
entrega, que va con IVA 15%.

Las DOS fuentes de precio del formulario tienen que estar en la MISMA escala:
la matriz (clase x tela) y las telas de precio único. Si una sola de las dos
se olvidara del IVA, el mismo pedido cotizaría unas telas con IVA y otras sin,
y nadie lo vería mirando la pantalla.
"""
from __future__ import annotations

import pytest

from modules.precios import queries as precios_queries
from modules.proformas import queries as proformas_queries


@pytest.fixture()
def matriz_fake(monkeypatch):
    """Una sola clase con JERSEY = 7,93 NETO (el ejemplo real: con IVA da los
    9,12 del Excel de la dueña)."""
    fila = {"clase": 1, "descripcio": "BLANCO"}
    for col, _ in proformas_queries.TELAS:
        fila[col] = None
    fila["jersey"] = 7.93
    monkeypatch.setattr(
        proformas_queries.db, "fetch_all", lambda *a, **k: [fila], raising=True
    )
    monkeypatch.setattr(proformas_queries, "telas_planas", lambda: [], raising=True)


def test_precio_con_iva_es_el_neto_por_1_15():
    assert precios_queries.IVA_PCT == 15.0
    assert precios_queries.precio_con_iva(7.93) == pytest.approx(9.1195)
    assert precios_queries.precio_con_iva(None) is None


def test_matriz_de_la_proforma_sale_con_iva(matriz_fake):
    m = proformas_queries.matriz_precios()
    # 7,93 neto -> 9,12 con IVA: el número que la dueña tiene en su Excel.
    assert m["precios"]["1"]["jersey"] == pytest.approx(9.1195)
    assert round(m["precios"]["1"]["jersey"], 2) == 9.12
    assert m["iva_pct"] == 15.0


def test_columna_sin_precio_sigue_siendo_none(matriz_fake):
    m = proformas_queries.matriz_precios()
    assert m["precios"]["1"]["rib"] is None


def test_telas_de_precio_unico_tambien_salen_con_iva(monkeypatch):
    monkeypatch.setattr(
        precios_queries,
        "precio_plano",
        lambda: [
            {"id": 3, "tela": "SCUBA", "precio": 11.25, "ref_col": None},
            {"id": 1, "tela": "JERSEY 3,5", "precio": None, "ref_col": "jersey"},
        ],
        raising=True,
    )
    planos = {p["label"]: p for p in proformas_queries.telas_planas()}
    # Precio propio -> con IVA, en la misma escala que la matriz.
    assert planos["SCUBA"]["precio"] == pytest.approx(12.9375)
    # Las de ref_col NO llevan cifra propia: toman la de la matriz (que ya
    # viene con IVA). Si acá apareciera un número, sería IVA doble.
    assert planos["JERSEY 3,5"]["precio"] is None
    assert planos["JERSEY 3,5"]["col"] == "jersey"


def test_la_lista_de_precios_en_pantalla_sigue_NETA(matriz_fake):
    """El fix es SOLO de la proforma: /precios se edita y se muestra neto, y
    el IVA lo agrega la hoja al imprimir (factor_hoja). Si alguien "arregla"
    el IVA moviéndolo a la tabla, este test cae."""
    assert precios_queries.factor_hoja(0, con_iva=False) == 1.0
    assert precios_queries.factor_hoja(0, con_iva=True) == pytest.approx(1.15)


# ---------------------------------------------------------------------------
# La hoja impresa agrupa por TIPO y subagrupa por COLOR (dueña 2026-08-05:
# "ingresaron fleece abajo pero al momento de imprimir se podría agrupar por
# tipo?"). El armado es JS del lado del cliente, así que acá sólo se ata que
# las piezas sigan estando: el render se verifica corriendo el template en
# jsdom (ver skill precios-lista).
# ---------------------------------------------------------------------------
_NUEVA = "modules/proformas/templates/proformas/nueva.html"


def _plantilla() -> str:
    from pathlib import Path
    return Path(_NUEVA).read_text(encoding="utf-8")


def test_la_hoja_agrupa_por_tipo_y_color():
    t = _plantilla()
    assert "function agruparParaHoja" in t
    # El tipo dejó de ser una columna: ahora es la banda del grupo.
    assert "<th>Tipo / Color</th>" in t
    assert "tr class=\"gt\"" in t          # banda del tipo
    assert "Subtotal ${esc(g.tipo)}" in t  # cierre de cada bloque


def test_la_pantalla_NO_se_reordena_mientras_se_carga():
    """Alex tipea en el orden que quiere: si las filas se ordenaran solas, se
    le moverían bajo el cursor. `recalc` recorre el tbody tal cual está."""
    t = _plantilla()
    cuerpo = t.split("function recalc()")[1].split("function ")[0]
    assert "body.querySelectorAll('tr').forEach" in cuerpo
    assert "sort" not in cuerpo
    assert "agruparParaHoja" not in cuerpo
