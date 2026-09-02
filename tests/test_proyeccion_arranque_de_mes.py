"""Andrés 2026-09-02 — la Proyección / Utilidad Esperada los primeros días del mes.

Las tarifas del cuadro RESULTADOS son del MES EN CURSO: el precio es
`ventas U$ / ventas kg` y el $/kg de colorantes es `consumo químico / kg
terminados`. El día 1, antes de la primera factura y del primer ingreso de
tintorería, las dos valen 0 — y con ellas se armaba la PROYECCIÓN:

  * precio 0  → Proyección U$ = 0 → Utilidad Esperada = −(gastos + costo directo),
    varios millones en rojo.
  * colorantes 0 → costo directo corto → Utilidad Esperada de más.

`informe_balance` ya resolvía esto para su propio `proy_uvent` con `_eff_rate`
(live, y si es 0 la tarifa meta de `scintela.iniciales`); la TABLA se había
quedado con las tarifas crudas. Ahora las proyecciones usan la tarifa efectiva y
las filas de datos siguen mostrando lo real.
"""
from __future__ import annotations

from modules.informes import queries


def _tabla(**kw):
    base = dict(
        venta_kg=0.0, venta_us=0.0, dia_actual=1, mp_ukg=2.92,
        v1=0.0, v2=0.0, v3=0.0, dtj=0.0, kg_tejidos=0.0,
        v4=0.0, v5=0.0, v6=0.0, dcc=0.0, itin=0.0, ktint=0.0,
        v7=0.0, v8=0.0, v9=0.0, deprcar=0.0,
        patr=0.0, patant=0.0, uret=0.0, kgpro=320_000.0,
    )
    base.update(kw)
    return queries.resultados_costos_tabla(**base)


def _row(tabla, label):
    return next(f for f in tabla if f.get("label") == label)


def test_sin_ventas_todavia_la_proyeccion_usa_el_precio_meta():
    """El caso del día 1: ni una factura cargada."""
    p = _row(_tabla(precio_meta=8.57), "Proyección")
    assert p["ukg"] == 8.57
    assert p["us"] == 320_000.0 * 8.57
    assert p["meta_precio"] is True
    assert "precio objetivo de Iniciales" in p["ayuda"]


def test_la_fila_ventas_sigue_diciendo_la_verdad():
    """El fallback es SÓLO para proyectar: sin ventas, Ventas muestra 0."""
    tab = _tabla(precio_meta=8.57)
    v = _row(tab, "Ventas")
    assert v["kg"] == 0.0 and v["us"] == 0.0 and v["ukg"] == 0.0


def test_con_ventas_del_mes_manda_el_precio_live():
    tab = _tabla(venta_kg=40_000.0, venta_us=324_000.0, precio_meta=8.57)
    p = _row(tab, "Proyección")
    assert p["ukg"] == 8.10
    assert p["us"] == 320_000.0 * 8.10
    assert p["meta_precio"] is False
    assert "precio objetivo" not in p["ayuda"]


def test_sin_tintura_todavia_el_costo_de_proyectar_usa_la_meta_de_colorantes():
    tab = _tabla(precio_meta=8.57, col_ukg_meta=0.64)
    col = _row(tab, "Colorantes/Quím.")
    p = _row(tab, "Proyección")
    # La fila sigue mostrando el live (0: no se tinturó nada todavía)…
    assert col["ukg"] == 0.0
    # …pero el costo con el que se proyecta lleva la meta.
    assert p["costo_var_ukg"] == 2.92 + 0.64
    assert p["meta_costo"] is True


def test_con_tintura_del_mes_manda_el_colorante_live():
    tab = _tabla(itin=25_600.0, ktint=40_000.0, ktint_colorantes=40_000.0,
                 precio_meta=8.57, col_ukg_meta=0.99)
    p = _row(tab, "Proyección")
    assert _row(tab, "Colorantes/Quím.")["ukg"] == 0.64
    assert round(p["costo_var_ukg"], 4) == round(2.92 + 0.64, 4)
    assert p["meta_costo"] is False


def test_sin_metas_cargadas_el_comportamiento_es_el_de_antes():
    """Callers viejos (y meses sin Iniciales) no cambian de resultado."""
    tab = _tabla(venta_kg=40_000.0, venta_us=324_000.0)
    p = _row(tab, "Proyección")
    assert p["ukg"] == 8.10
    assert p["costo_var_ukg"] == 2.92
    assert p["meta_precio"] is False and p["meta_costo"] is False
