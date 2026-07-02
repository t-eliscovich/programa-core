"""Prueba determinística del rollover de fin de mes (crear_iniciales del mes
nuevo) SIN esperar al 01/08. Simula que falta la fila del mes en curso y
verifica que se crea copiando el CIERRE del mes anterior — replica del
MENU.PRG/SETEO del dBase. Bug origen: 2026-07-01 (sin fila del mes → stock 0)."""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.informes import queries

# Cierre de JULIO (lo que el write-back dejó en INICIALE[Jul])
JULIO = {
    "id_iniciales": 318, "mesnum": 7, "yy": 2026,
    "hilado": 1_620_603.70, "tejido": 233_323.70, "terminado": 359_116.02,
    "vq": 126_394.0, "um": 2.92, "uk": 3.42, "uf": 5.12, "uq": 0.64, "pre": 8.57,
    "kprog": 320_000.0, "gprog": 0.0, "numnot": 365.0, "dificil": 0.0,
    "pretej": 120_000.0, "pretin": 0.0, "preadm": 0.0, "pretot": 0.0,
}

_BAL_FAKE = {
    "resultados": {"stock": {
        "hilado": {"kg": 1_620_603.70, "ukg": 2.92},
        "tejido": {"kg": 233_323.70, "ukg": 3.42},
        "terminado": {"kg": 359_116.02, "ukg": 5.12},
    }},
    "diagnostico": {"componentes": {"vqx": 126_394.0}},
}


def _install(monkeypatch, agosto_existe: bool):
    calls = {"execute": []}

    def fake_fetch_one(sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        # 1) existe fila del mes en curso (agosto = 8, 2026)?
        if "SELECT id_iniciales FROM scintela.iniciales WHERE mesnum" in s:
            return {"id_iniciales": 400} if agosto_existe else None
        # 2) fila del mes anterior (julio) para copiar
        if "SELECT * FROM scintela.iniciales WHERE mesnum" in s:
            m, y = params
            return JULIO if (m == 7 and y == 2026) else None
        return None

    def fake_execute(sql, params=None, conn=None):
        calls["execute"].append((" ".join(sql.split()), params))

    monkeypatch.setattr(queries.db, "fetch_one", fake_fetch_one, raising=True)
    monkeypatch.setattr(queries.db, "execute", fake_execute, raising=True)
    monkeypatch.setattr(queries, "informe_balance", lambda: _BAL_FAKE, raising=True)
    return calls


def test_rollover_crea_agosto_desde_cierre_julio(monkeypatch):
    """01/08, sin fila de Agosto → la crea copiando el cierre de Julio."""
    calls = _install(monkeypatch, agosto_existe=False)
    out = queries.rollover_y_writeback_iniciales(fecha=_dt.date(2026, 8, 1))

    assert out["rollover"] is True, "debía crear la fila de agosto"
    inserts = [(s, p) for (s, p) in calls["execute"] if "INSERT INTO scintela.iniciales" in s]
    assert len(inserts) == 1, "debía haber exactamente 1 INSERT"
    _, p = inserts[0]
    # La apertura de agosto = el CIERRE de julio, campo por campo
    assert p["mesnum"] == 8 and p["yy"] == 2026
    assert p["mesnom"] == "Aug"
    assert p["hilado"] == JULIO["hilado"]
    assert p["tejido"] == JULIO["tejido"]
    assert p["terminado"] == JULIO["terminado"]
    assert p["vq"] == JULIO["vq"]
    # Y después el write-back (UPDATE) del stock vivo
    updates = [1 for (s, _) in calls["execute"] if "UPDATE scintela.iniciales" in s]
    assert updates, "debía correr el write-back (UPDATE)"


def test_rollover_idempotente_si_agosto_existe(monkeypatch):
    """Si la fila de Agosto ya existe → NO la crea (no duplica)."""
    calls = _install(monkeypatch, agosto_existe=True)
    out = queries.rollover_y_writeback_iniciales(fecha=_dt.date(2026, 8, 1))

    assert out["rollover"] is False
    inserts = [1 for (s, _) in calls["execute"] if "INSERT INTO scintela.iniciales" in s]
    assert not inserts, "NO debía insertar (idempotente)"
    # pero el write-back igual corrige el stock del mes
    updates = [1 for (s, _) in calls["execute"] if "UPDATE scintela.iniciales" in s]
    assert updates, "el write-back debía correr igual"
