"""Tests de la 2ª pata del historial (movimientos de doble asiento cuyo
mov_doble es auto-referencia: retiro OP, gasto a crédito).

Usan mocks de `db` — no necesitan Postgres. TMT 2026-07-14 (dueña).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.historial import queries


def test_segunda_pata_tipo_desconocido_es_none():
    """Un tipo sin resolver (o sin 2ª pata) devuelve None."""
    assert queries.segunda_pata({"tipo": "caja_s_simple", "origen_id": 5}) is None
    assert queries.segunda_pata({}) is None
    assert queries.segunda_pata(None) is None


def test_segunda_pata_retiro_op_muestra_imputacion(monkeypatch):
    """retiro_op → una línea con la imputación a la línea OP + la nota de que
    el ↺ revierte ambas patas."""
    monkeypatch.setattr(
        queries.db, "fetch_one",
        lambda *a, **k: {
            "id_op_retiro_linea": 12,
            "line_key": "P|305|APORTE ANDRES",
            "monto": 1500.0,
            "bajo_posdat": True,
            "fecha": None,
        },
    )
    sp = queries.segunda_pata({"tipo": "retiro_op", "origen_id": 88})
    assert sp is not None
    assert "revierte AMBAS" in sp["nota"]
    assert len(sp["lineas"]) == 1
    linea = sp["lineas"][0]
    assert linea["ref"] == "OP #305"
    assert linea["importe"] == 1500.0
    assert "baja el restante" in linea["concepto"]
    # bajo_posdat=True → menciona que sube el posdat OP.
    assert "posdat OP" in linea["concepto"]
    assert "APORTE ANDRES" in linea["concepto"]


def test_segunda_pata_retiro_op_sin_imputacion_es_none(monkeypatch):
    """Si no hay op_retiro_linea para ese retiro → None (no rompe)."""
    monkeypatch.setattr(queries.db, "fetch_one", lambda *a, **k: None)
    assert queries.segunda_pata({"tipo": "retiro_op", "origen_id": 88}) is None


def test_segunda_pata_gasto_a_posdat_muestra_deuda(monkeypatch):
    """gasto_a_posdat → una línea con la deuda posdat (pasivo)."""
    llamadas = {"n": 0}

    def fake_fetch_one(*a, **k):
        llamadas["n"] += 1
        if llamadas["n"] == 1:  # xgast
            return {"prov": "PROVX", "num": 7, "importe": 320.5, "fechad": "31/07/2026"}
        return {"num": 7}  # posdat hermana

    monkeypatch.setattr(queries.db, "fetch_one", fake_fetch_one)
    sp = queries.segunda_pata({"tipo": "gasto_a_posdat", "origen_id": 44})
    assert sp is not None
    assert "revierte AMBAS" in sp["nota"]
    linea = sp["lineas"][0]
    assert linea["ref"] == "#7"
    assert linea["importe"] == 320.5
    assert "pasivo" in linea["concepto"]
    assert "vence 31/07/2026" in linea["extra"]


def test_segunda_pata_es_defensiva_ante_error(monkeypatch):
    """Si el lookup levanta, segunda_pata swallowea y devuelve None."""
    def boom(*a, **k):
        raise RuntimeError("db caída")

    monkeypatch.setattr(queries.db, "fetch_one", boom)
    assert queries.segunda_pata({"tipo": "retiro_op", "origen_id": 1}) is None
    assert queries.segunda_pata({"tipo": "gasto_a_posdat", "origen_id": 1}) is None
