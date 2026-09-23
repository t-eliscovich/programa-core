"""Top 10 de /informes/estado-cuenta: saldo, cheques y total IGUALES a la ficha.

TMT 2026-09-23 (dueña): *"sumar cheques y total — fijate que coincidan exacto
con lo que vemos al abrir estado de cuenta"*. Para que no puedan divergir, el
Top 10 corre las MISMAS consultas de totales que la ficha
(`_agregados_estado_cuenta` + `_totales_estado_cuenta`).
"""
from __future__ import annotations

import inspect
from unittest.mock import patch

from modules.informes import queries as iq


def test_ficha_y_top10_comparten_las_consultas_de_totales():
    assert "_agregados_estado_cuenta(cods)" in inspect.getsource(iq.estado_cuenta_lote)
    src = inspect.getsource(iq.top_clientes_estado_cuenta)
    assert "_agregados_estado_cuenta(cods)" in src
    assert "_totales_estado_cuenta(" in src


def _correr(agregados, cods):
    def fake_fetch_all(sql, params=None):
        if "DISTINCT codigo_cli" in sql:
            return [{"codigo_cli": c} for c in cods]
        return [{"codigo_cli": c, "nombre": f"N-{c}"} for c in params[0]]

    with patch.object(iq.db, "fetch_all", side_effect=fake_fetch_all), \
         patch.object(iq, "_agregados_estado_cuenta", return_value=agregados):
        return iq.top_clientes_estado_cuenta(2)


def test_numeros_son_los_tiles_de_la_ficha():
    fac = {"AAA": {"saldo": 1000}, "BBB": {"saldo": 5000}, "CCC": {"saldo": 300}}
    che = {"AAA": {"por_cobrar": 520.10}, "BBB": {"por_cobrar": 1500}}
    ant = {"CCC": {"anticipo_raw": -80}}
    top = _correr((fac, che, ant), ["AAA", "BBB", "CCC"])
    assert [r["codigo_cli"] for r in top] == ["BBB", "AAA"]
    for r in top:
        t = iq._totales_estado_cuenta(fac.get(r["codigo_cli"]) or {},
                                      che.get(r["codigo_cli"]) or {},
                                      ant.get(r["codigo_cli"]) or {})
        assert r["saldo"] == t["saldo_neto"]
        assert r["cheques"] == t["cheques_por_cobrar"]
        assert r["total"] == t["saldo_neto"] + t["cheques_por_cobrar"]
        assert r["nombre"] == f"N-{r['codigo_cli']}"


def test_el_saldo_a_favor_netea_como_en_la_ficha():
    fac = {"AAA": {"saldo": 300}, "BBB": {"saldo": 250}}
    ant = {"AAA": {"anticipo_raw": -80}}
    top = _correr((fac, {}, ant), ["AAA", "BBB"])
    assert [(r["codigo_cli"], r["saldo"]) for r in top] == [("BBB", 250), ("AAA", 220)]


def test_sin_clientes_no_explota():
    with patch.object(iq.db, "fetch_all", return_value=[]):
        assert iq.top_clientes_estado_cuenta() == []


# ---------------------------------------------------------------------------
# Pestañas Vendedor / Provincia / Grupos / Todos (mismo pedido, 23/09)
# ---------------------------------------------------------------------------


def test_pestanias_usan_los_totales_de_la_ficha():
    src = inspect.getsource(iq.con_totales_de_la_ficha)
    assert "_agregados_estado_cuenta(cods)" in src
    assert "_totales_estado_cuenta(" in src


def test_con_totales_de_la_ficha_pisa_saldo_y_agrega_cheques_y_total():
    filas = [{"codigo_cli": "AAA", "saldo": 300.0}, {"codigo_cli": "BBB", "saldo": 50.0}]
    fac = {"AAA": {"saldo": 300}, "BBB": {"saldo": 50}}
    che = {"BBB": {"por_cobrar": 25.5}}
    ant = {"AAA": {"anticipo_raw": -80}}
    with patch.object(iq, "_agregados_estado_cuenta", return_value=(fac, che, ant)):
        out = iq.con_totales_de_la_ficha(filas)
    assert [(r["saldo"], r["cheques"], r["total"]) for r in out] == [
        (220.0, 0.0, 220.0), (50.0, 25.5, 75.5)]
    # No toca las filas que recibe (las usa también el aviso del portal).
    assert filas[0]["saldo"] == 300.0 and "cheques" not in filas[0]


def test_con_totales_de_la_ficha_sin_filas():
    assert iq.con_totales_de_la_ficha([]) == []
