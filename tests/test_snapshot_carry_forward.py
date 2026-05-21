"""Regresión: insertar_snapshot hace carry-forward de ktej/ktin/utej/utin.

Bug original (Federico 2026-05-21): el snapshot nuevo dejaba esos 4
campos —que vienen de TINT.BAT y calcular_kpis() NO computa— en NULL/0,
lo que ponía en cero la TINTORERIA y el $/kg de /flujo-produccion.

insertar_snapshot() ahora los copia del último snapshot que sí los tenga.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import snapshot_historia_mensual as snap

_KPIS_BASE = {
    "fecha": date(2026, 5, 21),
    "cart": 1.0, "deuda": 2.0, "banco": 3.0, "gasto": 4.0, "retiro": 5.0,
    "kvent": 6.0, "uvent": 7.0, "kcom": 8.0, "ucom": 9.0,
}


def test_carry_forward_copia_campos_de_produccion(monkeypatch):
    """Con un snapshot previo, el INSERT lleva sus ktej/ktin/utej/utin."""
    capturado: dict = {}

    def fake_fetch_one(sql, params=None):
        if "ktej" in sql and "scintela.historia" in sql:
            return {"ktej": 1234.0, "ktin": 567.0, "utej": 89000.0, "utin": 45000.0}
        return None

    def fake_execute_returning(sql, params=None):
        capturado["sql"] = sql
        capturado["params"] = params
        return {"id_historia": 999}

    monkeypatch.setattr(snap.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(snap.db, "execute_returning", fake_execute_returning)

    new_id = snap.insertar_snapshot(dict(_KPIS_BASE), usuario="test")

    assert new_id == 999
    p = capturado["params"]
    assert p["ktej"] == 1234.0
    assert p["ktin"] == 567.0
    assert p["utej"] == 89000.0
    assert p["utin"] == 45000.0
    # Las columnas nuevas están en el INSERT.
    assert "ktej, ktin, utej, utin" in capturado["sql"]


def test_sin_snapshot_previo_no_rompe(monkeypatch):
    """Sin snapshot previo, los 4 campos quedan en None y no hay crash."""
    capturado: dict = {}

    def fake_execute_returning(sql, params=None):
        capturado["params"] = params
        return {"id_historia": 1}

    monkeypatch.setattr(snap.db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(snap.db, "execute_returning", fake_execute_returning)

    snap.insertar_snapshot(dict(_KPIS_BASE), usuario="test")

    p = capturado["params"]
    assert p["ktej"] is None
    assert p["ktin"] is None
    assert p["utej"] is None
    assert p["utin"] is None
