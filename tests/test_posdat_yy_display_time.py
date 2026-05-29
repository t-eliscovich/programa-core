"""Tests del display-time YY (migración 0061, TMT 2026-05-28).

Cubre:
  - posdat.queries._dias_habiles_entre — fórmula de offset L-V.
  - posdat.queries._ultimo_dia_del_mes — helper de fin de mes.
  - posdat.queries._aplicar_display_time_yy:
      · No toca filas non-YY.
      · No toca YY sin baseline_date.
      · No toca YY con cuota_diaria=0.
      · Aplica fórmula correcta dentro del mes (hoy = baseline → offset 0,
        hoy = baseline+1 hábil → offset 1, etc.).
      · Saltea fines de semana.
      · Cruza de mes → dispara cierre lazy + reset a 0 (mockeado).

Patrón: sin DB real. Lo único que importa es la matemática + el flujo
de decisiones; los efectos en DB (mov_doble + UPDATE) se mockean.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import patch

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from modules.posdat import queries as q  # noqa: E402


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
        # Jue 28/05 → Vie 29/05 (intervalo (jue, vie]).
        assert q._dias_habiles_entre(date(2026, 5, 28), date(2026, 5, 29)) == 1

    def test_jueves_a_sabado_no_suma_finde(self):
        assert q._dias_habiles_entre(date(2026, 5, 28), date(2026, 5, 30)) == 1

    def test_jueves_a_domingo_no_suma_finde(self):
        assert q._dias_habiles_entre(date(2026, 5, 28), date(2026, 5, 31)) == 1

    def test_jueves_a_lunes_siguiente_dos_habiles(self):
        # (jue 28, lun 01] = vie 29 + lun 01 = 2.
        assert q._dias_habiles_entre(date(2026, 5, 28), date(2026, 6, 1)) == 2

    def test_cierre_mensual_baseline_31_a_lunes_es_uno(self):
        # Reset: baseline = 31/05 (dom). Lun 01/06 → offset 1.
        assert q._dias_habiles_entre(date(2026, 5, 31), date(2026, 6, 1)) == 1

    def test_un_mes_completo_habil(self):
        # Mayo 2026: lu 4, ma 5, ..., vie 29. Días hábiles = 21.
        # Desde "30/04" (jue, no incluido) hasta "31/05" (dom): cuenta L-V de mayo.
        assert q._dias_habiles_entre(date(2026, 4, 30), date(2026, 5, 31)) == 21

    def test_none_safe(self):
        assert q._dias_habiles_entre(None, date(2026, 5, 28)) == 0
        assert q._dias_habiles_entre(date(2026, 5, 28), None) == 0


# ── _ultimo_dia_del_mes ─────────────────────────────────────────────────

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
             id_posdat=42, concepto="SUELDOS"):
    """Construye una fila estilo `buscar()` para los tests."""
    return {
        "id_posdat": id_posdat,
        "prov": "YY",
        "importe": importe,
        "baseline_date": baseline,
        "cuota_diaria": cuota,
        "concepto": concepto,
    }


class TestAplicarDisplayTimeYY:
    def test_no_toca_filas_non_yy(self):
        f = {"prov": "RT", "importe": 500, "baseline_date": None, "cuota_diaria": 50}
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
        # 28/05 hoy = baseline. No suma nada.
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 28))
        assert f["importe"] == 86100.0
        assert f["dias_offset"] == 0
        assert f["importe_base"] == 86100.0

    def test_un_dia_habil_despues(self):
        # Jue 28/05 + 1 (vie 29/05) → +5000.
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 29))
        assert f["importe"] == 91100.0
        assert f["dias_offset"] == 1

    def test_fin_de_semana_no_suma(self):
        # Sábado 30/05 → mismo que viernes (+1 hábil).
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 30))
        assert f["importe"] == 91100.0
        # Domingo 31/05 → idem.
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 31))
        assert f["importe"] == 91100.0

    def test_redondeo_a_2_decimales(self):
        # cuota fraccional para verificar round(...,2).
        f = _fila_yy(importe=100.005, baseline=date(2026, 5, 28), cuota=33.337)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 29))
        # 100.005 + 33.337 = 133.342 → 133.34
        assert f["importe"] == 133.34


class TestCierreMensualLazy:
    """Cruce de mes: 28/05 baseline → 01/06 hoy.
    Esperado:
      1. _ejecutar_cierre_mensual_yy se llama con importe_cierre=86100+5000*1=91100
         (= valor display al último día hábil de mayo, vie 29).
      2. Después del cierre, importe_base=0, baseline=31/05, offset=1, importe=5000.
    """

    def test_cruza_mes_dispara_cierre_y_resetea(self):
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)

        with patch.object(q, "_ejecutar_cierre_mensual_yy") as mock_cierre:
            q._aplicar_display_time_yy([f], hoy=date(2026, 6, 1))

        # 1. Cierre invocado con importe del último día hábil de mayo.
        assert mock_cierre.called
        call_args = mock_cierre.call_args[0]
        # (id_posdat, concepto, importe_cierre, ult_dia_mes_ant)
        assert call_args[0] == 42  # id_posdat
        assert call_args[2] == 91100.0  # 86100 + 5000 × 1 (vie 29)
        assert call_args[3] == date(2026, 5, 31)  # último día calendario mayo

        # 2. Tras el cierre, la fila refleja el reset + lunes 01/06 suma 1.
        assert f["importe_base"] == 0.0
        assert f["baseline_date"] == date(2026, 5, 31)
        assert f["dias_offset"] == 1
        assert f["importe"] == 5000.0
        assert f.get("yy_cerrado_lazy") is True

    def test_no_redispara_cierre_si_baseline_ya_es_ultimo_dia_del_mes(self):
        """Edge case: la fila ya fue cerrada (baseline = 31/05). Hoy es 02/06.
        El render NO debe re-disparar el cierre (sería un UPDATE inútil
        cada vez que se carga la página)."""
        f = _fila_yy(importe=0, baseline=date(2026, 5, 31), cuota=5000)
        with patch.object(q, "_ejecutar_cierre_mensual_yy") as mock_cierre:
            q._aplicar_display_time_yy([f], hoy=date(2026, 6, 2))
        assert not mock_cierre.called  # NO se disparó cierre
        # Display normal: offset = días_hábiles(31/05, 02/06] = 2 (lun + mar).
        assert f["importe"] == 10000
        assert f["dias_offset"] == 2

    def test_si_cierre_falla_no_aplica_display_time(self):
        """Si el cierre lazy levanta, la fila queda con el importe
        persistido tal cual — sin sumar offsets erróneos."""
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)

        with patch.object(q, "_ejecutar_cierre_mensual_yy",
                          side_effect=RuntimeError("DB down")):
            q._aplicar_display_time_yy([f], hoy=date(2026, 6, 1))

        # La fila NO se modificó (importe sigue el persistido).
        assert f["importe"] == 86100
        assert f["baseline_date"] == date(2026, 5, 28)


class TestEjemploSueldosCompleto:
    """End-to-end del ejemplo del plan: SUELDOS cuota_diaria=5000.

    | Día | importe esperado |
    | Jue 28/05 (hoy real) | 86.100 |
    | Vie 29/05            | 91.100 |
    | Sáb 30/05            | 91.100 |
    | Dom 31/05            | 91.100 |
    | Lun 01/06 (post bake-in) | 5.000 |
    | Mar 02/06            | 10.000 |
    """

    def test_jueves_baseline(self):
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 28))
        assert f["importe"] == 86100

    def test_viernes_29(self):
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 29))
        assert f["importe"] == 91100

    def test_sabado_30(self):
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 30))
        assert f["importe"] == 91100

    def test_domingo_31(self):
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        q._aplicar_display_time_yy([f], hoy=date(2026, 5, 31))
        assert f["importe"] == 91100

    def test_lunes_01_06_post_bake_in(self):
        f = _fila_yy(importe=86100, baseline=date(2026, 5, 28), cuota=5000)
        with patch.object(q, "_ejecutar_cierre_mensual_yy"):
            q._aplicar_display_time_yy([f], hoy=date(2026, 6, 1))
        assert f["importe"] == 5000  # reset + 1 día hábil

    def test_martes_02_06(self):
        # Fila ya en estado post-reset (importe=0, baseline=31/05).
        # baseline = último día de mayo → no redispara cierre.
        f = _fila_yy(importe=0, baseline=date(2026, 5, 31), cuota=5000)
        with patch.object(q, "_ejecutar_cierre_mensual_yy") as mock_cierre:
            q._aplicar_display_time_yy([f], hoy=date(2026, 6, 2))
        assert not mock_cierre.called
        assert f["importe"] == 10000  # 2 hábiles × 5000
