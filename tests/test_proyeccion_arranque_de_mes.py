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


# ---------------------------------------------------------------------------
# Segunda vuelta — Andrés 2026-09-02: "sigue mal la esperada"
# ---------------------------------------------------------------------------
# El arreglo de arriba sólo cubría `col_ukg == 0`. Pero a principio de mes el
# $/kg de colorantes no da 0: da un número DEFORMADO, que es peor porque parece
# un dato. Es `consumo físico ÷ kg de terminado ingresados`, y esas dos
# magnitudes vienen de sistemas que no van al mismo ritmo — el consumo se
# registra al tinturar, el terminado entra a Asinfo después.
#
# Para proyectar se usa ITIN/KR: importe y kg de las MISMAS órdenes de tintura,
# en fase por construcción. Es la fórmula del dBase (INFORMES.PRG L419).


def test_para_proyectar_manda_itin_sobre_kr_no_el_ukg_de_la_fila():
    tab = _tabla(
        # Fila: 14.605 U$ de químico físico sobre 5.380 kg terminados = 2,715.
        col_us_fisico=14_605.0, ktint=5_380.0, ktint_colorantes=5_380.0,
        # Órdenes de tintura del mes: 12.000 U$ sobre 18.750 kg = 0,64.
        itin=12_000.0, kr_tinto=18_750.0,
        col_ukg_meta=0.70,
    )
    assert round(_row(tab, "Colorantes/Quím.")["ukg"], 3) == 2.715  # la fila no cambia
    p = _row(tab, "Proyección")
    assert round(p["costo_var_ukg"], 4) == round(2.92 + 0.64, 4)    # proyecta con ITIN/KR
    assert p["meta_costo"] is False


def test_sin_ordenes_de_tintura_todavia_cae_a_la_meta():
    tab = _tabla(col_us_fisico=14_605.0, ktint=5_380.0, ktint_colorantes=5_380.0,
                 itin=0.0, kr_tinto=0.0, col_ukg_meta=0.70)
    p = _row(tab, "Proyección")
    assert round(p["costo_var_ukg"], 4) == round(2.92 + 0.70, 4)
    assert p["meta_costo"] is True


def test_caller_sin_kr_conserva_el_comportamiento_viejo():
    """Compat: sin `kr_tinto` se sigue proyectando con el $/kg de la fila."""
    tab = _tabla(col_us_fisico=14_605.0, ktint=5_380.0, ktint_colorantes=5_380.0,
                 col_ukg_meta=0.70)
    assert round(_row(tab, "Proyección")["costo_var_ukg"], 3) == round(2.92 + 2.715, 3)


def test_regresion_produccion_02_09_2026():
    """Los números que Andrés vio en pantalla, con y sin el arreglo.

    Fila Colorantes 2,715 U$/kg (14.605 ÷ 5.380) contra una tarifa real de 0,64:
    más de 4×. Sobre los 320.000 kg de la meta eso metía ~660.000 U$ de costo
    inventado y dejaba la Utilidad Esperada en −24.364 en vez de ~+669.000.
    """
    proy_us, gastos, proy_kg, mp = 2_716_189.0, 815_000.0, 320_000.0, 3.044

    def utilidad(col_ukg_proy):
        return proy_us - gastos - proy_kg * (mp + col_ukg_proy) * 1.045

    # Lo que se veía en pantalla, con el $/kg deformado de la fila.
    assert round(utilidad(2.715)) == -24_621          # pantalla: -24.364 (redondeo)
    # Con el par en fase ITIN/KR.
    assert round(utilidad(0.64)) == 669_259
    # El arreglo tiene que mover la fila del rojo al verde, ~700k.
    assert utilidad(0.64) - utilidad(2.715) > 690_000

    # Y la tabla, armada con esos mismos insumos, proyecta con ITIN/KR.
    tab = _tabla(venta_kg=33_578.0, venta_us=285_014.0, mp_ukg=mp, kgpro=proy_kg,
                 col_us_fisico=14_605.0, ktint=5_380.0, ktint_colorantes=5_380.0,
                 itin=12_000.0, kr_tinto=18_750.0, col_ukg_meta=0.70)
    p = _row(tab, "Proyección")
    assert round(proy_us - gastos - p["kg"] * p["costo_var_ukg"] * 1.045) == 669_259
