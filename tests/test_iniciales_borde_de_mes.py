"""Bug 2026-07-01: en el borde de mes faltaba la fila de INICIALES del mes en
curso (Julio) y sobraba la del mes SIGUIENTE (Agosto). `iniciales_mes_actual()`
caía a "la más reciente con datos" = Agosto (futuro), y el stock de terminado se
calculaba contra un mes previo inexistente -> terminado 0 -> patrimonio -2M ->
utilidad -1,69M fantasma.

Estos tests fijan la regla: si falta el mes en curso, NUNCA caer a un mes futuro;
usar el último cierre <= mes corriente. Y excluir filas corruptas (yy NULL).
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.informes import queries


class _FakeToday:
    """today_ec() -> 2026-07-01 (borde de mes)."""
    @staticmethod
    def __call__():
        return _dt.date(2026, 7, 1)


JUNIO = {"id_iniciales": 100, "yy": 2026, "mesnum": 6, "hilado": 1_629_523.0,
         "tejido": 250_301.0, "terminado": 351_849.0, "um": 2.92}
AGOSTO = {"id_iniciales": 120, "yy": 2026, "mesnum": 8, "hilado": 1_629_523.0,
          "tejido": 250_301.0, "terminado": 351_849.0, "um": 2.92}


def _install(monkeypatch, rows):
    """rows = lista de filas 'existentes' (dicts). fetch_one emula las queries
    de iniciales_mes_actual filtrando por la SQL/params recibidos."""
    monkeypatch.setattr(queries, "today_ec", _FakeToday(), raising=True)

    def fake_fetch_one(sql, params=None):
        params = params or ()
        s = " ".join(sql.split())  # normalizar espacios
        cand = list(rows)
        # paso 1: exacto mes/anio
        if "mesnum = %s AND yy = %s" in s:
            m, y = params
            hit = [r for r in cand if r["mesnum"] == m and r["yy"] == y]
            return hit[0] if hit else None
        # excluir corruptas
        cand = [r for r in cand if r.get("yy") is not None and r.get("mesnum") is not None]
        # datos reales
        if "kprog" in s or "hilado" in s or "pretot" in s:
            cand = [r for r in cand if (r.get("kprog") or 0) > 0 or (r.get("hilado") or 0) > 0 or (r.get("pretot") or 0) > 0]
        # paso 2: restringido a <= mes corriente
        if "yy = %s AND mesnum <= %s" in s and len(params) == 3:
            y1, y2, m = params
            cand = [r for r in cand if (r["yy"] < y1 or (r["yy"] == y2 and r["mesnum"] <= m))]
        cand.sort(key=lambda r: (r["yy"], r["mesnum"], r["id_iniciales"]), reverse=True)
        return cand[0] if cand else None

    monkeypatch.setattr(queries.db, "fetch_one", fake_fetch_one, raising=True)


def test_falta_mes_en_curso_no_agarra_mes_futuro(monkeypatch):
    """Julio ausente, Agosto presente -> debe devolver JUNIO, no Agosto."""
    _install(monkeypatch, [JUNIO, AGOSTO])
    row = queries.iniciales_mes_actual()
    assert row is not None
    assert row["mesnum"] == 6, f"esperaba Junio, devolvió mes {row['mesnum']}"
    assert row["mesnum"] != 8, "NO debe usar Agosto (mes futuro)"


def test_mes_en_curso_presente_tiene_prioridad(monkeypatch):
    """Si Julio existe con datos, se usa Julio (paso 1)."""
    julio = {"id_iniciales": 110, "yy": 2026, "mesnum": 7, "hilado": 1_600_000.0,
             "terminado": 351_849.0, "um": 2.92}
    _install(monkeypatch, [JUNIO, julio, AGOSTO])
    row = queries.iniciales_mes_actual()
    assert row["mesnum"] == 7


def test_fila_corrupta_yy_null_se_ignora(monkeypatch):
    """La fila basura yy=None ('JÔl') no debe devolverse."""
    basura = {"id_iniciales": 999, "yy": None, "mesnum": 7, "hilado": 0.0, "terminado": 0.0}
    _install(monkeypatch, [JUNIO, basura, AGOSTO])
    row = queries.iniciales_mes_actual()
    assert row["yy"] is not None
    assert row["mesnum"] == 6
