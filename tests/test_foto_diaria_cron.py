"""Tests para `scripts/foto_diaria_cron.py`.

El script en sí es un wrapper finito: llama a `ejecutar_foto_diaria()`
(la misma función que usa `/admin/health/snapshot-diario`, ver
`modules/admin_dbase/health_audit_view.py`) y traduce el resultado a un
exit code para que el Scheduled Task de Windows pueda marcar la corrida
en rojo sin que nadie mire a mano. No toca Postgres: se testea
monkeypencheando `ejecutar_foto_diaria` directamente.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


@pytest.fixture(autouse=True)
def _dia_comun(monkeypatch):
    """Estos tests son de la foto diaria: un día cualquiera de mitad de mes,
    para que el cierre nocturno (sólo el último día) no se meta si la suite
    corre justo un fin de mes. El cierre se testea en test_cierre_nocturno.py."""
    from datetime import date

    import filters
    monkeypatch.setattr(filters, "today_ec", lambda: date(2026, 9, 15))


@pytest.fixture(autouse=True)
def _sin_conexiones(monkeypatch):
    """El script abre las conexiones de la base y de formulas_app; en los
    tests no se abre nada de verdad."""
    import db
    from modules._lib import formulas_db
    monkeypatch.setattr(db, "init_pool", lambda: None)
    monkeypatch.setattr(formulas_db, "init_pool", lambda: None)


def _importar_script():
    import importlib

    import scripts.foto_diaria_cron as mod
    importlib.reload(mod)
    return mod


def test_main_devuelve_0_cuando_ok(monkeypatch):
    mod = _importar_script()
    import modules.admin_dbase.health_audit_view as hav

    monkeypatch.setattr(
        hav, "ejecutar_foto_diaria",
        lambda: {
            "ok": True, "alerts": [],
            "stats": {"hoy": {"fecha": "2026-09-02", "patrimonio": 21_732_772.07,
                               "ustock": 8_727_036.69}},
        },
    )
    assert mod.main() == 0


def test_main_devuelve_1_cuando_hay_alertas(monkeypatch):
    mod = _importar_script()
    import modules.admin_dbase.health_audit_view as hav

    monkeypatch.setattr(
        hav, "ejecutar_foto_diaria",
        lambda: {
            "ok": False,
            "alerts": ["Patrimonio saltó +900,000 vs 2026-09-01 — revisar (umbral $500k)."],
            "stats": {"hoy": {"fecha": "2026-09-02", "patrimonio": 22_632_772.07,
                               "ustock": 8_727_036.69}},
        },
    )
    assert mod.main() == 1


def test_main_devuelve_1_si_la_foto_fallo_sin_stats(monkeypatch):
    """Caso `crear_snapshot_diario()` levantó adentro de `ejecutar_foto_diaria`
    -- ok=False y "stats" sin la llave "hoy" (ver el except en la función
    compartida). El script no debe reventar leyendo `stats["hoy"]`."""
    mod = _importar_script()
    import modules.admin_dbase.health_audit_view as hav

    monkeypatch.setattr(
        hav, "ejecutar_foto_diaria",
        lambda: {"ok": False, "alerts": ["snapshot diario falló: boom"], "stats": {}},
    )
    assert mod.main() == 1


def test_main_llama_ejecutar_foto_diaria_una_sola_vez(monkeypatch):
    mod = _importar_script()
    import modules.admin_dbase.health_audit_view as hav

    llamadas = []

    def _fake():
        llamadas.append(1)
        return {"ok": True, "alerts": [], "stats": {"hoy": {}}}

    monkeypatch.setattr(hav, "ejecutar_foto_diaria", _fake)
    mod.main()
    assert len(llamadas) == 1


def test_main_abre_la_conexion_a_formulas_antes_de_la_foto(monkeypatch):
    """Tamara 2026-09-30: el script corre sin la app y nadie abría la conexión
    a formulas_app. Sin ella el Stock de Químicos caía al cálculo viejo, que no
    resta el consumo: la foto de la noche ponía 689.100 cuando había 506.373
    (+182.727 de utilidad inventada, justo la noche del cierre)."""
    mod = _importar_script()
    import db
    import modules.admin_dbase.health_audit_view as hav
    from modules._lib import formulas_db

    orden = []
    monkeypatch.setattr(db, "init_pool", lambda: orden.append("db"))
    monkeypatch.setattr(formulas_db, "init_pool", lambda: orden.append("formulas"))

    def _foto():
        orden.append("foto")
        return {"ok": True, "alerts": [], "stats": {"hoy": {}}}

    monkeypatch.setattr(hav, "ejecutar_foto_diaria", _foto)
    assert mod.main() == 0
    assert orden == ["db", "formulas", "foto"]


def test_si_una_conexion_no_abre_la_foto_igual_sale(monkeypatch):
    mod = _importar_script()
    import modules.admin_dbase.health_audit_view as hav
    from modules._lib import formulas_db

    def _explota():
        raise RuntimeError("sin red")

    monkeypatch.setattr(formulas_db, "init_pool", _explota)
    monkeypatch.setattr(
        hav, "ejecutar_foto_diaria",
        lambda: {"ok": True, "alerts": [], "stats": {"hoy": {}}},
    )
    assert mod.main() == 0
