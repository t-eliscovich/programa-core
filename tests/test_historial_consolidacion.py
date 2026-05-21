"""Tests de la lógica nueva de la pantalla Historial.

- consolidar_snapshots_mes_actual: deja las N columnas más recientes del
  mes en curso y borra el resto.
- eliminar_ultima_columna_mes_actual: borra la columna más reciente y
  deja viva la previa (botón "Eliminar última columna").

Usan mocks de `db` — no necesitan Postgres.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.informes import queries


def test_consolidar_deja_las_2_mas_recientes(monkeypatch):
    """conservar=2 → el DELETE usa LIMIT 2 y devuelve lo borrado."""
    cap: dict = {}

    def fake_execute(sql, params=None):
        cap["sql"] = sql
        cap["params"] = params
        return 3

    monkeypatch.setattr(queries.db, "execute", fake_execute)

    n = queries.consolidar_snapshots_mes_actual(conservar=2)

    assert n == 3
    assert cap["params"]["k"] == 2
    assert "DELETE FROM scintela.historia" in cap["sql"]
    assert "LIMIT %(k)s" in cap["sql"]


def test_consolidar_nunca_borra_todo(monkeypatch):
    """conservar=0 se clampa a 1 — nunca deja el mes sin ninguna columna."""
    cap: dict = {}
    monkeypatch.setattr(
        queries.db, "execute",
        lambda sql, params=None: cap.update(params=params) or 0,
    )
    queries.consolidar_snapshots_mes_actual(conservar=0)
    assert cap["params"]["k"] == 1


def test_consolidar_solo_toca_el_mes_actual(monkeypatch):
    """Regla de fin de mes: la consolidación SOLO borra columnas del mes
    en curso — nunca de meses ya cerrados.

    Esto garantiza que, al entrar el 1 de junio, la columna del 31 de
    mayo NO se toca: queda viva como resultado mensual de mayo.
    """
    cap: dict = {}
    monkeypatch.setattr(
        queries.db, "execute",
        lambda sql, params=None: cap.update(sql=sql, params=params) or 0,
    )
    queries.consolidar_snapshots_mes_actual(conservar=2)

    hoy = date.today()
    # El DELETE está acotado al AÑO y MES de hoy:
    assert cap["params"]["a"] == hoy.year
    assert cap["params"]["m"] == hoy.month
    assert "EXTRACT(YEAR FROM fecha) = %(a)s" in cap["sql"]
    assert "EXTRACT(MONTH FROM fecha) = %(m)s" in cap["sql"]
    # Una fila de un mes cerrado nunca puede matchear ese WHERE.


def test_eliminar_ultima_borra_la_mas_reciente(monkeypatch):
    """Borra la columna más reciente del mes y la reporta."""
    cap: dict = {}
    monkeypatch.setattr(queries.db, "fetch_one",
                        lambda sql, params=None: {"id_historia": 77})

    def fake_execute(sql, params=None):
        cap["params"] = params
        return 1

    monkeypatch.setattr(queries.db, "execute", fake_execute)

    r = queries.eliminar_ultima_columna_mes_actual()

    assert r["borrado"] is True
    assert r["id_historia"] == 77
    assert cap["params"] == (77,)


def test_eliminar_ultima_sin_columnas_no_rompe(monkeypatch):
    """Si no hay columnas del mes, no borra nada y no crashea."""
    monkeypatch.setattr(queries.db, "fetch_one", lambda *a, **k: None)
    monkeypatch.setattr(queries.db, "execute", lambda *a, **k: 0)

    r = queries.eliminar_ultima_columna_mes_actual()

    assert r["borrado"] is False
