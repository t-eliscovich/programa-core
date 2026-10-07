"""Tamara 2026-10-07: /admin/health/all "se cuelga".

Medido en vivo: no se colgaba, tardaba ~80 s, y la rutina diaria se rendía a
los 45-60 s. `salidas_sin_saldo` (~48 s) y `acabado_pedidos` (~38 s) volvían a
preguntarle a Asinfo lo que el hilo de fondo ya había medido. Ahora el
health/all usa la medición del hilo si es reciente.
"""
from __future__ import annotations

import inspect

import pytest

from modules._lib import medicion_reciente as mr
from modules.asinfo import salidas_sin_saldo as sss
from modules.pedidos import vigia_acabado as vig


@pytest.fixture(autouse=True)
def _limpio():
    sss.ULTIMA.olvidar()
    vig.ULTIMA.olvidar()
    yield
    sss.ULTIMA.olvidar()
    vig.ULTIMA.olvidar()


def test_guarda_y_devuelve_con_la_edad(monkeypatch):
    m = mr.MedicionReciente()
    assert m.leer(60) is None
    m.guardar({"ok": True, "alerts": [], "stats": {"n": 1}})
    r = m.leer(60)
    assert r == {"ok": True, "alerts": [], "stats": {"n": 1, "medido_hace_min": 0}}
    r["stats"]["n"] = 99
    assert m.leer(60)["stats"]["n"] == 1, "devuelve una copia"


def test_vieja_no_sirve(monkeypatch):
    m = mr.MedicionReciente()
    reloj = [1000.0]
    monkeypatch.setattr(mr.time, "monotonic", lambda: reloj[0])
    m.guardar({"ok": True, "alerts": [], "stats": {}})
    reloj[0] += 30 * 60
    assert m.leer(3600)["stats"]["medido_hace_min"] == 30
    reloj[0] += 31 * 60
    assert m.leer(3600) is None


def test_sin_datos_no_se_guarda():
    m = mr.MedicionReciente()
    m.guardar({"ok": True, "alerts": [], "stats": {"sin_datos": True}})
    assert m.leer(3600) is None


def test_sin_resultado_vuelve_a_medir(monkeypatch):
    llamadas = []
    monkeypatch.setattr(sss, "health", lambda **k: llamadas.append(1) or {"ok": True})
    assert sss.health_reciente() == {"ok": True}
    sss.ULTIMA.guardar({"ok": False, "alerts": [{"x": 1}], "stats": {}})
    r = sss.health_reciente()
    assert r["ok"] is False and r["stats"]["medido_hace_min"] == 0
    assert llamadas == [1], "con una medición reciente no vuelve a Asinfo"


def test_salidas_health_guarda_lo_que_mide(monkeypatch):
    monkeypatch.setattr(sss, "detectar", lambda dias: {
        "ok": True, "salidas": [], "dias": dias, "lotes_sin_bajar": 0,
        "kg_sin_bajar": 0, "lotes_repetidos": 0})
    monkeypatch.setattr(sss, "detectar_ingresos", lambda dias: {"ok": True, "ingresos": []})
    monkeypatch.setattr(sss, "cuadre", lambda: {"ok": True, "bodegas": []})
    h = sss.health(avisar=False)
    assert sss.ULTIMA.leer(3600)["ok"] == h["ok"]


def test_vigia_reciente_y_corre_solo_cada_hora(monkeypatch):
    llamadas = []
    monkeypatch.setattr(vig, "health", lambda: llamadas.append(1) or {"ok": True})
    monkeypatch.setattr(vig, "_auto_ultimo", None)
    monkeypatch.delenv("ACABADO_VIGIA_AUTO", raising=False)
    monkeypatch.setenv("ACABADO_VIGIA_SECS", "nada")
    assert vig.correr_si_toca() == {"corrio": True, "ok": True}
    assert vig.correr_si_toca()["corrio"] is False, "no repite antes de la hora"
    assert vig.health_reciente() == {"ok": True}
    vig.ULTIMA.guardar({"ok": True, "alerts": [], "stats": {"lineas": 3}})
    assert vig.health_reciente()["stats"]["lineas"] == 3
    assert llamadas == [1, 1]


def test_vigia_se_apaga_y_no_se_cae(monkeypatch):
    monkeypatch.setattr(vig, "_auto_ultimo", None)
    monkeypatch.setenv("ACABADO_VIGIA_AUTO", "0")
    assert vig.correr_si_toca()["corrio"] is False
    monkeypatch.setenv("ACABADO_VIGIA_AUTO", "1")

    def boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(vig, "health", boom)
    assert vig.correr_si_toca()["corrio"] is False


def test_el_hilo_y_el_health_all_los_usan():
    from modules._lib import autocarga_facturas as af
    from modules.admin_dbase import health_audit_view as h
    assert "_vig_acab.correr_si_toca()" in inspect.getsource(af._loop)
    fuente = inspect.getsource(h.health_all)
    assert "_sss.health_reciente()" in fuente
    assert "_vig_acab.health_reciente()" in fuente
