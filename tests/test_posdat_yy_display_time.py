"""Tests del display-time YY/RT (TMT 2026-06-03 — reforma sin cierre).

Cubre:
  - posdat.queries._dias_habiles_entre — fórmula de offset L-V.
  - posdat.queries._ultimo_dia_del_mes — helper (legacy, sólo helper).
  - posdat.queries._aplicar_display_time_yy:
      · Aplica a filas prov IN ('YY','RT') con baseline_date + cuota > 0.
      · No toca filas con prov fuera de (YY,RT).
      · No toca YY/RT sin baseline_date.
      · No toca YY/RT con cuota_diaria=0.
      · Acumula PERPETUO (sin cierre mensual lazy): el offset crece
        sin reset al cruzar mes — comportamiento dBase MENU.PRG L283-333.
      · Saltea fines de semana.

Patrón: sin DB real, sólo la matemática + el flujo de decisiones.
"""
from __future__ import annotations

import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.posdat import queries as q  # noqa: E402


# TMT 2026-07-23 (dueña): switch ACUMULACION_YY_ACTIVA ELIMINADO — la
# acumulación YY/RT está siempre activa. Estos tests validan la matemática
# del display-time, que ahora corre siempre.


# ── _dias_habiles_entre ─────────────────────────────────────────────────

class TestDiasHabilesEntre:
    """Calendario 2026-05/06 a la mano para sanity-check:
        Lu 25/05  Ma 26  Mi 27  Ju 28 ← HOY  Vi 29  Sá 30  Do 31
        Lu 01/06  Ma 02  Mi 03  Ju 04  Vi 05  Sá 06  Do 07
    """

    def test_mismo_dia_es_cero(self):
        assert q._dias_habiles_entre(date(2026, 5, 28), date(2026, 5, 28)) == 0

    def test_hasta_anterior_es_cero(self):
        assert q._dias_habiles_entre(date(2026, 5, 28), date(2026, 5, 27)) == 0

    def test_jueves_a_viernes_es_uno(self):
        assert q._dias_habiles_entre(date(2026, 5, 28), date(2026, 5, 29)) == 1

    def test_jueves_a_sabado_no_suma_finde(self):
        assert q._dias_habiles_entre(date(2026, 5, 28), date(2026, 5, 30)) == 1

    def test_jueves_a_domingo_no_suma_finde(self):
        assert q._dias_habiles_entre(date(2026, 5, 28), date(2026, 5, 31)) == 1

    def test_jueves_a_lunes_siguiente_dos_habiles(self):
        # (jue 28, lun 01] = vie 29 + lun 01 = 2.
        assert q._dias_habiles_entre(date(2026, 5, 28), date(2026, 6, 1)) == 2

    def test_un_mes_completo_habil(self):
        # Mayo 2026: L-V de mayo = 21 días hábiles.
        assert q._dias_habiles_entre(date(2026, 4, 30), date(2026, 5, 31)) == 21

    def test_perpetuo_cruza_meses(self):
        # (30/04, 03/06] = mayo L-V (21) + junio L,M,Mi (3) = 24.
        assert q._dias_habiles_entre(date(2026, 4, 30), date(2026, 6, 3)) == 24

    def test_none_safe(self):
        assert q._dias_habiles_entre(None, date(2026, 5, 28)) == 0
        assert q._dias_habiles_entre(date(2026, 5, 28), None) == 0


# ── _ultimo_dia_del_mes (legacy helper) ─────────────────────────────────

class TestUltimoDiaDelMes:
    def test_mayo_31(self):
        assert q._ultimo_dia_del_mes(date(2026, 5, 15)) == date(2026, 5, 31)

    def test_febrero_no_bisiesto_28(self):
        assert q._ultimo_dia_del_mes(date(2026, 2, 1)) == date(2026, 2, 28)

    def test_febrero_bisiesto_29(self):
        assert q._ultimo_dia_del_mes(date(2024, 2, 1)) == date(2024, 2, 29)

    def test_diciembre_31(self):
        assert q._ultimo_dia_del_mes(date(2026, 12, 31)) == date(2026, 12, 31)


# ── _aplicar_display_time_yy ─────────────────────────────────────────────

def _fila_yy(*, importe=1000, baseline=date(2026, 5, 28), cuota=100,
             id_posdat=42, concepto="SUELDOS", prov="YY"):
    """Construye una fila estilo `buscar()` para los tests."""
    return {
        "id_posdat": id_posdat,
        "prov": prov,
        "importe": importe,
        "baseline_date": baseline,
        "cuota_diaria": cuota,
        "concepto": concepto,
    }


