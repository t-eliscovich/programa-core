"""Tests de _generar_xlsx_pendientes — buckets que el xlsx debe iterar.

TMT 2026-06-17 (Tamara, sesión #40 — 2do reporte): gastos/comisiones
del extracto aparecen en la PANTALLA pero NO en el xlsx descargable.
Causa: estado_sesion() separa los movs del extracto en buckets:
  - manual_banco: real_only NO comision
  - impuestos: real_only categorizados COMISION (SENAE, IVA, comisiones)

El código del xlsx solo iteraba "manual_banco" y perdía los del bucket
"impuestos". Este test es anti-regresión: garantiza que el código que
arma el xlsx procese AMBOS buckets.
"""
from __future__ import annotations
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_xlsx_itera_bucket_impuestos():
    """El generador de xlsx debe leer también el bucket 'impuestos' del
    estado_sesion — no solo 'manual_banco' — sino se pierden los
    gastos/comisiones del extracto en el download."""
    # Leo el source del módulo directamente para no requerir db real.
    bv = (ROOT / "modules/conciliacion/banco_v2_view.py").read_text()
    # El fix introduce un loop sobre _bk_xt.get("impuestos") dentro de
    # la función que arma el xlsx. Anti-regresión: ese loop debe existir.
    assert '_bk_xt.get("impuestos")' in bv, (
        "_generar_xlsx_pendientes debe iterar el bucket 'impuestos' del "
        "estado_sesion para incluir gastos/comisiones del extracto."
    )
    # Y debe agregarlos a rows_cargos (no a rows_reales) — son cargos
    # del banco que la dueña asienta como gasto, no pendientes que cruzan.
    # Verificamos que cerca del get("impuestos") se hace rows_cargos.append.
    idx = bv.find('_bk_xt.get("impuestos")')
    snippet = bv[idx:idx + 1500]
    assert "rows_cargos.append" in snippet, (
        "Los items del bucket 'impuestos' deben ir a rows_cargos "
        "(sección CARGOS DEL BANCO), no a rows_reales (pendientes)."
    )


def test_no_regreso_a_solo_manual_banco():
    """Anti-regresión paranoide: si alguien borra el loop de impuestos
    sin querer, este test falla."""
    bv = (ROOT / "modules/conciliacion/banco_v2_view.py").read_text()
    # Cuenta los loops sobre buckets del extracto
    n_manual = bv.count('_bk_xt.get("manual_banco")')
    n_impuestos = bv.count('_bk_xt.get("impuestos")')
    assert n_impuestos >= 1, "Falta el loop sobre 'impuestos'"
    assert n_manual >= n_impuestos, (
        "Se esperan al menos tantos loops manual_banco como impuestos "
        "(suelen ir juntos)."
    )
