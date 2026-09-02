"""COSTOS DE TINTORERÍA del mes en caché — `tintoreria_mensual_cacheada`.

TMT 2026-09-02 (dueña: *"¿páginas lentas?"*). El balance en caliente eran 129
consultas (300 ms) más 9 idas a formulas (550 ms), y el termómetro dijo cuál
era la peor: las órdenes del mes de formulas que `_build_tintoreria_mensual`
pide en cada visita para sacar UN número. Ahora se guarda 3 minutos y el
calentador la refresca.
"""
from __future__ import annotations

import inspect

from modules.comparativa_tintoreria import views as v


def test_la_segunda_visita_no_vuelve_a_armar_la_tabla(monkeypatch):
    llamadas = []

    def _armar(anio, mes, n_meses=None):
        llamadas.append((anio, mes))
        return {"filas": [{"t_imp": 100.0, "t_kg": 10.0}]}

    monkeypatch.setattr(v, "_build_tintoreria_mensual", _armar)
    a = v.tintoreria_mensual_cacheada(2026, 9)
    b = v.tintoreria_mensual_cacheada(2026, 9)
    assert a is b and llamadas == [(2026, 9)]
    v.tintoreria_mensual_cacheada(2026, 8)          # otro mes: otra clave
    assert llamadas == [(2026, 9), (2026, 8)]


def test_vencida_se_vuelve_a_armar(monkeypatch):
    import time

    llamadas = []
    monkeypatch.setattr(v, "_build_tintoreria_mensual",
                        lambda a, m, n_meses=None: llamadas.append(1) or {"filas": []})
    reloj = [1_000.0]
    monkeypatch.setattr(time, "time", lambda: reloj[0])
    v.tintoreria_mensual_cacheada(2026, 9)
    reloj[0] += v._TINT_MENSUAL_TTL_SECS - 1
    v.tintoreria_mensual_cacheada(2026, 9)
    assert len(llamadas) == 1
    reloj[0] += 2
    v.tintoreria_mensual_cacheada(2026, 9)
    assert len(llamadas) == 2


def test_un_fallo_no_se_guarda(monkeypatch):
    respuestas = [None, {"filas": [{"t_imp": 1.0}]}]
    monkeypatch.setattr(v, "_build_tintoreria_mensual",
                        lambda a, m, n_meses=None: respuestas.pop(0))
    assert v.tintoreria_mensual_cacheada(2026, 9) is None
    assert v.tintoreria_mensual_cacheada(2026, 9) == {"filas": [{"t_imp": 1.0}]}


def test_el_balance_y_el_flujo_usan_la_cacheada():
    """Si alguien vuelve a llamar a `_build_…` directo desde el balance, las
    9 idas a formulas vuelven a cada visita."""
    from modules.informes import queries as q
    from modules.informes import views as iv

    src = inspect.getsource(q.informe_balance)
    assert "tintoreria_mensual_cacheada(yy_actual, mesnum_actual)" in src
    assert "_build_tintoreria_mensual(yy_actual" not in src
    assert "tintoreria_mensual_cacheada(anio, mes)" in inspect.getsource(iv)


def test_el_calentador_la_refresca():
    from modules._lib import warmup

    assert "tintoreria_mensual_cacheada(yy, mm)" in inspect.getsource(warmup._warm_once)


# ── Las órdenes del mes de formulas, cacheadas ──────────────────────────────

def test_las_ordenes_del_mes_se_piden_una_vez_por_tres_minutos(monkeypatch):
    from datetime import date

    from modules.tintura import service as tsvc

    llamadas = []
    monkeypatch.setattr(tsvc, "_tinto_equiv_formulas",
                        lambda d, h, ex: llamadas.append((d, h, ex)) or ["orden"])
    a = tsvc.tinto_equiv_formulas(date(2026, 9, 1), date(2026, 9, 30), excluir_lavados=False)
    b = tsvc.tinto_equiv_formulas(date(2026, 9, 1), date(2026, 9, 30), excluir_lavados=False)
    assert a == b == ["orden"] and len(llamadas) == 1
    tsvc.tinto_equiv_formulas(date(2026, 9, 1), date(2026, 9, 30))   # con lavados excluidos: otra
    assert len(llamadas) == 2
    a.append("mutación del que llamó")
    assert tsvc.tinto_equiv_formulas(date(2026, 9, 1), date(2026, 9, 30), excluir_lavados=False) == ["orden"]


def test_sin_ordenes_no_se_cachea(monkeypatch):
    from modules.tintura import service as tsvc

    respuestas = [[], ["orden"]]
    monkeypatch.setattr(tsvc, "_tinto_equiv_formulas", lambda d, h, ex: respuestas.pop(0))
    assert tsvc.tinto_equiv_formulas() == []
    assert tsvc.tinto_equiv_formulas() == ["orden"]


def test_el_balance_lee_el_quimico_por_la_cache_del_flujo():
    """`quimicos_flujo.fisico_total_al_dia` es el MISMO número que pedía el
    balance a formulas en cada visita, con 240 s de caché y calentador. Y el
    colorante físico (otra ida) sólo se pide si el total no está."""
    from modules.informes import queries as q

    src = inspect.getsource(q.informe_balance)
    assert "_qf_bal.fisico_total_al_dia(today_ec())" in src
    assert "quimico_total_fisico(today_ec())" not in src
    assert src.index("fisico_total_al_dia(today_ec())") < src.index("stock_colorante_fisico(today_ec())")