class TestAplicarDisplayTimeYY:
    def test_no_toca_filas_otras_provs(self):
        # provedor regular (EM, BP, etc) no devenga
        f = {"prov": "EM", "importe": 500, "baseline_date": date(2026, 5, 28),
             "cuota_diaria": 50}
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 29))
        assert f["importe"] == 500

    def test_no_toca_yy_sin_baseline(self):
        f = {"prov": "YY", "importe": 500, "baseline_date": None, "cuota_diaria": 50}
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 29))
        assert f["importe"] == 500

    def test_no_toca_yy_con_cuota_cero(self):
        f = _fila_yy(cuota=0)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 29))
        assert f["importe"] == 1000

    def test_hoy_igual_baseline_offset_cero(self):
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 28))
        assert f["importe"] == 86100.0
        assert f["dias_offset"] == 0
        assert f["importe_base"] == 86100.0

    def test_un_dia_habil_despues(self):
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 29))
        assert f["importe"] == 91100.0
        assert f["dias_offset"] == 1

    def test_fin_de_semana_no_suma(self):
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 30))
        assert f["importe"] == 91100.0
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 31))
        assert f["importe"] == 91100.0

    def test_redondeo_a_2_decimales(self):
        f = _fila_yy(importe=100.005, baseline=date(2026, 5, 28), cuota=33.337)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 29))
        assert f["importe"] == 133.34


class TestAcumulaPerpetuoSinCierre:
    """TMT 2026-06-03 reforma: SIN cierre mensual lazy.

    El offset se acumula sobre TODOS los meses entre baseline y hoy, igual
    que dBase REPLACE IMPORTE+cuota cada día hábil sin reset. Estos tests
    blindan que el cierre no vuelva por accidente."""

    def test_cruza_mes_acumula_sin_reset(self):
        # Jue 28/05 baseline → Lun 01/06 hoy. Hábiles = vie 29 + lun 01 = 2.
        # Comportamiento perpetuo: importe = 86100 + 5000*2 = 96100. NO 5000.
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 6, 1))
        assert f["importe"] == 96100.0
        assert f["importe_base"] == 86100.0  # no se resetea
        assert f["dias_offset"] == 2
        # marker yy_cerrado_lazy NO debe aparecer (el cierre fue removido)
        assert "yy_cerrado_lazy" not in f

    def test_cruza_meses_sigue_acumulando(self):
        # baseline 30/04 → 03/06 hoy. (30/04, 03/06] = 21 may + 3 jun = 24 hábiles.
        f = _fila_yy(importe=10000, baseline=date(2026, 4, 30), cuota=1000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 6, 3))
        assert f["importe"] == 34000.0  # 10000 + 1000*24
        assert f["importe_base"] == 10000.0
        assert f["dias_offset"] == 24


class TestRTtambienAcumula:
    """RT (IVA) debe acumular igual que YY (mismo dBase L319-321).

    Antes RT estaba excluido del display-time; ahora entra."""

    def test_rt_aplica_display_time(self):
        f = _fila_yy(importe=125000, baseline=date(2026, 5, 28),
                     cuota=8400, prov="RT", concepto="")
        q._aplicar_display_time_yy([f], hoy=date(2026, 6, 3))
        # (28/05, 03/06] = vie 29 + lun 01 + mar 02 + mie 03 = 4 hábiles.
        assert f["importe"] == 125000 + 8400 * 4
        assert f["dias_offset"] == 4

    def test_rt_sin_baseline_intacto(self):
        f = {"prov": "RT", "importe": 100000, "baseline_date": None,
             "cuota_diaria": 8400}
        q._aplicar_display_time_yy([f], hoy=date(2026, 6, 3))
        assert f["importe"] == 100000  # sin baseline, no se toca


class TestEjemploSueldosCompletoSinCierre:
    """End-to-end de SUELDOS cuota_diaria=5000, baseline=jue 28/05/2026.

    Comportamiento NUEVO (perpetuo, sin cierre):
    | Día | importe esperado |
    | Jue 28/05 (baseline) | 86.100 |
    | Vie 29/05            | 91.100 |
    | Sáb 30/05            | 91.100 |
    | Dom 31/05            | 91.100 |
    | Lun 01/06            | 96.100 |  (antes era 5.000 post-reset)
    | Mar 02/06            | 101.100 |  (antes era 10.000)
    | Mie 03/06            | 106.100 |
    """

    @pytest.mark.parametrize("hoy,esperado", [
        (date(2026, 5, 28), 86100),
        (date(2026, 5, 29), 91100),
        (date(2026, 5, 30), 91100),
        (date(2026, 5, 31), 91100),
        (date(2026, 6, 1),  96100),
        (date(2026, 6, 2),  101100),
        (date(2026, 6, 3),  106100),
    ])
    def test_acumulacion_perpetua(self, hoy, esperado):
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=hoy)
        assert f["importe"] == esperado
