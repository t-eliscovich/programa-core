"""Tamara 2026-09-03 — *"dejá que se autocomplete con el mes anterior hasta que
alguien le ponga valores"*.

Septiembre 2026 nació sin `pre`, `pretej`, `pretin`, `preadm` (lo creó
`cerrar_mes_auto` cuando copiaba 12 columnas) y así siguió tres días. Ahora el
mismo paso diario que hace el rollover (`rollover_y_writeback_iniciales`, en
el cron y en /admin/health/all) rellena lo que está NULL/0 con lo del mes
anterior — y sólo eso: lo cargado a mano gana siempre.
"""
from __future__ import annotations

import datetime as _dt

from modules.informes import queries

AGOSTO = {
    "id_iniciales": 400, "mesnum": 8, "yy": 2026,
    "hilado": 1_500_000.0, "tejido": 200_000.0, "terminado": 350_000.0, "vq": 120_000.0,
    "um": 3.04, "uk": 3.5, "uf": 5.2, "uq": 0.64, "pre": 8.55,
    "kprog": 320_000.0, "gprog": 0.0, "numnot": 365.0, "dificil": 0.0,
    "pretej": 165_000.0, "pretin": 360_000.0, "preadm": 290_000.0, "pretot": 815_000.0,
}
# Lo que dejó cerrar_mes_auto el 01/09: 12 columnas.
SEPTIEMBRE = dict(AGOSTO, id_iniciales=401, mesnum=9,
                  pre=None, pretej=None, pretin=None, preadm=None, dificil=None)


def _install(monkeypatch, sep, ago=AGOSTO):
    calls = []

    def fake_fetch_one(sql, params=None):
        s = " ".join(sql.split())
        if "SELECT id_iniciales FROM scintela.iniciales WHERE mesnum" in s:
            return {"id_iniciales": 401} if sep else None
        if "SELECT * FROM scintela.iniciales WHERE mesnum" in s:
            m, _y = params
            return {9: sep, 8: ago}.get(m)
        return None

    monkeypatch.setattr(queries.db, "fetch_one", fake_fetch_one, raising=True)
    monkeypatch.setattr(queries.db, "execute",
                        lambda sql, params=None, conn=None: calls.append((" ".join(sql.split()), params)),
                        raising=True)
    return calls


def test_septiembre_2026_se_completa_con_agosto(monkeypatch):
    calls = _install(monkeypatch, SEPTIEMBRE)
    out = queries.completar_iniciales_desde_mes_anterior(_dt.date(2026, 9, 3))
    # dificil NO: agosto tampoco lo tenía.
    assert out == {"pre": 8.55, "pretej": 165_000.0, "pretin": 360_000.0, "preadm": 290_000.0}
    assert len(calls) == 1
    sql, params = calls[0]
    assert sql == ("UPDATE scintela.iniciales SET pre=%s, pretej=%s, pretin=%s, preadm=%s "
                   "WHERE id_iniciales=%s")
    assert params == (8.55, 165_000.0, 360_000.0, 290_000.0, 401)


def test_lo_cargado_a_mano_gana(monkeypatch):
    """Andrés puso 8,70 de precio: no se pisa con el 8,55 de agosto."""
    calls = _install(monkeypatch, dict(SEPTIEMBRE, pre=8.70))
    out = queries.completar_iniciales_desde_mes_anterior(_dt.date(2026, 9, 3))
    assert "pre" not in out
    assert calls[0][1][-1] == 401 and 8.55 not in calls[0][1]


def test_una_fila_entera_no_toca_nada(monkeypatch):
    calls = _install(monkeypatch, dict(AGOSTO, id_iniciales=401, mesnum=9))
    assert queries.completar_iniciales_desde_mes_anterior(_dt.date(2026, 9, 3)) == {}
    assert calls == []


def test_sin_fila_del_mes_o_del_anterior_no_hace_nada(monkeypatch):
    calls = _install(monkeypatch, None)
    assert queries.completar_iniciales_desde_mes_anterior(_dt.date(2026, 9, 3)) == {}
    calls = _install(monkeypatch, SEPTIEMBRE, ago=None)
    assert queries.completar_iniciales_desde_mes_anterior(_dt.date(2026, 9, 3)) == {}
    assert calls == []


def test_dry_run_dice_que_completaria_sin_escribir(monkeypatch):
    calls = _install(monkeypatch, SEPTIEMBRE)
    out = queries.completar_iniciales_desde_mes_anterior(_dt.date(2026, 9, 3), dry_run=True)
    assert set(out) == {"pre", "pretej", "pretin", "preadm"}
    assert calls == []


def test_en_enero_hereda_de_diciembre(monkeypatch):
    dic = dict(AGOSTO, mesnum=12, yy=2026)
    ene = dict(SEPTIEMBRE, mesnum=1, yy=2027)

    def fake_fetch_one(sql, params=None):
        m, y = params
        return {(1, 2027): ene, (12, 2026): dic}.get((m, y))

    monkeypatch.setattr(queries.db, "fetch_one", fake_fetch_one, raising=True)
    monkeypatch.setattr(queries.db, "execute", lambda *a, **k: None, raising=True)
    out = queries.completar_iniciales_desde_mes_anterior(_dt.date(2027, 1, 1))
    assert out["pre"] == 8.55


def test_el_rollover_diario_lo_llama_y_lo_informa(monkeypatch):
    """Es el paso que corre todos los días (cron + /admin/health/all): así lo
    que nació a medias se completa solo, sin que nadie abra nada."""
    _install(monkeypatch, SEPTIEMBRE)
    monkeypatch.setattr(queries, "informe_balance", lambda: {"error": "sin asinfo"}, raising=True)
    out = queries.rollover_y_writeback_iniciales(fecha=_dt.date(2026, 9, 3))
    assert out["rollover"] is False
    assert set(out["completadas"]) == {"pre", "pretej", "pretin", "preadm"}


def test_las_columnas_son_las_del_health():
    from modules.admin_dbase import health_audit_view as hv
    assert hv.INICIALES_COLUMNAS_QUE_VIAJAN is queries.INICIALES_COLUMNAS_QUE_VIAJAN
