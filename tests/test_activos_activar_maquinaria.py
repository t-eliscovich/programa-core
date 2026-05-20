"""Tests para activos.queries.activar_maquinaria.

Invariantes que verifico (TMT 2026-05-20, pedido dueña):

1. valor_total <= 0 → ValueError
2. vida_util <= 0 → ValueError
3. Anticipos > valor_total → ValueError (los anticipos superan).
4. Anticipos vivos de OTRO proveedor → ValueError (mezcla).
5. Anticipo ya consumido (st != '') → ValueError.
6. Si deuda > 0 y n_cuotas < 1 → ValueError.
7. Si deuda > 0 y falta fecha_primera_cuota → ValueError.
8. Happy path con deuda: UPDATE dolares + INSERT activos + N INSERT posdat + mov_doble.
9. Happy path sin deuda (anticipos = valor): NO crea posdats.
10. Importe por cuota se distribuye: base = round(deuda/N, 2), última cuota
    absorbe el resto del round.

El patrón de stub sigue lo de test_bancos_emitir_cheque.py (sin DB real).
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import date, timedelta

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


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
    def __init__(self):
        # Default fixture: proveedor MY existe, 2 anticipos vivos de $30 + $10.
        self.prov_row = {"id_proveedor": 7, "codigo_prov": "MY", "nombre": "Mayer"}
        self.anticipos = [
            {"id_dolares": 101, "cta": "MY", "importe": 30.0, "st": None},
            {"id_dolares": 102, "cta": "MY", "importe": 10.0, "st": None},
        ]
        self.next_max_num = 500
        self.next_id_activos = 9000
        self.next_id_posdat = 8000
        self.executes: list[tuple] = []
        self.execute_returning_results: list[dict] = []

    # ── stub interface ──────────────────────────────────────────────────
    def fetch_one(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.proveedor where codigo_prov" in s:
            return self.prov_row
        if "max(num)" in s and "scintela.posdat" in s:
            return {"sig": self.next_max_num}
        return None

    def fetch_all(self, sql, params=None, conn=None):
        s = " ".join(sql.split()).lower()
        if "from scintela.dolares" in s and "where id_dolares in" in s:
            ids = list(params or [])
            return [a for a in self.anticipos if a["id_dolares"] in ids]
        return []

    def execute(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        return 1

    def execute_returning(self, sql, params=None, conn=None):
        self.executes.append((sql, tuple(params or ())))
        s = " ".join(sql.split()).lower()
        if "insert into scintela.activos" in s:
            self.next_id_activos += 1
            return {"id_activos": self.next_id_activos}
        if "insert into scintela.posdat" in s:
            self.next_id_posdat += 1
            return {"id_posdat": self.next_id_posdat}
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
    import modules.activos.queries as aq
    monkeypatch.setattr(aq, "asegurar_fecha_abierta", lambda *a, **kw: None)
    # Stub mov_doble.registrar para que no rompa por falta de tabla en test.
    import mov_doble as _md
    monkeypatch.setattr(_md, "registrar", lambda **kw: None)
    return s


def _sql_text(executes):
    return " | ".join(e[0].lower() for e in executes)


# ── Validaciones de inputs ──────────────────────────────────────────────
def test_valor_total_cero_o_negativo(stub):
    from modules.activos import queries as q
    with pytest.raises(ValueError, match="Valor total"):
        q.activar_maquinaria(
            codigo_prov="MY", ids_anticipos=[101], concepto="T1",
            tipo="M", valor_total=0, vida_util_meses=60,
            n_cuotas=1, meses_entre_cuotas=3, fecha_primera_cuota=date.today(),
        )


def test_vida_util_cero(stub):
    from modules.activos import queries as q
    with pytest.raises(ValueError, match="Vida útil"):
        q.activar_maquinaria(
            codigo_prov="MY", ids_anticipos=[101], concepto="T1",
            tipo="M", valor_total=100, vida_util_meses=0,
            n_cuotas=1, meses_entre_cuotas=3, fecha_primera_cuota=date.today(),
        )


def test_proveedor_inexistente(stub):
    from modules.activos import queries as q
    stub.prov_row = None
    with pytest.raises(ValueError, match="no existe"):
        q.activar_maquinaria(
            codigo_prov="ZZZ", ids_anticipos=[], concepto="T1",
            tipo="M", valor_total=100, vida_util_meses=60,
            n_cuotas=1, meses_entre_cuotas=3, fecha_primera_cuota=date.today(),
        )


def test_anticipo_de_otro_proveedor(stub):
    """Anticipos del proveedor OTRO (no MY) → ValueError."""
    from modules.activos import queries as q
    stub.anticipos = [
        {"id_dolares": 101, "cta": "OTRO", "importe": 30.0, "st": None},
    ]
    with pytest.raises(ValueError, match="es del proveedor"):
        q.activar_maquinaria(
            codigo_prov="MY", ids_anticipos=[101], concepto="T1",
            tipo="M", valor_total=100, vida_util_meses=60,
            n_cuotas=1, meses_entre_cuotas=3, fecha_primera_cuota=date.today(),
        )


def test_anticipo_ya_consumido(stub):
    from modules.activos import queries as q
    stub.anticipos = [
        {"id_dolares": 101, "cta": "MY", "importe": 30.0, "st": "B"},
    ]
    with pytest.raises(ValueError, match="ya está consumido"):
        q.activar_maquinaria(
            codigo_prov="MY", ids_anticipos=[101], concepto="T1",
            tipo="M", valor_total=100, vida_util_meses=60,
            n_cuotas=1, meses_entre_cuotas=3, fecha_primera_cuota=date.today(),
        )


def test_anticipos_superan_valor(stub):
    """anticipos $40 vs valor $30 → error (no se pueden activar con
    cambio a favor del proveedor)."""
    from modules.activos import queries as q
    with pytest.raises(ValueError, match="superan el valor"):
        q.activar_maquinaria(
            codigo_prov="MY", ids_anticipos=[101, 102], concepto="T1",
            tipo="M", valor_total=30, vida_util_meses=60,
            n_cuotas=1, meses_entre_cuotas=3, fecha_primera_cuota=date.today(),
        )


def test_deuda_sin_n_cuotas(stub):
    from modules.activos import queries as q
    # anticipos $40, valor $100 → deuda $60. n_cuotas=0 debería fallar.
    with pytest.raises(ValueError, match="cuotas debe ser >= 1"):
        q.activar_maquinaria(
            codigo_prov="MY", ids_anticipos=[101, 102], concepto="T1",
            tipo="M", valor_total=100, vida_util_meses=60,
            n_cuotas=0, meses_entre_cuotas=3, fecha_primera_cuota=date.today(),
        )


def test_deuda_sin_fecha_primera_cuota(stub):
    from modules.activos import queries as q
    with pytest.raises(ValueError, match="primera cuota requerida"):
        q.activar_maquinaria(
            codigo_prov="MY", ids_anticipos=[101, 102], concepto="T1",
            tipo="M", valor_total=100, vida_util_meses=60,
            n_cuotas=4, meses_entre_cuotas=3, fecha_primera_cuota=None,
        )


# ── Happy paths ─────────────────────────────────────────────────────────
def test_happy_path_con_deuda(stub):
    """Ejemplo de la dueña: anticipos $40 + valor $110 → activo + 4 posdats de $17.50 cada uno (deuda $70 / 4)."""
    from modules.activos import queries as q
    r = q.activar_maquinaria(
        codigo_prov="MY", ids_anticipos=[101, 102], concepto="Telar Sulzer",
        tipo="M", valor_total=110, vida_util_meses=60,
        n_cuotas=4, meses_entre_cuotas=3,
        fecha_primera_cuota=date.today() + timedelta(days=90),
    )
    assert r["valor_total"] == 110
    assert r["deuda_total"] == 70
    assert r["n_cuotas"] == 4
    assert r["n_anticipos_consumidos"] == 2
    assert len(r["ids_posdat"]) == 4
    # cuota mensual depreciación = 110/60 ≈ 1.83
    assert round(r["cuota_mensual"], 2) == 1.83
    txt = _sql_text(stub.executes)
    assert "update scintela.dolares" in txt and "set st = " in txt.lower()
    assert "insert into scintela.activos" in txt
    # 4 inserts en posdat
    n_posdat = txt.count("insert into scintela.posdat")
    assert n_posdat == 4, f"esperaba 4 INSERTs en posdat, obtuve {n_posdat}"


def test_happy_path_sin_deuda(stub):
    """Si anticipos = valor exacto, no se crean posdats — sólo activo + consumo."""
    from modules.activos import queries as q
    # anticipos $40 → valor $40 → deuda $0
    r = q.activar_maquinaria(
        codigo_prov="MY", ids_anticipos=[101, 102], concepto="Bomba aux",
        tipo="K", valor_total=40, vida_util_meses=60,
        n_cuotas=0, meses_entre_cuotas=0, fecha_primera_cuota=None,
    )
    assert r["deuda_total"] == 0
    assert r["ids_posdat"] == []
    txt = _sql_text(stub.executes)
    assert "insert into scintela.activos" in txt
    assert "insert into scintela.posdat" not in txt


def test_distribucion_cuotas_redondeo(stub):
    """Deuda $100 en 3 cuotas: 33.33 + 33.33 + 33.34. La última absorbe el round."""
    from modules.activos import queries as q
    # anticipos $0 (no seleccionamos), valor $100 → deuda $100
    r = q.activar_maquinaria(
        codigo_prov="MY", ids_anticipos=[], concepto="Maq sin anticipo",
        tipo="M", valor_total=100, vida_util_meses=60,
        n_cuotas=3, meses_entre_cuotas=3,
        fecha_primera_cuota=date.today() + timedelta(days=90),
    )
    assert len(r["ids_posdat"]) == 3
    # Extraer importes de los INSERTs de posdat (param posición 4)
    importes = []
    for sql, params in stub.executes:
        if "INSERT INTO scintela.posdat" in sql:
            # params: (num, fecha, fechad, prov, importe, concepto, usuario)
            importes.append(float(params[4]))
    assert sum(importes) == pytest.approx(100, abs=0.01)
    assert importes[0] == pytest.approx(33.33, abs=0.01)
    assert importes[-1] == pytest.approx(33.34, abs=0.01)
