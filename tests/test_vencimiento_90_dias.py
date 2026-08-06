"""Vencimiento de facturas: 90 días default (antes 30) + migración 0169.

TMT 2026-08-05 (dueña): "que la fecha de vencimiento de facturas sea 90
días y no 30, y que todos esos vencidos no aparezcan tanto". Dos mitades:

1. `dias_vencimiento()` — la regla del alta: `cliente.pago` numérico se
   respeta; cualquier otra cosa (contado 'C', 'X', vacío, None) cae al
   default, que hoy es 90.
2. La migración 0169 recalcula las IMPAGAS existentes con la MISMA regla,
   y sólo puede mover el vencimiento HACIA ADELANTE (GREATEST): un
   vencimiento cargado a mano más lejano no se acorta nunca.
"""
from __future__ import annotations

import re
from pathlib import Path

from modules.facturas.queries import DIAS_VENCIMIENTO_DEFAULT, dias_vencimiento

RAIZ = Path(__file__).resolve().parent.parent
MIG_0169 = (RAIZ / "migrations" / "0169_vencimiento_90_dias.py").read_text(
    encoding="utf-8"
)


# ---------------------------------------------------------------------------
# La regla del alta
# ---------------------------------------------------------------------------


def test_el_default_es_90_dias():
    """La decisión de la dueña (05/08): 90, no 30."""
    assert DIAS_VENCIMIENTO_DEFAULT == 90


def test_pago_no_numerico_cae_al_default():
    """Bug D: los legacy tienen 'C', 'X', '+', '.', '' — nada de eso parsea."""
    for basura in ("C", "X", "+", ".", "", "  ", None, "3O"):  # 3O = letra O
        assert dias_vencimiento(basura) == DIAS_VENCIMIENTO_DEFAULT, basura


def test_pago_numerico_se_respeta():
    """Si algún cliente tiene plazo propio cargado, ese manda — aún < 90."""
    assert dias_vencimiento("45") == 45
    assert dias_vencimiento(" 120 ") == 120
    assert dias_vencimiento("15") == 15


# ---------------------------------------------------------------------------
# La migración 0169 — misma regla, sólo hacia adelante, sólo impagas
# ---------------------------------------------------------------------------


def test_la_migracion_usa_el_mismo_default():
    """Si el default del alta cambia, la migración tiene que acompañar."""
    assert MIG_0169.count("ELSE 90") == 2, "las dos ramas (con y sin cliente)"
    assert "+ 90" in MIG_0169  # la rama sin cliente


def test_la_migracion_solo_mueve_hacia_adelante():
    """GREATEST en cada UPDATE: nunca acorta un vencimiento cargado a mano."""
    updates = re.findall(r"UPDATE\s+scintela\.factura.*?(?=\"\"\")", MIG_0169, re.DOTALL)
    assert len(updates) == 2
    for u in updates:
        assert "GREATEST" in u
        assert "COALESCE(f.vencimiento, f.fecha)" in u


def test_la_migracion_solo_toca_impagas():
    """Pendientes (stat Z/A/NULL/vacío) con saldo <> 0 — las pagas no se tocan."""
    assert MIG_0169.count("f.stat IN ('Z', 'A', '', ' ')") == 2
    assert MIG_0169.count("COALESCE(f.saldo, 0) <> 0") == 2


def test_la_migracion_es_idempotente_por_filtro():
    """El WHERE excluye las que ya están en o más allá del objetivo."""
    assert MIG_0169.count("COALESCE(f.vencimiento, f.fecha) <") == 2
