"""Tests del batch 2026-05-20.

Cubre las queries que cambiaron en este push:
  - posdat.queries.buscar(tab='posdatados'|'yy') — split por prov='YY'.
  - dolares.queries.anticipos_pendientes_por_proveedor(tipos_filter=['U']).
  - cartera.queries.aging_totales — net (facturas − cheques).
  - activos.queries.editar_tipo / reordenar.

Patrón: stub mock estilo test_activos_activar_maquinaria.py — sin DB real.
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import date

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ── Stub básico ───────────────────────────────────────────────────────
class _Cur:
    def __init__(self, parent):
        self.parent = parent

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.parent.executes.append((sql, tuple(params or ())))


class _Conn:
    def __init__(self, parent):
        self.parent = parent

    def cursor(self, **kw):
        return _Cur(self.parent)


class _DBStub:
    """Stub configurable: el test setea `responses` para fetch_one/fetch_all."""
    def __init__(self):
        self.executes: list[tuple] = []
        self.fetch_one_responses: list = []
        self.fetch_all_responses: list = []
        self.execute_returning_results: list = []
        self.params_log: list[tuple] = []

    def fetch_one(self, sql, params=None, conn=None):
        self.params_log.append((sql.strip().split()[0:6], params))
        if self.fetch_one_responses:
            return self.fetch_one_responses.pop(0)
        return None

    def fetch_all(self, sql, params=None, conn=None):
        self.params_log.append((sql.strip().split()[0:6], params))
        if self.fetch_all_responses:
            return self.fetch_all_responses.pop(0)
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        if self.execute_returning_results:
            return self.execute_returning_results.pop(0)
        return {}

    @contextlib.contextmanager
    def tx(self):
        yield _Conn(self)


@pytest.fixture
def stub(monkeypatch):
    import db
    s = _DBStub()
    monkeypatch.setattr(db, "fetch_one", s.fetch_one)
    monkeypatch.setattr(db, "fetch_all", s.fetch_all)
    monkeypatch.setattr(db, "execute", s.execute)
    monkeypatch.setattr(db, "execute_returning", s.execute_returning)
    monkeypatch.setattr(db, "tx", s.tx)
    import periodo_guard
    monkeypatch.setattr(periodo_guard, "asegurar_fecha_abierta", lambda *a, **kw: None)
    return s


# ── posdat tabs ───────────────────────────────────────────────────────
def test_posdat_buscar_tab_posdatados_excluye_yy(stub):
    """tab='posdatados' debe pasar el SQL con `<> 'YY'` y `= 'posdatados'`."""
    from modules.posdat import queries as q
    stub.fetch_all_responses = [[]]  # rows vacío, OK
    q.buscar(tab="posdatados")
    # Inspeccionar params del fetch_all principal.
    main_call = stub.params_log[0]
    params = main_call[1]
    assert params["tab"] == "posdatados", "tab debe llegar como 'posdatados'"


def test_posdat_buscar_tab_yy_solo_yy(stub):
    from modules.posdat import queries as q
    stub.fetch_all_responses = [[]]
    q.buscar(tab="yy")
    params = stub.params_log[0][1]
    assert params["tab"] == "yy"


def test_posdat_buscar_tab_invalida_normaliza_a_posdatados(stub):
    """tab='BASURA' o vacío → default 'posdatados'."""
    from modules.posdat import queries as q
    stub.fetch_all_responses = [[]]
    q.buscar(tab="basura")
    # tab_norm es lowercase. La condición CASE WHEN del SQL hace match
    # contra 'yy'/'posdatados' — si tab no matchea ninguno, ambos lados
    # del OR son FALSE → query no devuelve nada (no rompe el contrato).
    params = stub.params_log[0][1]
    assert params["tab"] == "basura"  # se normaliza a lower pero no se coerce


# ── posdat resumen ────────────────────────────────────────────────────
def test_posdat_resumen_tab_default_posdatados(stub):
    from modules.posdat import queries as q
    stub.fetch_one_responses = [{"total_abierto": 0, "partidas_abiertas": 0}]
    r = q.resumen()
    assert r["total_abierto"] == 0
    assert r["partidas_abiertas"] == 0


# ── dolares filtrados por tipo ────────────────────────────────────────
def test_dolares_anticipos_filter_U(stub):
    from modules.dolares import queries as q
    stub.fetch_all_responses = [[]]
    q.anticipos_pendientes_por_proveedor(tipos_filter=["U"])
    params = stub.params_log[0][1]
    assert params["tipos_norm"] == ["U"]


def test_dolares_anticipos_filter_H(stub):
    from modules.dolares import queries as q
    stub.fetch_all_responses = [[]]
    q.anticipos_pendientes_por_proveedor(tipos_filter=["H"])
    params = stub.params_log[0][1]
    assert params["tipos_norm"] == ["H"]


def test_dolares_anticipos_sin_filter(stub):
    """Sin filter → tipos_norm=None (devuelve todos)."""
    from modules.dolares import queries as q
    stub.fetch_all_responses = [[]]
    q.anticipos_pendientes_por_proveedor()
    params = stub.params_log[0][1]
    assert params["tipos_norm"] is None


def test_dolares_anticipos_normaliza_a_1_char(stub):
    """tipos_filter=['UU','HHH'] → corta a 1 char."""
    from modules.dolares import queries as q
    stub.fetch_all_responses = [[]]
    q.anticipos_pendientes_por_proveedor(tipos_filter=["UU", "H", "Q"])
    params = stub.params_log[0][1]
    assert params["tipos_norm"] == ["U", "H", "Q"]


# ── cartera aging totales (BRUTO + sobrepagos) ────────────────────────
# TMT 2026-05-24 — Dueña: /cartera total == Balance Subtotal Cartera.
# Formula nueva: total = TOTF + TOTC (sum de saldos facturas con sobrepagos
# + cheques en cartera). Buckets son aging de facturas SOLO; sum(buckets)
# == saldo_facturas (TOTF), no `total`.
def test_cartera_aging_totales_balance_cartera(stub):
    """total = TOTF + TOTC (= Balance Subtotal Cartera)."""
    from modules.cartera import queries as q
    stub.fetch_one_responses = [{
        "b0_30": 1000.0, "b31_60": 500.0, "b61_90": 200.0, "b90_plus": 100.0,
        "saldo_facturas": 1800.0,           # TOTF
        "cheques_en_cartera": 300.0,        # TOTC
        "sobrepagos": 0.0,
        "total": 2100.0,                    # = TOTF + TOTC
        "n_facturas": 50, "n_clientes": 10,
    }]
    r = q.aging_totales()
    assert r["total"] == 2100.0
    assert r["saldo_facturas"] == 1800.0
    assert r["cheques_en_cartera"] == 300.0
    # Sum buckets == TOTF (aging de facturas, no incluye cheques).
    s_buckets = sum(r[k] for k in ("b0_30", "b31_60", "b61_90", "b90_plus"))
    assert abs(s_buckets - r["saldo_facturas"]) < 0.005


def test_cartera_aging_totales_buckets_son_aging_facturas(stub):
    """Buckets son aging de facturas — NO se netean cheques (TMT 2026-05-24).
    Antes los cheques se descontaban del bucket más joven; ahora cheques
    viven en su KPI propio (TOTC) y los buckets son intactos."""
    from modules.cartera import queries as q
    stub.fetch_one_responses = [{
        "b0_30": 1000.0, "b31_60": 500.0, "b61_90": 0.0, "b90_plus": 0.0,
        "saldo_facturas": 1500.0,
        "cheques_en_cartera": 600.0,
        "sobrepagos": 0.0,
        "total": 2100.0,
        "n_facturas": 1, "n_clientes": 1,
    }]
    r = q.aging_totales()
    # Buckets sin tocar — cheques NO se restan.
    assert r["b0_30"] == 1000.0
    assert r["b31_60"] == 500.0
    assert r["cheques_en_cartera"] == 600.0


def test_cartera_aging_totales_sobrepagos(stub):
    """Sobrepagos (saldo<0) ya vienen netados en saldo_facturas porque la
    query no filtra por signo (igual que TOTF)."""
    from modules.cartera import queries as q
    stub.fetch_one_responses = [{
        "b0_30": 1000.0, "b31_60": 500.0, "b61_90": 0.0, "b90_plus": 0.0,
        "saldo_facturas": 1300.0,  # 1500 brutos + sobrepagos -200
        "cheques_en_cartera": 200.0,
        "sobrepagos": -200.0,
        "total": 1500.0,  # 1300 + 200
        "n_facturas": 1, "n_clientes": 1,
    }]
    r = q.aging_totales()
    assert r["sobrepagos"] == -200.0
    assert r["total"] == 1500.0


# ── activos.editar_tipo ───────────────────────────────────────────────
def test_activos_editar_tipo_happy_path(stub):
    from modules.activos import queries as q
    # 1ª respuesta: UPDATE returning 1.
    # 2ª respuesta: fetch_one(SELECT … categoria_orden) → tipo='T'.
    stub.fetch_one_responses = [
        {"id_activos": 5, "tipo": "T", "concepto": "Lote Sur",
         "categoria_orden": 1},
    ]
    r = q.editar_tipo(5, "T")
    assert r["id_activos"] == 5
    assert r["tipo"] == "T"
    assert r["categoria_orden"] == 1
    assert r["categoria_label"] == "Terrenos"


def test_activos_editar_tipo_vacio_raises(stub):
    from modules.activos import queries as q
    with pytest.raises(ValueError, match="requerido"):
        q.editar_tipo(5, "")


def test_activos_editar_tipo_no_existe_raises(stub):
    from modules.activos import queries as q
    # UPDATE devuelve 0 filas (no existe el id).
    s_orig = stub.execute
    def _exec(sql, params=None, conn=None):
        s_orig(sql, params, conn)
        return 0
    stub.execute = _exec
    import db
    db.execute = _exec  # patchear directamente
    with pytest.raises(ValueError, match="no existe"):
        q.editar_tipo(99999, "T")


# ── activos.reordenar ─────────────────────────────────────────────────
def test_activos_reordenar_asigna_orden_secuencial(stub, monkeypatch):
    from modules.activos import queries as q
    monkeypatch.setattr(q, "_tiene_orden_manual", lambda: True)
    n = q.reordenar([10, 20, 30])
    assert n == 3
    # Cada UPDATE debe tener (orden, id) con orden incremental.
    updates = [e for e in stub.executes if "UPDATE scintela.activos" in e[0]]
    assert len(updates) == 3
    for i, (_sql, params) in enumerate(updates):
        assert params == (i + 1, [10, 20, 30][i])


def test_activos_reordenar_sin_columna_raises(stub, monkeypatch):
    from modules.activos import queries as q
    monkeypatch.setattr(q, "_tiene_orden_manual", lambda: False)
    with pytest.raises(ValueError, match="0037"):
        q.reordenar([10, 20])


def test_activos_reordenar_dedupe(stub, monkeypatch):
    """Si vienen ids duplicados, dedupe preservando primer aparición."""
    from modules.activos import queries as q
    monkeypatch.setattr(q, "_tiene_orden_manual", lambda: True)
    n = q.reordenar([10, 20, 10, 30, 20])
    assert n == 3  # 10, 20, 30 únicos


def test_activos_reordenar_vacio(stub, monkeypatch):
    from modules.activos import queries as q
    monkeypatch.setattr(q, "_tiene_orden_manual", lambda: True)
    assert q.reordenar([]) == 0
