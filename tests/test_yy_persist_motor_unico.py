"""Tests del motor único de acumulación YY/RT (TMT 2026-06-10).

Bug original: persistir_acumulacion_yy() leía una columna
scintela.posdat.cuota_diaria que NINGUNA migración creó → UndefinedColumn
silencioso (callers con except:pass) → el persist nunca corrió y el Balance
Pasivos quedaba congelado en el último pin del reconcile (~32k/día de drift),
"arreglándose" con cada sync y rompiéndose después. Estos tests garantizan:
  1. el persist NO depende de columnas inexistentes (usa _resolver_cuotas),
  2. acumula cuota × días hábiles y avanza baseline (idempotente),
  3. la clave canónica YY sobrevive ediciones de concepto,
  4. la suma de cuotas espejo del PRG da 32.000/día (MENU.PRG L283-333).
"""
from datetime import date

import pytest

from modules.posdat import queries as pq


# TMT 2026-07-21 (dueña): acumulación automática YY/RT apagada por default
# (parejo con dBase). Estos tests validan la matemática del motor → flag ON.
@pytest.fixture(autouse=True)
def _acumulacion_activa(monkeypatch):
    monkeypatch.setattr(pq, "ACUMULACION_YY_ACTIVA", True)


# ─── 1. el SQL del persist no referencia la columna fantasma ───────────────

def test_persist_no_usa_columna_fantasma():
    import inspect
    src = inspect.getsource(pq.persistir_acumulacion_yy)
    sel = src[src.index("SELECT"):src.index("FROM scintela.posdat")]
    assert "cuota_diaria" not in sel, (
        "persistir_acumulacion_yy volvió a SELECTear posdat.cuota_diaria — "
        "esa columna NO existe en el schema (bug 2026-06-10: UndefinedColumn "
        "silencioso → Pasivos congelados)."
    )


# ─── 2. acumulación funcional ───────────────────────────────────────────────

def _setup_persist(monkeypatch, rows, provisiones):
    import db as _db
    updates = []

    def fake_fetch_all(sql, params=None, **kw):
        if "FROM scintela.posdat" in sql:
            return [dict(r) for r in rows]
        if "FROM scintela.provisiones" in sql:
            return [dict(p) for p in provisiones]
        return []

    def fake_execute(sql, params=None, **kw):
        if sql.strip().startswith("UPDATE scintela.posdat"):
            updates.append(params)
        return 1

    monkeypatch.setattr(_db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(_db, "fetch_one", lambda *a, **k: {"x": 1})  # baseline col existe
    monkeypatch.setattr(_db, "execute", fake_execute)
    return updates


def test_persist_acumula_cuota_por_dias_habiles(monkeypatch):
    # baseline lunes 2026-06-08, hoy miércoles 2026-06-10 → 2 días hábiles
    updates = _setup_persist(
        monkeypatch,
        rows=[{"id_posdat": 132, "prov": "YY", "concepto": "A,E,C AG,EN,CMB",
               "importe": -17300.0, "baseline_date": date(2026, 6, 8)}],
        provisiones=[{"id_provisiones": 1, "concepto": "A,E,C", "importe": 7700.0,
                      "periodo_aplica": None}],
    )
    n = pq.persistir_acumulacion_yy(hoy=date(2026, 6, 10))
    assert n == 1
    importe, baseline, id_ = updates[0]
    assert importe == pytest.approx(-17300.0 + 7700.0 * 2)  # -1900
    assert baseline == date(2026, 6, 10)
    assert id_ == 132


def test_persist_rt_usa_cuota_hardcodeada_8400(monkeypatch):
    updates = _setup_persist(
        monkeypatch,
        rows=[{"id_posdat": 141, "prov": "RT", "concepto": "",
               "importe": -24850.0, "baseline_date": date(2026, 6, 9)}],
        provisiones=[],  # RT no está en provisiones — fallback 8400
    )
    n = pq.persistir_acumulacion_yy(hoy=date(2026, 6, 10))
    assert n == 1
    assert updates[0][0] == pytest.approx(-24850.0 + 8400.0)


def test_persist_idempotente_baseline_hoy(monkeypatch):
    updates = _setup_persist(
        monkeypatch,
        rows=[{"id_posdat": 1, "prov": "YY", "concepto": "SUELDOS",
               "importe": 100.0, "baseline_date": date(2026, 6, 10)}],
        provisiones=[{"id_provisiones": 2, "concepto": "SUELDOS",
                      "importe": 6000.0, "periodo_aplica": None}],
    )
    n = pq.persistir_acumulacion_yy(hoy=date(2026, 6, 10))
    assert n == 0 and updates == []


def test_persist_fin_de_semana_no_suma(monkeypatch):
    # baseline viernes 2026-06-12, hoy domingo 2026-06-14 → 0 días hábiles
    updates = _setup_persist(
        monkeypatch,
        rows=[{"id_posdat": 1, "prov": "YY", "concepto": "SUELDOS",
               "importe": 100.0, "baseline_date": date(2026, 6, 12)}],
        provisiones=[{"id_provisiones": 2, "concepto": "SUELDOS",
                      "importe": 6000.0, "periodo_aplica": None}],
    )
    n = pq.persistir_acumulacion_yy(hoy=date(2026, 6, 14))
    assert n == 0 and updates == []


# ─── 3. clave canónica (identidad reconcile) ────────────────────────────────

def test_clave_canonica_sobrevive_edicion_de_concepto():
    k1 = pq.clave_canonica_yy("YY", "A,E,C AG,EN,CMB")
    k2 = pq.clave_canonica_yy("YY", "A,E,C aportes y encaje")
    assert k1 == k2 == ("YY", "^A,E,C")


def test_clave_canonica_contains_intereses_e_incobrable():
    assert pq.clave_canonica_yy("YY", "INTERESES") == ("YY", "~INTER")
    assert pq.clave_canonica_yy("YY", "PROV.INCOBRABLE") == ("YY", "~INCOB")


def test_clave_canonica_rt_y_sin_regla():
    assert pq.clave_canonica_yy("RT", "RETENCIONES") == ("RT", "")
    assert pq.clave_canonica_yy("YY", "OTRA COSA") == ("YY", "OTRA COSA")


# ─── 4. paridad de cuotas con MENU.PRG ──────────────────────────────────────

def test_cuotas_dbase_suman_32000_por_dia():
    """MENU.PRG L283-333: SR 3300 + 13 1000 + 14 300 + AB 1300 + SS 2400 +
    A,E,C 7700 + SUELDOS 6000 + ALQUILER 700 + RT 8400 + INCOB 400 +
    JP 200 + INTER 300 = 32.000/día hábil. Si la tabla provisiones de la
    dueña difiere de esto, PC y dBase driftean a diario: este test documenta
    el contrato del PRG."""
    cuotas_prg = {"SR": 3300, "13": 1000, "14": 300, "AB": 1300, "SS": 2400,
                  "A,E,C": 7700, "SUELDOS": 6000, "ALQUILER": 700, "RT": 8400,
                  "INCOB": 400, "JP": 200, "INTER": 300}
    assert sum(cuotas_prg.values()) == 32000
