"""Flujo de producción en un mes CERRADO (Tamara 2026-10-01).

Mirando agosto, la última fila decía "Stock act." y mostraba el stock de HOY.
Ahora un mes cerrado sale de los movimientos de Asinfo del mes (cuadran al
kilo con la planilla de Tamara), dice "Stock final", y al lado va el saldo de
Asinfo con los kilos que cuenta de más. Más el desplegable "Mes a mes".
"""
from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.asinfo import kardex_bodegas as kb  # noqa: E402

# Lo que devolvió Asinfo el 01/10/2026 para septiembre (consola /admin/sql).
_FILAS_SEP = [
    {"b": 51, "ym": 0, "pre": "", "op": 1, "kg": 16148524.3},
    {"b": 51, "ym": 0, "pre": "", "op": -1, "kg": 14238360.9},
    {"b": 51, "ym": 202609, "pre": "BOD", "op": 1, "kg": 582330.0},
    {"b": 51, "ym": 202609, "pre": "SM", "op": -1, "kg": 347608.3},
    {"b": 52, "ym": 0, "pre": "", "op": 1, "kg": 14222332.0},
    {"b": 52, "ym": 0, "pre": "", "op": -1, "kg": 14002295.2},
    {"b": 52, "ym": 202609, "pre": "IFT", "op": 1, "kg": 354102.9},
    {"b": 52, "ym": 202609, "pre": "SM", "op": -1, "kg": 369800.9},
    {"b": 52, "ym": 202609, "pre": "AEGR", "op": -1, "kg": 67.7},
    {"b": 52, "ym": 202609, "pre": "TFB", "op": 1, "kg": 1425.8},
    {"b": 52, "ym": 202609, "pre": "TFB", "op": -1, "kg": 1425.8},
    {"b": 53, "ym": 0, "pre": "", "op": 1, "kg": 14569550.3},
    {"b": 53, "ym": 0, "pre": "", "op": -1, "kg": 14245940.9},
    {"b": 53, "ym": 202609, "pre": "IFT", "op": 1, "kg": 341457.1},
    {"b": 53, "ym": 202609, "pre": "DES", "op": -1, "kg": 351752.7},
    {"b": 53, "ym": 202609, "pre": "AING", "op": 1, "kg": 34.2},
    {"b": 53, "ym": 202609, "pre": "001", "op": 1, "kg": 5439.7},
    {"b": 53, "ym": 202609, "pre": "AEGR", "op": -1, "kg": 783.4},
    {"b": 53, "ym": 202609, "pre": "NCNT", "op": 1, "kg": 478.1},
    {"b": 53, "ym": 202609, "pre": "SM", "op": -1, "kg": 429.0},
    {"b": 53, "ym": 202609, "pre": "TFB", "op": 1, "kg": 25251.6},
    {"b": 53, "ym": 202609, "pre": "TFB", "op": -1, "kg": 25251.6},
]
_SALDO_SEP = [{"b": 51, "saldo": 2202034.0}, {"b": 52, "saldo": 251675.2},
              {"b": 53, "saldo": 318082.9}]


def _fake_consultar(sql, max_results=5000):
    if "saldo_producto_lote" in sql:
        return _SALDO_SEP, True
    return _FILAS_SEP, True


def test_septiembre_cuadra_con_la_planilla():
    kb.reset_cache()
    with patch.object(kb, "_consultar", side_effect=_fake_consultar):
        m = kb.mes(2026, 9, hoy=date(2026, 10, 1))
    h = m[51]
    # La planilla: 1.910.162 + 582.329 − 347.608 = 2.144.883; saldo 2.202.0xx
    assert round(h["inicial"]) == 1910163
    assert round(h["ingresos"]) == 582330
    assert round(h["salidas"]) == 347608
    assert round(h["final"]) == 2144885
    assert round(h["de_mas"]) == 57149
    assert m["en_curso"] is False
    # Tela cruda: AEGR y las transferencias van a ajustes, no a ingresos/salidas.
    c = m[52]
    assert round(c["ajustes"], 1) == -67.7
    assert round(c["saldo"] - c["final"]) == 47404
    # Terminado: devoluciones (001, NCNT) y ajustes no son ni IFT ni DES.
    t = m[53]
    assert round(t["final"]) == 318053
    assert round(t["de_mas"]) == 29


def test_el_inicial_de_un_mes_es_el_final_del_anterior():
    kb.reset_cache()
    filas = [
        {"b": 51, "ym": 0, "pre": "", "op": 1, "kg": 1000.0},
        {"b": 51, "ym": 202606, "pre": "BOD", "op": 1, "kg": 300.0},
        {"b": 51, "ym": 202606, "pre": "SM", "op": -1, "kg": 100.0},
        {"b": 51, "ym": 202607, "pre": "AING", "op": 1, "kg": 5.0},
    ]

    def fake(sql, max_results=5000):
        if "saldo_producto_lote" in sql:
            return [{"b": 51, "saldo": 1210.0}], True
        return filas, True

    with patch.object(kb, "_consultar", side_effect=fake):
        r = kb.meses(2026, 6, 7, hoy=date(2026, 10, 1))
    jun, jul = r["meses"]
    assert jun[51]["final"] == 1200.0
    assert jul[51]["inicial"] == 1200.0
    assert jul[51]["ajustes"] == 5.0
    assert jul[51]["final"] == 1205.0
    assert jun[51]["de_mas"] == 10.0
    assert jul[51]["de_mas_nuevo"] == -5.0


