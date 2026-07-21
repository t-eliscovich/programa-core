"""Banda QUÍM del flujo TODO-formulas (dueña 2026-07-21): la tintorería/
químicos del mes sale entera de formulas_app y la banda cierra a cero con
filas nombradas — Inicial (físico fin mes anterior) + Entradas bodega ±
Ajustes inventario − Consumo (órdenes TERMINADAS, fecha_terminado) = Físico
hoy. Todo a precio de catálogo × factor IVA (sal num=12 exenta).

Unit tests de las 4 funciones nuevas de modules/informes/quimicos_flujo
(mock de formulas_db y de tintura.service — sin DB real, patrón
test_dolares_rework) + integración _build_mov_asinfo / _chequeo_coherencia
(modelo formulas y fallback al modelo A viejo).
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

from modules.informes import quimicos_flujo as qf
from modules.informes.views import _build_mov_asinfo, _chequeo_coherencia
from modules.tintura.service import StockProductoAlDia


def _item(num, familia, stock_kg, precio_us):
    return StockProductoAlDia(
        num=num, num_visible=num, familia=familia, nombre=f"P{num}",
        unidad="kg", precio_us=precio_us, lectura_kg=stock_kg,
        fecha_lectura=None, ajustes_kg=0.0, compras_kg=0.0, consumo_kg=0.0,
        stock_al_dia_kg=stock_kg,
    )


# ── fisico_total_al_dia ──────────────────────────────────────────────────────
def test_fisico_total_valua_poli_alg_aux_con_iva_y_sal_exenta():
    """TODO el químico (POLI+ALG+AUX) × precio catálogo × IVA; la sal (num=12)
    va al 1.0 y las familias ajenas quedan afuera."""
    items = [
        _item(1, "poli", 100.0, 2.0),    # 100×2×1.15 = 230
        _item(2, "ALG", 50.0, 1.0),      # 50×1×1.15 = 57.5
        _item(12, "aux", 200.0, 0.05),   # sal: 200×0.05×1.0 = 10
        _item(9, "OTRA", 999.0, 9.0),    # familia ajena → no suma
    ]
    with patch("modules.tintura.service.stock_quimicos_al_dia",
               return_value=items) as m:
        v = qf.fisico_total_al_dia(date(2026, 6, 30))
    assert m.call_args[0][0] == date(2026, 6, 30)
    assert round(v, 2) == 297.5


def test_fisico_total_sin_datos_devuelve_none():
    """[] (bridge apagado / falló) → None, fail-soft."""
    with patch("modules.tintura.service.stock_quimicos_al_dia", return_value=[]):
        assert qf.fisico_total_al_dia(date(2026, 6, 30)) is None


def test_fisico_total_excepcion_devuelve_none():
    with patch("modules.tintura.service.stock_quimicos_al_dia",
               side_effect=RuntimeError("boom")):
        assert qf.fisico_total_al_dia(date(2026, 6, 30)) is None


# ── entradas_bodega_mes ──────────────────────────────────────────────────────
def test_entradas_bodega_mes_suma_compras_del_mes():
    with patch("modules._lib.formulas_db.disponible", return_value=True), \
         patch("modules._lib.formulas_db.fetch_one",
               return_value={"us": 80467.5, "n": 93}) as m:
        out = qf.entradas_bodega_mes(2026, 7)
    assert out == {"us": 80467.5, "n": 93}
    sql, params = m.call_args[0]
    assert "FROM compras" in sql
    assert "COALESCE(NULLIF(c.precio_us, 0), p.us, 0)" in sql
    assert params == {"d1": "2026-07-01", "d2": "2026-07-31"}


def test_entradas_bodega_mes_fail_soft():
    with patch("modules._lib.formulas_db.disponible", return_value=False):
        assert qf.entradas_bodega_mes(2026, 7) is None
    with patch("modules._lib.formulas_db.disponible", return_value=True), \
         patch("modules._lib.formulas_db.fetch_one", return_value=None):
        assert qf.entradas_bodega_mes(2026, 7) is None


# ── ajustes_inventario_mes ───────────────────────────────────────────────────
def test_ajustes_inventario_mes_neto_y_detalle_por_motivo():
    rows = [
        {"motivo": "CORRECCION", "n": 30, "us": 3782.71},
        {"motivo": "AJUSTE INVENTARIO", "n": 101, "us": -2424.04},
        {"motivo": "(sin motivo)", "n": 1, "us": 33.53},
    ]
    with patch("modules._lib.formulas_db.disponible", return_value=True), \
         patch("modules._lib.formulas_db.fetch_all", return_value=rows) as m:
        out = qf.ajustes_inventario_mes(2026, 7)
    assert round(out["us"], 2) == 1392.2       # neto ± a precio catálogo
    assert out["n"] == 132
    assert [d["motivo"] for d in out["detalle"]] == [
        "CORRECCION", "AJUSTE INVENTARIO", "(sin motivo)"]
    sql, params = m.call_args[0]
    assert "FROM inventario_ajustes" in sql
    assert params == {"d1": "2026-07-01", "d2": "2026-07-31"}


def test_ajustes_inventario_mes_sin_ajustes_es_cero():
    """Mes sin ajustes (bridge vivo, 0 filas) = cero legítimo, NO None."""
    with patch("modules._lib.formulas_db.disponible", return_value=True), \
         patch("modules._lib.formulas_db.fetch_all", return_value=[]):
        out = qf.ajustes_inventario_mes(2026, 7)
    assert out == {"us": 0, "n": 0, "detalle": []}


def test_ajustes_inventario_mes_sin_bridge_devuelve_none():
    with patch("modules._lib.formulas_db.disponible", return_value=False):
        assert qf.ajustes_inventario_mes(2026, 7) is None


# ── consumo_terminadas_mes ───────────────────────────────────────────────────
def test_consumo_terminadas_mes_por_fecha_terminado():
    """Criterio contable dueña 2026-07-21: el químico se descuenta AL TERMINAR
    la orden (fecha_terminado ISO), igual que los kg y el costo."""
    with patch("modules._lib.formulas_db.disponible", return_value=True), \
         patch("modules._lib.formulas_db.fetch_one",
               return_value={"us": 147616.0}) as m:
        out = qf.consumo_terminadas_mes(2026, 7)
    assert out == {"us": 147616.0}
    sql, params = m.call_args[0]
    assert "fecha_terminado" in sql
    assert "'POLI', 'ALG', 'AUX'" in sql
    assert params == {"d1": "2026-07-01", "d2": "2026-07-31"}


def test_consumo_terminadas_mes_fail_soft():
    with patch("modules._lib.formulas_db.disponible", return_value=False):
        assert qf.consumo_terminadas_mes(2026, 7) is None
    with patch("modules._lib.formulas_db.disponible", return_value=True), \
         patch("modules._lib.formulas_db.fetch_one", return_value=None):
        assert qf.consumo_terminadas_mes(2026, 7) is None


# ── _build_mov_asinfo: modelo banda formulas + fallback ─────────────────────
def _data():
    return {"header": {"hilado": {}, "tejido": {},
                       "terminado": {}, "colorantes": {"stock_inic_us": 144637.0}}}


def _inv(disp=True):
    return {
        "disponible": disp, "hilo": 100000.0, "tela_cruda": 50000.0,
        "terminada": 333315.0, "en_proceso_tc": 0.0, "en_proceso_pt": 0.0,
    }


def _patch_asinfo():
    return [
        patch("modules.asinfo.service.hilado_recibido_mes", return_value=0.0),
        patch("modules.asinfo.service.fabricacion_flujo_mes",
              return_value={"issued": 0.0, "fab": 0.0}),
        patch("modules.asinfo.service.movimiento_bodega_mes",
              return_value={"ingreso": 0.0, "egreso": 0.0}),
        patch("modules.asinfo.service.despacho_fisico_mes", return_value=0.0),
        patch("modules.asinfo.service.ventas_facturado_kg", return_value=0.0),
        patch("modules.importaciones.service.promedio_hilado_usd_kg",
              return_value=3.0),
        patch("modules.importaciones.service.costo_hilado_recibido_mes",
              return_value={"us": 0.0, "kg": 0.0, "usd_kg": None}),
    ]


def test_mov_asinfo_banda_formulas_cierra_con_5_filas():
    """Con los 5 términos de formulas: modelo 'formulas', libro = inicial +
    entradas + ajustes − consumo, físico aparte, sin fila de máquinas ni
    ajuste de arranque en la columna QUÍM.$."""
    ctxs = _patch_asinfo()
    with ctxs[0], ctxs[1], ctxs[2], ctxs[3], ctxs[4], ctxs[5], ctxs[6], \
         patch("db.fetch_one", return_value={"importe": 152776.0, "n": 4}), \
         patch("modules.informes.quimicos_flujo.fisico_total_al_dia",
               side_effect=lambda c: 437500.0 if c == date(2026, 6, 30) else 363180.0), \
         patch("modules.informes.quimicos_flujo.entradas_bodega_mes",
               return_value={"us": 80467.5, "n": 93}), \
         patch("modules.informes.quimicos_flujo.ajustes_inventario_mes",
               return_value={"us": 1392.2, "n": 132,
                             "detalle": [{"motivo": "CORRECCION", "n": 30, "us": 3782.71}]}), \
         patch("modules.informes.quimicos_flujo.consumo_terminadas_mes",
               return_value={"us": 147616.0}):
        mov = _build_mov_asinfo(_data(), _inv(), _inv(), anio=2026, mes=7,
                                proy_quimico=90000.0)
    qm = mov["quimicos_modelo"]
    assert qm["modelo"] == "formulas"
    assert qm["inicial"] == 437500.0
    assert qm["compras"] == 80467.5 and qm["compras_n"] == 93
    assert round(qm["ajustes_inv"], 2) == 1392.2
    assert qm["egresos"] == 147616.0              # NO el proy_quimico (90k)
    assert round(qm["final_prog"], 2) == round(
        437500.0 + 80467.5 + 1392.2 - 147616.0, 2)
    assert qm["final_form"] == 363180.0
    assert round(qm["ajuste"], 2) == round(363180.0 - qm["final_prog"], 2)
    assert qm["facturado_prog"] == 152776.0 and qm["facturado_n"] == 4
    assert mov["quimicos_banda"] == qm
    # Columna QUÍM.$: fila Ajustes inventario, sin arranque ni máquinas.
    co = mov["colorantes"]
    assert co["stock_inic_us"] == 437500.0
    assert co["ingresos_us"] == 80468.0            # round(...)
    assert co["egresos_us"] == 147616.0
    assert co["ajustes_inv_us"] == 1392.0
    assert "CORRECCION" in co["ajustes_inv_title"]
    assert co["stock_act_us"] == 363180.0
    assert "ajuste_us" not in co                   # "Ajuste de arranque" muere
    assert "maquinas_us" not in co                 # "En máquinas" QUÍM → "—"


def test_mov_asinfo_fallback_modelo_viejo_si_falta_un_termino():
    """Si CUALQUIER término formulas viene None → modelo A anterior completo
    (inicial VQ0, compras tipo Q, consumo proyectado, en máquinas, ajuste de
    arranque)."""
    ctxs = _patch_asinfo()
    with ctxs[0], ctxs[1], ctxs[2], ctxs[3], ctxs[4], ctxs[5], ctxs[6], \
         patch("db.fetch_one", return_value={"importe": 50000.0, "n": 3}), \
         patch("modules.informes.quimicos_flujo.fisico_total_al_dia",
               return_value=363180.0), \
         patch("modules.informes.quimicos_flujo.entradas_bodega_mes",
               return_value=None), \
         patch("modules.informes.quimicos_flujo.ajustes_inventario_mes",
               return_value={"us": 0.0, "n": 0, "detalle": []}), \
         patch("modules.informes.quimicos_flujo.consumo_terminadas_mes",
               return_value={"us": 1.0}), \
         patch("modules.informes.quimicos_flujo.consumo_quimico_desglose",
               return_value={"costeado": 90000.0, "proceso": 5000.0, "lavado": 1000.0}), \
         patch("modules.informes.quimicos_flujo.fisico_colorante_al_dia",
               return_value=338000.0):
        mov = _build_mov_asinfo(_data(), _inv(), _inv(), anio=2026, mes=7,
                                proy_quimico=90000.0)
    qm = mov["quimicos_modelo"]
    assert "modelo" not in qm                      # modelo A viejo
    assert qm["inicial"] == 144637.0               # VQ0 del header
    assert qm["compras"] == 50000.0                # compras tipo Q programa
    assert qm["egresos"] == 90000.0                # proyectado
    assert qm["en_maquinas"] == 5000.0             # proceso del desglose
    assert qm["final_prog"] == 144637.0 + 50000.0 - 90000.0 - 5000.0
    assert qm["final_form"] == 338000.0
    co = mov["colorantes"]
    assert co["ajuste_us"] == round(qm["ajuste"], 0)
    assert co["maquinas_us"] == 5000.0
    assert "ajustes_inv_us" not in co


# ── _chequeo_coherencia ──────────────────────────────────────────────────────
def _check_quimicos(mov):
    return next((c for c in _chequeo_coherencia({}, mov, None)
                 if c["clave"] == "quimicos"), None)


def test_chequeo_banda_formulas_cierra_ok():
    mov = {"quimicos_banda": {"modelo": "formulas",
                              "final_prog": 371744.0, "final_form": 371000.0}}
    c = _check_quimicos(mov)
    assert c["etiqueta"] == "Químicos: la banda cierra"
    assert c["tipo"] == "cuadre"                   # normal, ya no "ajuste"
    assert c["estado"] == "ok"                     # |Δ| ≈ 0.2% ≤ 1%


def test_chequeo_banda_formulas_descuadre_warn():
    mov = {"quimicos_banda": {"modelo": "formulas",
                              "final_prog": 400000.0, "final_form": 363180.0}}
    c = _check_quimicos(mov)
    assert c["estado"] == "warn"                   # >1% → ⚠


def test_chequeo_fallback_sigue_informativo():
    """Modelo A viejo (sin 'modelo') → check físico vs libro tipo ajuste."""
    mov = {"quimicos_banda": {"final_prog": 100000.0, "final_form": 160000.0}}
    c = _check_quimicos(mov)
    assert c["etiqueta"] == "Químicos: físico vs libro"
    assert c["tipo"] == "ajuste" and c["estado"] == "info"
