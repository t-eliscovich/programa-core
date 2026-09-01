"""La fila "Gs. Mes Anterior" de /informes/gastos tiene que sumarle a
TEJEDURÍA el tejido tercerizado (AP/RY) del mes que se congela — igual que
ya hace el total EN VIVO del mes en curso.

Federico reportó (01/09/2026) que agosto dio 165 real vs 123.902,12 en
pantalla. `col_total_prev["tej"]` se armaba con V1+V2+V3+amort DTJ del mes
anterior, sin sumar `tejido_mes_componentes(meses_atras=1)["us_externo"]` —
a diferencia de `col_total["tej"]` (mes en curso), que sí lo suma. Verificado
contra producción: 123.902,12 + 40.649,60 (tercerizado real de agosto) =
164.551,72 ≈ los 165 de Federico. Tintorería/Administración no llevan
tercerizado — por eso sólo tejeduría quedaba corta.
"""
from __future__ import annotations

from unittest.mock import patch

from modules.informes import queries
from modules.informes.views import _gastos_mes_anterior_componentes


def test_suma_tercerizado_solo_a_tejeduria():
    v_prev = {"v1": 50000.0, "v2": 30000.0, "v3": 10000.0,
              "v4": 100000.0, "v5": 50000.0, "v6": 20000.0,
              "v7": 80000.0, "v8": 40000.0, "v9": 10000.0}
    a_prev = {"dtj": 1000.0, "dcc": 2000.0, "deprcar": 500.0}
    tej_comp_prev = {"us_externo": 40649.60, "us_kk_gastos": 0.0}

    with patch.object(queries, "gastos_xgast_v1_a_v9_mes", return_value=v_prev), \
         patch.object(queries, "amortizaciones_mensuales", return_value=a_prev), \
         patch.object(queries, "tejido_mes_componentes", return_value=tej_comp_prev) as m_tej:
        out = _gastos_mes_anterior_componentes(meses_atras=1)
        m_tej.assert_called_once_with(meses_atras=1)

    assert out["tej"] == 50000.0 + 30000.0 + 10000.0 + 1000.0 + 40649.60
    # Tintorería y administración NO llevan tercerizado.
    assert out["tin"] == 100000.0 + 50000.0 + 20000.0 + 2000.0
    assert out["adm"] == 80000.0 + 40000.0 + 10000.0 + 500.0


def test_sin_tercerizado_queda_igual_que_antes():
    """Mes sin maquila externa (la mayoría de los meses): no cambia nada."""
    v_prev = {"v1": 1.0, "v2": 2.0, "v3": 3.0,
              "v4": 4.0, "v5": 5.0, "v6": 6.0,
              "v7": 7.0, "v8": 8.0, "v9": 9.0}
    a_prev = {"dtj": 0.0, "dcc": 0.0, "deprcar": 0.0}
    with patch.object(queries, "gastos_xgast_v1_a_v9_mes", return_value=v_prev), \
         patch.object(queries, "amortizaciones_mensuales", return_value=a_prev), \
         patch.object(queries, "tejido_mes_componentes", return_value={}):
        out = _gastos_mes_anterior_componentes(meses_atras=1)
    assert out["tej"] == 1.0 + 2.0 + 3.0


def test_tejido_mes_componentes_pasa_meses_atras_a_la_query():
    class _Capt:
        def __init__(self):
            self.params = None

        def fetch_all(self, sql, params=None):
            self.params = params
            return []

    capt = _Capt()
    with patch.object(queries, "db", capt):
        queries.tejido_mes_componentes(meses_atras=1)
    assert capt.params == {"meses_atras": 1}


def test_tejido_mes_componentes_default_es_mes_en_curso():
    class _Capt:
        def __init__(self):
            self.params = None

        def fetch_all(self, sql, params=None):
            self.params = params
            return []

    capt = _Capt()
    with patch.object(queries, "db", capt):
        queries.tejido_mes_componentes()
    assert capt.params == {"meses_atras": 0}
