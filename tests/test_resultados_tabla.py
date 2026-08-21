"""Tests de resultados_costos_tabla — la tabla RESULTADOS del balance.

Funcion pura: no necesita Postgres. Rediseno Federico 2026-05-21,
definido fila por fila con el dueno.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.informes.queries import resultados_costos_tabla


def _tabla(**over):
    base = dict(
        venta_kg=200000.0,
        venta_us=1000000.0,
        dia_actual=20,
        mp_ukg=2.911,
        v1=10000.0, v2=5000.0, v3=3000.0, dtj=2000.0,
        kg_tejidos=180000.0,
        v4=8000.0, v5=4000.0, v6=2000.0, dcc=1000.0,
        itin=120000.0, ktint=197000.0,
        v7=15000.0, v8=6000.0, v9=4000.0, deprcar=3000.0,
        patr=21000000.0, patant=20000000.0, uret=150000.0,
    )
    base.update(over)
    return resultados_costos_tabla(**base)


def _row(tabla, label):
    return next(r for r in tabla if r.get("label") == label)


def test_venta_precio_promedio():
    """Venta: u$/kg = u$s / kg."""
    v = _row(_tabla(), "Ventas")
    assert v["kg"] == 200000.0
    assert v["us"] == 1000000.0
    assert abs(v["ukg"] - 5.0) < 1e-9


def test_proyeccion_usa_meta_kgpro():
    """Proyección: meta del mes (KGPRO de Iniciales) × precio — igual que el dBase
    (INFORMES.PRG L4: PROYECCION = KGPRO × precio), NO regla de 3. TMT 2026-06-05."""
    p = _row(_tabla(kgpro=300000.0), "Proyección")
    assert abs(p["kg"] - 300000.0) < 1e-6      # = KGPRO (meta), no regla de 3
    assert abs(p["us"] - 1500000.0) < 1e-6     # KGPRO × precio (5.0)
    assert abs(p["ukg"] - 5.0) < 1e-9


def test_tejeduria_usa_compras_tipo_k_si_se_pasa():
    """Tejeduría: si se pasa tej_base_us (importe compras tipo K = VK del dBase),
    usa eso + dtj en vez de V1+V2+V3. INFORMES.PRG L241. TMT 2026-06-05."""
    t = _row(_tabla(tej_base_us=12000.0), "Tejeduría")
    assert abs(t["us"] - 14000.0) < 1e-6       # tej_base_us(12000) + dtj(2000)


def test_materia_prima_sobre_kg_vendidos():
    """Materia Prima: kg = kg VENDIDOS; $ = kg vendidos × tarifa hilado;
    u$/kg = tarifa. Federico 2026-07-27 (costeo de lo vendido, no el hilado
    consumido)."""
    mp = _row(_tabla(), "Materia Prima")
    assert abs(mp["kg"] - 200000.0) < 1e-6
    assert abs(mp["us"] - 200000.0 * 2.911) < 1e-6
    assert abs(mp["ukg"] - 2.911) < 1e-9


def test_tejeduria_costo_sobre_kg_tejidos():
    """Tejeduria: (V1+V2+V3+dtj) / kg tejidos."""
    t = _row(_tabla(), "Tejeduría")
    assert abs(t["us"] - 20000.0) < 1e-6       # 10000+5000+3000+2000
    assert abs(t["ukg"] - 20000.0 / 180000.0) < 1e-9


def test_tintoreria_y_colorantes_sobre_ktint():
    """Tintoreria y Colorantes dividen por KTINT (kg que salen)."""
    tab = _tabla()
    tin = _row(tab, "Tintorería")
    col = _row(tab, "Colorantes/Quím.")
    assert abs(tin["us"] - 15000.0) < 1e-6     # 8000+4000+2000+1000
    assert abs(tin["ukg"] - 15000.0 / 197000.0) < 1e-9
    assert abs(col["us"] - 120000.0) < 1e-6
    assert abs(col["ukg"] - 120000.0 / 197000.0) < 1e-9


def test_subtotal_45_solo_materiales():
    """Subtotal = Tejeduria + Tintoreria + merma*(MP + Colorantes). El +4.5%
    (merma) aplica SOLO a materiales (MP+Col), como el dBase (COSTUNI); la mano
    de obra de tejeduria/tintoreria NO lleva recargo. TMT 2026-07-12 (dueña)."""
    tab = _tabla(factor_desperdicio=1.045)
    mp = _row(tab, "Materia Prima")
    tej = _row(tab, "Tejeduría")
    tin = _row(tab, "Tintorería")
    col = _row(tab, "Colorantes/Quím.")
    sub = _row(tab, "Subtotal +4.5%")
    assert abs(sub["ukg"] - (tej["ukg"] + tin["ukg"] + 1.045 * (mp["ukg"] + col["ukg"]))) < 1e-9
    # Federico 2026-07-27: la fila ahora muestra también el importe ($): el +4.5%
    # aplica sobre los $ de materiales (MP+Col), Tejeduría/Tintorería a valor pleno.
    assert abs(sub["us"] - (1.045 * (mp["us"] + col["us"]) + tej["us"] + tin["us"])) < 1e-6
    assert sub["kg"] is None


def test_costo_total_subtotal_mas_admin():
    """Costo Total = Subtotal +4.5% + Administración, tanto en $/kg como en $.
    El +4.5% (merma) aplica sobre materiales (MP+Col) también en la columna $.
    Federico 2026-07-27 (antes el $ sumaba los renglones sin el recargo)."""
    tab = _tabla(factor_desperdicio=1.045)
    sub = _row(tab, "Subtotal +4.5%")
    adm = _row(tab, "Administración")
    mp = _row(tab, "Materia Prima")
    tej = _row(tab, "Tejeduría")
    tin = _row(tab, "Tintorería")
    col = _row(tab, "Colorantes/Quím.")
    ct = _row(tab, "Costo Total")
    assert abs(adm["ukg"] - 28000.0 / 200000.0) < 1e-9
    assert abs(ct["ukg"] - (sub["ukg"] + adm["ukg"])) < 1e-9
    assert abs(ct["us"] - (sub["us"] + adm["us"])) < 1e-6
    assert abs(ct["us"] - (1.045 * (mp["us"] + col["us"]) + tej["us"] + tin["us"] + adm["us"])) < 1e-6
    assert ct["kg"] is None


def test_la_utilidad_calculada_actual_ya_no_esta():
    """Federico 2026-08-20: "elimina estos datos" — la fila "Utilidad Calculada
    Actual" (Ventas − Costo Total, agregada el 2026-07-27) sale de la tabla.

    Se mira por etiqueta y no por conteo: una fila nueva más abajo no debe dar
    verde a esta prueba por casualidad.
    """
    labels = [r["label"] for r in _tabla(factor_desperdicio=1.045)]
    assert "Utilidad Calculada Actual" not in labels
    # las otras dos utilidades siguen, en orden Esperada → Real
    assert labels.index("Utilidad Esperada") < labels.index("Utilidad Real")


def test_utilidad_real_delta_patrimonio_mas_dividendos():
    """Utilidad Real u$s = (patr - patant) + dividendos del mes."""
    ur = _row(_tabla(), "Utilidad Real")
    assert abs(ur["us"] - 1150000.0) < 1e-6    # 1.000.000 + 150.000
    assert abs(ur["ukg"] - 1150000.0 / 200000.0) < 1e-9


def test_sin_ventas_no_rompe():
    """venta_kg = 0 no debe lanzar ZeroDivisionError."""
    tab = _tabla(venta_kg=0.0, venta_us=0.0)
    assert _row(tab, "Ventas")["ukg"] == 0.0
    # 12 filas: Federico 2026-08-20 sacó "Utilidad Calculada Actual" (la había
    # pedido el 2026-07-27, que las llevó de 12 a 13).
    assert len(tab) == 12


def test_seccion_costos_presente():
    """Hay una fila divisoria COSTOS de clase seccion."""
    assert _row(_tabla(), "COSTOS")["clase"] == "seccion"

def test_la_fila_se_llama_ventas_y_todo_lo_que_la_busca_por_ese_nombre():
    """Federico 2026-08-21: "debe decir Ventas (con s)".

    La etiqueta NO es solo texto: `views.py` la busca para colgarle el kg al
    ritmo actual, y `balance.html` la usa de clave en el mapa de drill-down y
    para el prefijo "kg." de la columna Proyecciones. Si se renombra en un solo
    lado, el link y el ritmo se caen sin que nadie se entere.
    """
    from pathlib import Path as _P
    labels = [r["label"] for r in _tabla()]
    assert "Ventas" in labels and "Venta" not in labels

    vistas = (ROOT / "modules/informes/views.py").read_text(encoding="utf-8")
    assert '_cell("Ventas", "kg")' in vistas
    assert '_r.get("label") == "Ventas"' in vistas

    tpl = (ROOT / "modules/informes/templates/informes/balance.html").read_text(encoding="utf-8")
    assert "'Ventas':" in tpl                      # mapa _row_links
    assert "row.label == 'Ventas'" in tpl          # prefijo "kg." en Proyecciones
