"""Tests del bridge read-only a formulas_app (modules._lib.formulas_db).

Cubre:
    - init_pool() sin FORMULAS_DATABASE_URL: bridge deshabilitado (pool=None).
    - init_pool() falla al abrir: bridge queda deshabilitado, no levanta.
    - disponible(): refleja el estado del pool.
    - fetch_all / fetch_one con pool=None devuelven [] / None sin I/O.
    - fetch_all / fetch_one con pool válido devuelven los rows.
    - fetch_all / fetch_one ante excepción devuelven [] / None (fail-soft).
    - healthcheck() para visibilidad en /healthz.

Diseño: nunca abrimos una DB real. Mockeamos psycopg2.pool.SimpleConnectionPool
y el módulo `_pool` directamente para simular los dos estados (deshabilitado
y abierto).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from modules._lib import formulas_db


@pytest.fixture(autouse=True)
def _reset_pool():
    """Cada test arranca con _pool=None (clean slate)."""
    formulas_db._pool = None
    yield
    formulas_db._pool = None


# ---------------------------------------------------------------------------
# init_pool
# ---------------------------------------------------------------------------


def test_init_pool_sin_env_var_no_abre_pool(monkeypatch):
    monkeypatch.delenv("FORMULAS_DATABASE_URL", raising=False)
    formulas_db.init_pool()
    assert formulas_db._pool is None
    assert formulas_db.disponible() is False


def test_init_pool_con_env_var_vacia_no_abre_pool(monkeypatch):
    monkeypatch.setenv("FORMULAS_DATABASE_URL", "")
    formulas_db.init_pool()
    assert formulas_db._pool is None


def test_init_pool_con_url_invalida_degrada_sin_levantar(monkeypatch):
    """Si SimpleConnectionPool levanta, init_pool() loguea y deja _pool=None."""
    monkeypatch.setenv("FORMULAS_DATABASE_URL", "postgresql://invalid:5432/x")
    with patch(
        "modules._lib.formulas_db.pool.SimpleConnectionPool",
        side_effect=Exception("conexión rechazada"),
    ):
        formulas_db.init_pool()
    assert formulas_db._pool is None
    assert formulas_db.disponible() is False


def test_init_pool_con_url_valida_abre_pool(monkeypatch):
    fake_pool = MagicMock()
    monkeypatch.setenv("FORMULAS_DATABASE_URL", "postgresql://reader:pw@db/postgres")
    monkeypatch.setenv("FORMULAS_POOL_MIN", "2")
    monkeypatch.setenv("FORMULAS_POOL_MAX", "5")

    with patch("modules._lib.formulas_db.pool.SimpleConnectionPool", return_value=fake_pool) as ctor:
        formulas_db.init_pool()

    ctor.assert_called_once_with(
        minconn=2,
        maxconn=5,
        dsn="postgresql://reader:pw@db/postgres",
    )
    assert formulas_db._pool is fake_pool
    assert formulas_db.disponible() is True


def test_init_pool_es_idempotente(monkeypatch):
    """Re-llamar init_pool() con un pool ya abierto no abre uno segundo."""
    fake_pool = MagicMock()
    formulas_db._pool = fake_pool
    monkeypatch.setenv("FORMULAS_DATABASE_URL", "postgresql://x/y")
    formulas_db.init_pool()
    assert formulas_db._pool is fake_pool  # mismo objeto


# ---------------------------------------------------------------------------
# fetch_all / fetch_one con pool deshabilitado
# ---------------------------------------------------------------------------


def test_fetch_all_sin_pool_devuelve_lista_vacia():
    assert formulas_db._pool is None
    assert formulas_db.fetch_all("SELECT 1") == []


def test_fetch_one_sin_pool_devuelve_none():
    assert formulas_db._pool is None
    assert formulas_db.fetch_one("SELECT 1") is None


def test_healthcheck_sin_pool_devuelve_false():
    assert formulas_db._pool is None
    assert formulas_db.healthcheck() is False


def test_conn_sin_pool_entrega_none():
    with formulas_db._conn() as conn:
        assert conn is None


# ---------------------------------------------------------------------------
# fetch_all / fetch_one con pool activo
# ---------------------------------------------------------------------------


def _wire_fake_pool(rows):
    """Configura un fake pool que al fetchall/fetchone devuelve `rows`."""
    fake_cur = MagicMock()
    fake_cur.fetchall.return_value = rows
    fake_cur.fetchone.return_value = rows[0] if rows else None

    fake_conn = MagicMock()
    # cursor(cursor_factory=...) es un context manager
    fake_conn.cursor.return_value.__enter__.return_value = fake_cur
    fake_conn.cursor.return_value.__exit__.return_value = False

    fake_pool = MagicMock()
    fake_pool.getconn.return_value = fake_conn
    formulas_db._pool = fake_pool
    return fake_cur


def test_fetch_all_con_pool_devuelve_rows():
    rows = [{"a": 1}, {"a": 2}]
    cur = _wire_fake_pool(rows)
    assert formulas_db.fetch_all("SELECT a FROM t", (42,)) == rows
    cur.execute.assert_called_once_with("SELECT a FROM t", (42,))


def test_fetch_one_con_pool_devuelve_primer_row():
    cur = _wire_fake_pool([{"a": 7}])
    assert formulas_db.fetch_one("SELECT a FROM t LIMIT 1") == {"a": 7}
    cur.execute.assert_called_once()


def test_fetch_all_ante_excepcion_devuelve_vacio():
    cur = _wire_fake_pool([])
    cur.execute.side_effect = Exception("boom")
    assert formulas_db.fetch_all("SELECT 1") == []


def test_fetch_one_ante_excepcion_devuelve_none():
    cur = _wire_fake_pool([])
    cur.execute.side_effect = Exception("boom")
    assert formulas_db.fetch_one("SELECT 1") is None


def test_healthcheck_con_pool_ok():
    _wire_fake_pool([(1,)])
    assert formulas_db.healthcheck() is True


def test_healthcheck_con_pool_levanta_devuelve_false():
    cur = _wire_fake_pool([])
    cur.execute.side_effect = Exception("rds caído")
    assert formulas_db.healthcheck() is False
