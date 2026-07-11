"""Tests de _generar_xlsx_pendientes — buckets que el xlsx debe iterar.

TMT 2026-06-17 (Tamara, sesión #40 — 2do reporte): gastos/comisiones
del extracto aparecen en la PANTALLA pero NO en el xlsx descargable.
Causa: estado_sesion() separa los movs del extracto en buckets:
  - manual_banco: real_only NO comision
  - impuestos: real_only categorizados COMISION (SENAE, IVA, comisiones)

El código del xlsx solo iteraba "manual_banco" y perdía los del bucket
"impuestos". Este test es anti-regresión: garantiza que el código que
arma el xlsx procese AMBOS buckets.

TMT 2026-06-17 (decisión final): se RETIRÓ la sección "CARGOS DEL BANCO".
Ahora AMBOS buckets (manual_banco + impuestos) se fusionan en la lista
ÚNICA de pendientes (rows_reales) y suman al AJUSTE — la diferencia se
muestra como un solo número. Los impuestos NO van a una sección aparte.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_xlsx_itera_bucket_impuestos():
    """El generador de xlsx debe procesar también el bucket 'impuestos' del
    estado_sesion — no solo 'manual_banco' — sino se pierden los
    gastos/comisiones del extracto en el download."""
    bv = (ROOT / "modules/conciliacion/banco_v2_view.py").read_text()
    assert '"impuestos"' in bv, (
        "_generar_xlsx_pendientes debe procesar el bucket 'impuestos' del "
        "estado_sesion para incluir gastos/comisiones del extracto."
    )
    assert '"manual_banco"' in bv, "Falta el bucket 'manual_banco'."


def test_impuestos_van_a_pendientes_no_a_cargos():
    """Anti-regresión: la sección 'CARGOS DEL BANCO' se retiró. Los
    impuestos del extracto van a la lista única de pendientes
    (rows_reales), NO a una sección separada rows_cargos."""
    bv = (ROOT / "modules/conciliacion/banco_v2_view.py").read_text()
    assert "rows_cargos" not in bv, (
        "La sección CARGOS DEL BANCO se retiró: no debe quedar la variable "
        "rows_cargos en el código."
    )
    # El bucket impuestos debe terminar en rows_reales (lista única).
    assert "rows_reales.append" in bv, (
        "Los movs del extracto (manual_banco + impuestos) deben sumarse a "
        "rows_reales (la lista única de pendientes)."
    )
