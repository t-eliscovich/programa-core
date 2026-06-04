"""Tests del guard anti-regresión de bancos en sync_dbase_actual.

Contexto (TMT 2026-06-03 incidente): un PICHINCH.DBF stale truncado en la
fila 422 (saldo 2,340,586.90) pisó vía TRUNCATE+INSERT los datos buenos de
PC (493 filas, saldo 2,385,393.30). El sync no avisó. Estos tests fijan el
comportamiento del guard que ahora lo caza, usando los números reales del
incidente como fixture.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sync_dbase_actual import _evaluar_regresion_banco  # noqa: E402

# Estado bueno (DBF fresco / PC sano): 493 filas, termina en 2,385,393.30.
FRESH = {
    "n": 493,
    "max_fecha": "2026-06-03",
    "last_saldo": 2385393.30,
    "saldos": {2340586.90, 2374620.96, 2385393.30},
}
# Estado stale/truncado: 493→422 filas, termina en un saldo INTERMEDIO del bueno.
STALE = {
    "n": 422,
    "max_fecha": "2026-06-03",
    "last_saldo": 2340586.90,
    "saldos": {2340586.90},
}
PC_VACIO = {"n": 0, "max_fecha": None, "last_saldo": None, "saldos": set()}


def test_sync_forward_es_ok():
    """Empujar el DBF fresco (493) sobre un PC stale (422) debe permitirse."""
    nivel, _ = _evaluar_regresion_banco(FRESH, STALE)
    assert nivel == "ok"


def test_regresion_stale_sobre_fresco_aborta():
    """EL BUG: empujar un DBF stale (422) sobre un PC sano (493) debe ABORTAR."""
    nivel, msg = _evaluar_regresion_banco(STALE, FRESH)
    assert nivel == "abort"
    assert "STALE" in msg or "truncado" in msg


def test_pc_vacio_no_bloquea():
    """Primer sync (PC sin filas) nunca debe bloquearse."""
    nivel, _ = _evaluar_regresion_banco(FRESH, PC_VACIO)
    assert nivel == "ok"


def test_fecha_maxima_anterior_warn():
    """DBF con fecha máxima anterior a PC, sin ser prefijo exacto → warn."""
    dbf = {"n": 600, "max_fecha": "2026-06-01", "last_saldo": 111.0, "saldos": {111.0}}
    pc = {"n": 500, "max_fecha": "2026-06-03", "last_saldo": 222.0, "saldos": {222.0, 333.0}}
    nivel, _ = _evaluar_regresion_banco(dbf, pc)
    assert nivel == "warn"


def test_mismo_estado_es_ok():
    """Re-sincronizar el mismo DBF (idempotente) no debe abortar."""
    nivel, _ = _evaluar_regresion_banco(FRESH, FRESH)
    assert nivel == "ok"


def test_dbf_sin_saldos_no_rompe():
    """DBF ilegible / sin saldos → guard se omite, no explota."""
    nivel, _ = _evaluar_regresion_banco({"n": 0, "max_fecha": None, "last_saldo": None, "saldos": set()}, FRESH)
    assert nivel == "ok"
