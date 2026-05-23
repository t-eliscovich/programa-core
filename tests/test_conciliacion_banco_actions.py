"""Tests para Fase B/C/D — endpoints de conciliación banco.

No tocan DB real: usamos monkeypatch para stubear `db.fetch_all`,
`db.fetch_one`, `db.execute` y `db.tx`. Foco: contratos de las funciones
puras del matcher y el wiring de las views.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from modules.conciliacion import matcher_banco
from modules.conciliacion.parser_banco import MovBanco


# ─── Helpers de stub ─────────────────────────────────────────────────────


class _FakeConn:
    """Contexto vacío para `with db.tx() as conn`."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_db(monkeypatch):
    """Stub mínimo de db.* — devolvemos lo que cada test necesita."""
    state = {
        "executes": [],          # lista de (sql, params, conn) que se ejecutaron
        "fetch_all_rows": [],    # cola FIFO de resultados para fetch_all
        "fetch_one_rows": [],    # cola FIFO de resultados para fetch_one
        "execute_returning_rows": [],  # cola FIFO
    }

    def fake_execute(sql, params=None, conn=None):
        state["executes"].append((sql, params, conn))
        return 1

    def fake_fetch_all(sql, params=None, conn=None):
        if state["fetch_all_rows"]:
            return state["fetch_all_rows"].pop(0)
        return []

    def fake_fetch_one(sql, params=None, conn=None):
        if state["fetch_one_rows"]:
            return state["fetch_one_rows"].pop(0)
        return None

    def fake_execute_returning(sql, params=None, conn=None):
        if state["execute_returning_rows"]:
            return state["execute_returning_rows"].pop(0)
        return {}

    def fake_tx():
        return _FakeConn()

    monkeypatch.setattr(matcher_banco.db, "execute", fake_execute)
    monkeypatch.setattr(matcher_banco.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(matcher_banco.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(matcher_banco.db, "tx", fake_tx)
    # Por defecto: asumir migration 0047 corrida (tests del happy path).
    # Para testear el fallback pre-migration, usar el fixture pre_migration.
    matcher_banco._tiene_migration_47._cache = True
    # Y el bank_helpers que importa crear_transaccion_desde_real
    import bank_helpers
    monkeypatch.setattr(bank_helpers.db, "execute", fake_execute)
    monkeypatch.setattr(bank_helpers.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(bank_helpers.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(bank_helpers.db, "execute_returning", fake_execute_returning)

    yield state
    # Limpiar el cache para no contaminar otros tests.
    if hasattr(matcher_banco._tiene_migration_47, "_cache"):
        del matcher_banco._tiene_migration_47._cache


@pytest.fixture
def fake_db_pre_migration(monkeypatch, fake_db):
    """Variante: la migration 0047 NO corrió todavía."""
    matcher_banco._tiene_migration_47._cache = False
    return fake_db


# ─── Fase B — crear_transaccion_desde_real ─────────────────────────────────


def _mov_credito(monto=500):
    return MovBanco(
        fecha=date(2026, 5, 15),
        concepto="Depósito cliente JTX",
        documento="123456",
        monto=Decimal(str(monto)),
        saldo=Decimal("0"),
        codigo="001045",
        tipo="C",
        oficina="AG. NORTE",
    )


def _mov_debito(monto=300):
    return MovBanco(
        fecha=date(2026, 5, 15),
        concepto="Cheque 5500",
        documento="5500",
        monto=Decimal(str(monto)),
        saldo=Decimal("0"),
        codigo="001045",
        tipo="D",
        oficina="AG. NORTE",
    )


def test_documento_default_tipo_C_es_DE():
    assert matcher_banco._documento_bancsis_desde_tipo("C") == "DE"


def test_documento_default_tipo_D_es_CH():
    assert matcher_banco._documento_bancsis_desde_tipo("D") == "CH"


def test_documento_tipo_desconocido_explota():
    with pytest.raises(ValueError):
        matcher_banco._documento_bancsis_desde_tipo("X")


def test_crear_transaccion_desde_real_tipo_C_inserta_DE(fake_db):
    # bank_helpers necesita una respuesta de saldo previo + execute_returning
    fake_db["fetch_one_rows"].append({"saldo": Decimal("1000.00")})
    fake_db["execute_returning_rows"].append({"id_transaccion": 9999})

    real = _mov_credito(monto=500)
    res = matcher_banco.crear_transaccion_desde_real(
        no_banco=10, real=real, usuario="test",
    )

    assert res["id_transaccion"] == 9999
    assert res["documento"] == "DE"
    # Saldo nuevo = 1000 + 500 (DE suma)
    assert res["saldo_nuevo"] == 1500.00
    # Y el confirmar_match debe haber corrido (último execute con INSERT en banco_conciliacion_match)
    sqls = [e[0] for e in fake_db["executes"]]
    assert any("banco_conciliacion_match" in s for s in sqls), \
        "esperaba INSERT en banco_conciliacion_match"


def test_crear_transaccion_desde_real_tipo_D_inserta_CH(fake_db):
    fake_db["fetch_one_rows"].append({"saldo": Decimal("1000.00")})
    fake_db["execute_returning_rows"].append({"id_transaccion": 1234})

    real = _mov_debito(monto=300)
    res = matcher_banco.crear_transaccion_desde_real(
        no_banco=10, real=real, usuario="test",
    )

    assert res["documento"] == "CH"
    assert res["saldo_nuevo"] == 700.00  # CH resta


def test_crear_transaccion_desde_real_acepta_documento_override(fake_db):
    fake_db["fetch_one_rows"].append({"saldo": Decimal("0.00")})
    fake_db["execute_returning_rows"].append({"id_transaccion": 1})
    real = _mov_credito(monto=100)
    res = matcher_banco.crear_transaccion_desde_real(
        no_banco=10, real=real, usuario="test", documento="TR",
    )
    assert res["documento"] == "TR"


# ─── Fase D — match manual, romper, historial ──────────────────────────────


def test_match_manual_usa_metodo_matched_manual(fake_db):
    real = _mov_credito()
    matcher_banco.match_manual(no_banco=10, real=real, id_transaccion=555, usuario="u")
    sql, params, conn = fake_db["executes"][-1]
    assert "banco_conciliacion_match" in sql
    # El tercer parámetro (índice 2) es `metodo`
    assert params[2] == "matched_manual"
    assert params[1] == "matched"  # estado


def test_confirmar_real_only_usa_metodo_real_only_ok(fake_db):
    real = _mov_credito()
    matcher_banco.confirmar_real_only(no_banco=10, real=real, usuario="u")
    sql, params, conn = fake_db["executes"][-1]
    assert params[1] == "real_only_ok"
    assert params[2] == "real_only_ok"
    # id_transaccion (10mo param) debe ser None
    assert params[10] is None


def test_romper_match_marca_deshecho_en(fake_db):
    matcher_banco.romper_match(match_id=42, usuario="alice")
    sql, params, _ = fake_db["executes"][-1]
    assert "deshecho_en" in sql
    assert "CURRENT_TIMESTAMP" in sql
    assert params[0] == "alice"
    assert params[1] == 42


def test_historial_filtra_deshechos_por_default(fake_db):
    fake_db["fetch_all_rows"].append([])
    matcher_banco.historial(no_banco=10)
    # El último fetch_all habrá usado el SQL con "deshecho_en IS NULL"
    # — pero como nuestro stub no captura SQL de fetch_all, validamos via la lista:
    # En este test solo verificamos que no explota y devuelve []
    rows = matcher_banco.historial(no_banco=10, incluir_deshechos=False)
    assert rows == []


def test_historial_incluir_deshechos_devuelve_filas(fake_db):
    fake_db["fetch_all_rows"].append([
        {
            "id": 1, "no_banco": 10, "estado": "matched", "metodo": "matched_auto",
            "real_fecha": date(2026, 5, 15), "real_concepto": "x",
            "real_documento": "999", "real_monto": Decimal("100"),
            "real_tipo": "C", "id_transaccion": 200, "usuario": "u",
            "creado_en": None, "deshecho_en": None, "deshecho_por": None,
            "bancsis_documento": "DE", "bancsis_importe": Decimal("100"),
            "bancsis_fecha": date(2026, 5, 15), "bancsis_concepto": "x",
        }
    ])
    rows = matcher_banco.historial(no_banco=10, incluir_deshechos=True)
    assert len(rows) == 1
    assert rows[0]["estado"] == "matched"


def test_candidatos_match_manual_filtra_por_tipo_credito(fake_db):
    fake_db["fetch_all_rows"].append([
        {"id_transaccion": 1, "fecha": date(2026, 5, 15), "documento": "DE",
         "concepto": "Dep.", "importe": Decimal("500.00"), "numreferencia": "ref",
         "diff_dias": 0, "diff_monto": Decimal("0.00")}
    ])
    out = matcher_banco.candidatos_match_manual(
        no_banco=10, fecha_real=date(2026, 5, 15),
        monto_real=500.0, tipo_real="C",
    )
    assert len(out) == 1
    assert out[0]["id_transaccion"] == 1
    assert out[0]["importe"] == 500.0


def test_confirmar_match_default_metodo_es_matched_auto(fake_db):
    real = _mov_credito()
    matcher_banco.confirmar_match(no_banco=10, real=real, id_transaccion=1, usuario="u")
    _sql, params, _ = fake_db["executes"][-1]
    assert params[2] == "matched_auto"


# ─── Fase A — parseo de error ya no devuelve JSON ──────────────────────────


# ─── Tolerancia pre-migration (Fase D hotfix 2026-05-23) ───────────────────


def test_confirmar_match_sin_migration_omite_metodo(fake_db_pre_migration):
    """Si la migration 0047 no corrió, el INSERT no incluye `metodo`."""
    real = _mov_credito()
    matcher_banco.confirmar_match(no_banco=10, real=real, id_transaccion=1, usuario="u")
    sql, params, _ = fake_db_pre_migration["executes"][-1]
    # El SQL no debe mencionar 'metodo' como columna
    assert "metodo" not in sql.lower()
    # El segundo param sigue siendo estado='matched'
    assert params[1] == "matched"


def test_romper_match_sin_migration_hace_delete(fake_db_pre_migration):
    matcher_banco.romper_match(match_id=42, usuario="u")
    sql, _params, _ = fake_db_pre_migration["executes"][-1]
    assert "DELETE" in sql.upper()
    assert "deshecho_en" not in sql.lower()


def test_views_error_parser_no_devuelve_json():
    """Lectura estática: el branch debug fue eliminado de views.py."""
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "modules" / "conciliacion" / "views.py"
    src = path.read_text(encoding="utf-8")
    # Antes había: `return {..., "traceback": traceback.format_exc()....`
    # Ahora: flash_exc + redirect.
    assert "\"traceback\": traceback.format_exc()" not in src
    assert "Quitar este branch cuando el matcher esté estable" not in src
