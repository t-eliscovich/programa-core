"""Tamara 28/09/2026: la revaluación "sin compras nuevas" va en el renglón del
stock — "hil. y tej. → term." −5.040 y "sin compras nuevas … · cambió el $/kg
de 3 etapas" −270 son un solo renglón de −5.310."""
from unittest.mock import patch

from modules.informes import traza as t

MOVS = [
    {"componente": "vsto", "doc_id": "#stock", "tipo": "cambio",
     "etiqueta": "hil. y tej. → term.", "regla": "Stock",
     "aporte": -5040.0, "familia": "utilidad"},
    {"componente": "vsto", "doc_id": "#stock:tarifa", "tipo": "cambio",
     "etiqueta": "cambió el $/kg de 3 etapas",
     "regla": "Revaluación de stock", "aporte": -270.0, "familia": "utilidad"},
]


def _resumir(causa):
    with patch.object(t.db, "fetch_all", return_value=[]):
        return t.resumir(MOVS, -5310.0, {}, causa_tarifa=causa)


def test_sin_compras_nuevas_se_suma_al_stock():
    out = _resumir(t.TXT_MEZCLA)
    assert len(out) == 1
    assert out[0]["aporte"] == -5310.0
    assert out[0]["por_col"] == {"vsto": -5310.0}
    assert "sin compras nuevas" not in out[0]["texto"]


def test_con_causa_de_verdad_sigue_aparte():
    out = _resumir("importación recibida (+2.606,88)")
    assert len(out) == 2
    assert any(g["texto"].startswith("importación recibida") for g in out)