def test_si_asinfo_no_contesta_no_inventa():
    kb.reset_cache()
    with patch.object(kb, "_consultar", return_value=([], False)):
        assert kb.mes(2026, 9, hoy=date(2026, 10, 1)) is None


def test_meses_futuros_no_se_piden():
    kb.reset_cache()
    with patch.object(kb, "_consultar", side_effect=_fake_consultar) as q:
        r = kb.meses(2026, 11, 12, hoy=date(2026, 10, 1))
    assert r == {"ok": True, "meses": []}
    q.assert_not_called()


def _mov_asinfo():
    return {
        "hilado": {"stock_inic_ukg": 3.0, "ingresos_us": 1_000_000.0, "egresos_ukg": 3.1},
        "tejido": {}, "terminado": {}, "colorantes": {},
        "maquinas": {"hilado": 31196.0, "crudo": 39432.0, "hilado_us": 1.0},
    }


def test_aplicar_mes_cerrado_pone_los_movimientos_y_el_saldo():
    from modules.informes import views
    kb.reset_cache()
    with patch.object(kb, "_consultar", side_effect=_fake_consultar):
        k = kb.mes(2026, 9, hoy=date(2026, 10, 1))
    mov = _mov_asinfo()
    with patch.object(views.queries, "apertura_ukg_hilado", return_value=3.2):
        views._aplicar_mes_cerrado(mov, k, 2026, 9)
    hl = mov["hilado"]
    assert round(hl["stock_inic_kg"]) == 1910163
    assert round(hl["egresos_kg"]) == 347608
    assert round(hl["stock_act_kg"]) == 2144885
    assert hl["stock_act_ukg"] == 3.2
    assert round(hl["stock_act_us"]) == round(k[51]["final"] * 3.2)
    # En máquinas no se reconstruye a fecha pasada → renglón fuera.
    assert not mov["maquinas"]["hilado"] and not mov["maquinas"]["crudo"]
    assert round(mov["saldo_asinfo"]["hilado_de_mas"]) == 57149


def _render(app, mes_cerrado, mov):
    from flask import render_template
    with app.test_request_context("/informes/flujo-produccion?anio=2026&mes=9"):
        from flask import g
        g.user = {"usuario": "t", "id_usuario": 1}
        g.permisos = {"*"}
        return render_template(
            "informes/flujo_produccion.html", data={}, anio=2026, mes=9, error=None,
            inv_asinfo={}, mov_asinfo=mov, prod_tej_asinfo=None, coherencia=[],
            inv_en_proceso_tc={}, espera_hilado=0, espera_crudo=0,
            inv_en_proceso_pt={}, kg_vendidos_terminado=0, mes_cerrado=mes_cerrado)


def test_la_pantalla_dice_stock_final_y_el_saldo_en_un_mes_cerrado(app):
    from modules.informes import views
    kb.reset_cache()
    with patch.object(kb, "_consultar", side_effect=_fake_consultar):
        k = kb.mes(2026, 9, hoy=date(2026, 10, 1))
    mov = _mov_asinfo()
    with patch.object(views.queries, "apertura_ukg_hilado", return_value=3.2):
        views._aplicar_mes_cerrado(mov, k, 2026, 9)
    html = _render(app, True, mov)
    assert "Stock final" in html and "Stock act." not in html
    assert "Saldo Asinfo" in html
    assert "De más" in html and "+57.149" in html
    assert "2.144.885" in html
    assert "Mes a mes" in html


def test_el_mes_en_curso_sigue_diciendo_stock_act(app):
    html = _render(app, False, _mov_asinfo())
    assert "Stock act." in html
    assert "Saldo Asinfo" not in html


def test_mes_a_mes_responde_la_planilla(app, fake_db):
    rid = fake_db.add_role("Inf", ["informes.ver"])
    uid = fake_db.add_user("inf", b"$2b$12$fakehash", rid)
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = uid
    kb.reset_cache()
    with patch.object(kb, "_consultar", side_effect=_fake_consultar):
        html = c.get("/informes/flujo-produccion/mes-a-mes?anio=2026&mes=9").get_data(as_text=True)
    assert "Sep" in html
    assert "2.144.885" in html     # final según movimientos
    assert "2.202.034" in html     # final según saldo Asinfo
    assert "57.149" in html        # kilos de más


def test_mes_a_mes_son_los_ultimos_cinco_cruzando_el_anio():
    kb.reset_cache()
    pedidos = []

    def fake(sql, max_results=5000):
        pedidos.append(sql)
        if "saldo_producto_lote" in sql:
            return [{"b": 51, "saldo": 0}], True
        return [], True

    with patch.object(kb, "_consultar", side_effect=fake):
        r = kb.ultimos(2026, 2, 5, hoy=date(2026, 10, 1))
    assert [(m["anio"], m["mes"]) for m in r["meses"]] == [
        (2025, 10), (2025, 11), (2025, 12), (2026, 1), (2026, 2)]
