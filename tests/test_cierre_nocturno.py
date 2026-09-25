"""El cierre de mes se saca la ÚLTIMA NOCHE del mes (Tamara 2026-09-25).

- `cerrar_mes_de_noche`: el último día congela los gastos del mes, saca la foto
  de cierre forzada (que arma el PDF) y verifica el PDF; cualquier otro día no
  hace nada. Cada paso falla solo, sin frenar a los otros.
- `debe_pisar_el_cierre`: la tarea del día 1 sólo rehace una foto diaria DE
  PASO; nunca la de la noche ni el cierre nocturno.
- `foto_diaria_cron.main` corre el cierre sólo el último día, dentro de la app.
- El health del día 1 avisa si el cierre lo sacó el día 1 o falta el PDF.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.informes import cierre_nocturno as cn  # noqa: E402


@pytest.mark.parametrize("d,esperado", [
    (date(2026, 9, 30), True), (date(2026, 9, 29), False),
    (date(2026, 10, 31), True), (date(2026, 10, 30), False),
    (date(2027, 2, 28), True), (date(2028, 2, 28), False), (date(2028, 2, 29), True),
])
def test_es_ultimo_dia(d, esperado):
    assert cn.es_ultimo_dia(d) is esperado


def _stubs(monkeypatch, gastos=None, foto=None, pdf=True, boom=()):
    from modules.informes import cierres_paquete, queries, views
    llamadas = {"gastos": [], "foto": [], "meses_atras": []}

    def comp(meses_atras=1):
        llamadas["meses_atras"].append(meses_atras)
        if "comp" in boom:
            raise RuntimeError("xgast caído")
        return dict(gastos or {"tej": 160_000.0, "tin": 360_000.0, "adm": 290_000.0})

    def gset(periodo, tej, tin, adm, usuario=None):
        llamadas["gastos"].append((periodo, tej, tin, adm, usuario))
        return {"periodo": periodo}

    def snap(anio, mes, usuario="auto", forzar=False, dry_run=False):
        llamadas["foto"].append((anio, mes, usuario, forzar))
        if "foto" in boom:
            raise RuntimeError("balance caído")
        return dict(foto or {"aplicado": True, "id_historia": 900, "razon": "ok"})

    monkeypatch.setattr(views, "_gastos_mes_anterior_componentes", comp)
    monkeypatch.setattr(queries, "gastos_mes_manual_set", gset)
    monkeypatch.setattr(queries, "crear_snapshot_historia", snap)
    monkeypatch.setattr(cierres_paquete, "obtener", lambda a, m: b"%PDF" if pdf else None)
    return llamadas


def test_un_dia_comun_no_hace_nada(monkeypatch):
    ll = _stubs(monkeypatch)
    r = cn.cerrar_mes_de_noche(date(2026, 9, 29))
    assert r["corrio"] is False
    assert ll == {"gastos": [], "foto": [], "meses_atras": []}


def test_ultimo_dia_congela_gastos_saca_la_foto_y_ve_el_pdf(monkeypatch):
    ll = _stubs(monkeypatch)
    r = cn.cerrar_mes_de_noche(date(2026, 9, 30))
    assert r["corrio"] is True and r["alertas"] == []
    assert ll["meses_atras"] == [0]                       # el mes que termina HOY
    assert ll["gastos"] == [("2026-09", 160_000.0, 360_000.0, 290_000.0, "cierre-nocturno")]
    assert ll["foto"] == [(2026, 9, "cierre-nocturno", True)]  # forzada
    assert r["pasos"]["pdf"] is True


def test_sin_pdf_avisa(monkeypatch):
    _stubs(monkeypatch, pdf=False)
    r = cn.cerrar_mes_de_noche(date(2026, 9, 30))
    assert any("PDF" in a for a in r["alertas"])


def test_gastos_en_cero_no_se_congelan(monkeypatch):
    ll = _stubs(monkeypatch, gastos={"tej": 0.0, "tin": 0.0, "adm": 0.0})
    r = cn.cerrar_mes_de_noche(date(2026, 9, 30))
    assert ll["gastos"] == []
    assert any("gastos" in a for a in r["alertas"])
    assert ll["foto"]                                     # la foto sale igual


def test_si_fallan_los_gastos_la_foto_sale_igual(monkeypatch):
    ll = _stubs(monkeypatch, boom=("comp",))
    r = cn.cerrar_mes_de_noche(date(2026, 9, 30))
    assert ll["foto"] and len(r["alertas"]) == 1


def test_si_falla_la_foto_se_avisa_y_no_revienta(monkeypatch):
    ll = _stubs(monkeypatch, boom=("foto",))
    r = cn.cerrar_mes_de_noche(date(2026, 9, 30))
    assert ll["gastos"]
    assert any("foto de cierre falló" in a for a in r["alertas"])


def test_foto_no_aplicada_avisa(monkeypatch):
    _stubs(monkeypatch, foto={"aplicado": False, "razon": "balance falló"})
    r = cn.cerrar_mes_de_noche(date(2026, 9, 30))
    assert any("no se guardó" in a for a in r["alertas"])


# ─── la tarea del día 1 ─────────────────────────────────────────────────────

CIERRE = date(2026, 9, 30)


def test_no_pisa_si_no_hay_nada():
    assert cn.debe_pisar_el_cierre([], CIERRE) is False


def test_pisa_una_foto_diaria_de_media_tarde():
    filas = [{"usuario_crea": "snapshot-diario", "fecha_crea": datetime(2026, 9, 30, 20, 0)}]
    assert cn.debe_pisar_el_cierre(filas, CIERRE) is True


def test_no_pisa_la_foto_de_la_noche():
    # 23:30 EC = 04:30 UTC del 01/10
    filas = [{"usuario_crea": "snapshot-diario", "fecha_crea": datetime(2026, 10, 1, 4, 30)}]
    assert cn.debe_pisar_el_cierre(filas, CIERRE) is False


def test_no_pisa_el_cierre_nocturno_aunque_haya_una_de_paso():
    filas = [{"usuario_crea": "snapshot-diario", "fecha_crea": datetime(2026, 9, 30, 15, 0)},
             {"usuario_crea": "cierre-nocturno", "fecha_crea": datetime(2026, 10, 1, 4, 31)}]
    assert cn.debe_pisar_el_cierre(filas, CIERRE) is False
    assert cn.debe_pisar_el_cierre(list(reversed(filas)), CIERRE) is False


def test_no_pisa_una_reconstruccion_a_mano():
    filas = [{"usuario_crea": "tamara", "fecha_crea": datetime(2026, 9, 30, 12, 0)}]
    assert cn.debe_pisar_el_cierre(filas, CIERRE) is False


def test_fecha_crea_con_zona_se_compara_bien():
    from datetime import timezone
    filas = [{"usuario_crea": "snapshot-diario",
              "fecha_crea": datetime(2026, 10, 1, 4, 30, tzinfo=UTC)}]
    assert cn.debe_pisar_el_cierre(filas, CIERRE) is False


def test_la_tarea_del_dia_1_usa_la_regla_y_no_limit_1():
    import inspect

    import scripts.procesa_provisiones_mensual as ppm
    src = inspect.getsource(ppm)
    assert "debe_pisar_el_cierre" in src
    assert "WHERE fecha = %s LIMIT 1" not in src


# ─── el cron de las 23:30 ───────────────────────────────────────────────────

def _cron(monkeypatch, hoy, cierre_alertas=()):
    import importlib

    import filters
    import modules.admin_dbase.health_audit_view as hav
    import scripts.foto_diaria_cron as mod
    importlib.reload(mod)
    monkeypatch.setattr(filters, "today_ec", lambda: hoy)
    monkeypatch.setattr(hav, "ejecutar_foto_diaria",
                        lambda: {"ok": True, "alerts": [], "stats": {"hoy": {}}})
    corridas = []

    def fake_cierre(h=None):
        corridas.append(True)
        return {"corrio": True, "periodo": "2026-09", "pasos": {}, "alertas": list(cierre_alertas)}

    monkeypatch.setattr(cn, "cerrar_mes_de_noche", fake_cierre)
    monkeypatch.setattr(mod, "_con_app", lambda fn: fn())
    return mod, corridas


def test_cron_un_dia_comun_no_cierra(monkeypatch):
    mod, corridas = _cron(monkeypatch, date(2026, 9, 29))
    assert mod.main() == 0 and corridas == []


def test_cron_ultimo_dia_cierra(monkeypatch):
    mod, corridas = _cron(monkeypatch, date(2026, 9, 30))
    assert mod.main() == 0 and corridas == [True]


def test_cron_ultimo_dia_con_alertas_sale_en_rojo(monkeypatch):
    mod, _ = _cron(monkeypatch, date(2026, 9, 30), cierre_alertas=["CIERRE: sin PDF"])
    assert mod.main() == 1


def test_con_app_apaga_los_hilos_y_sin_app_corre_igual(monkeypatch):
    import builtins
    import importlib

    import scripts.foto_diaria_cron as mod
    importlib.reload(mod)
    real_import = builtins.__import__

    def sin_app(name, *a, **k):
        if name == "app":
            raise ImportError("sin app")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", sin_app)
    for var in ("WARMUP_ASINFO", "VIGIA_SERVIDOR", "AUTOCARGA_FACTURAS", "PDF_NAVEGADOR_PERSISTENTE"):
        monkeypatch.setenv(var, "1")
    assert mod._con_app(lambda: "hecho") == "hecho"
    for var in ("WARMUP_ASINFO", "VIGIA_SERVIDOR", "AUTOCARGA_FACTURAS", "PDF_NAVEGADOR_PERSISTENTE"):
        assert os.environ[var] == "0"


# ─── el health del día 1 ────────────────────────────────────────────────────

def _cats_arranque(**cambios):
    import modules.admin_dbase.health_audit_view as hv
    from tests.test_arranque_de_mes_health import _datos
    return [a["category"] for a in hv.arranque_de_mes_alertas(_datos(**cambios))["alerts"]]


def test_health_avisa_si_el_cierre_lo_saco_el_dia_1():
    pa = {"fecha": date(2026, 8, 31), "patrimonio": 9_500_000.0,
          "usuario_crea": "cron_snapshot_historia"}
    assert "cierre_tomado_el_dia_1" in _cats_arranque(patant=pa)


def test_health_no_avisa_con_el_cierre_nocturno():
    pa = {"fecha": date(2026, 8, 31), "patrimonio": 9_500_000.0,
          "usuario_crea": "cierre-nocturno"}
    assert "cierre_tomado_el_dia_1" not in _cats_arranque(patant=pa)


def test_health_avisa_si_falta_el_pdf():
    assert "cierre_sin_pdf" in _cats_arranque(paquete_prev=False)
    assert "cierre_sin_pdf" not in _cats_arranque(paquete_prev=True)
    assert "cierre_sin_pdf" not in _cats_arranque()   # sin dato, no inventa
