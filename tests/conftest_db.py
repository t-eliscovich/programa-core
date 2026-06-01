"""Fixtures para tests `@pytest.mark.db` — corren contra Postgres real.

Los tests que la usan tienen que estar marcados con `@pytest.mark.db` y se
pueden correr con:

    pytest -m db

El fixture detecta un Postgres real via env `DB_HOST`, pero solo corre si esa
DB tiene restaurado el dump legacy que las migraciones esperan. Para una DB de
test vacia, usar:

    make restore-test-db
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    from pytest_postgresql import factories  # noqa: F401
    PYTEST_POSTGRESQL_AVAILABLE = True
except ImportError:
    PYTEST_POSTGRESQL_AVAILABLE = False


ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"
REQUIRED_LEGACY_TABLES = (
    "seguridad.rol",
    "seguridad.usuario",
    "seguridad.permiso",
    "scintela.factura",
    "scintela.cheque",
    "scintela.cliente",
)


def _configured_dsn() -> str | None:
    if not all(os.environ.get(k) for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")):
        return None
    host = os.environ["DB_HOST"]
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ["DB_NAME"]
    user = os.environ["DB_USER"]
    pwd = os.environ["DB_PASSWORD"]
    return f"postgresql://{user}:{pwd}@{host}:{port}/{name}"


def _missing_legacy_tables(dsn: str) -> list[str] | None:
    """Return missing base tables, or None when the DB cannot be reached.

    Programa Core migrations are overlays on the restored dBase dump. A blank
    Postgres database is reachable, but it is not a valid target for the
    migration/integration suite.
    """
    try:
        import psycopg2

        conn = psycopg2.connect(dsn, connect_timeout=2)
    except Exception:
        return None
    try:
        with conn.cursor() as cur:
            missing: list[str] = []
            for table_name in REQUIRED_LEGACY_TABLES:
                cur.execute("SELECT to_regclass(%s)", (table_name,))
                if cur.fetchone()[0] is None:
                    missing.append(table_name)
            return missing
    finally:
        conn.close()


def _ready_db_dsn() -> str | None:
    """CI/dev puede tener un Postgres ya corriendo en DB_HOST.

    No basta con que las env vars estén ni con que la conexión abra: las
    migraciones actuales esperan el dump legacy restaurado.
    """
    dsn = _configured_dsn()
    if not dsn:
        return None
    missing = _missing_legacy_tables(dsn)
    if missing is None:
        return None
    if missing:
        pytest.skip(
            "Postgres disponible, pero no tiene el dump base legacy requerido "
            "para migraciones. Restauralo con `make restore-test-db`; "
            f"faltan {', '.join(missing)}."
        )
    return dsn


@pytest.fixture(scope="session")
def real_pg_dsn():
    """Connection string a un Postgres usable.

    Prefiere el de env. Si no hay DB, o si está vacía y no tiene el dump
    legacy restaurado, skip — no error.
    """
    dsn = _ready_db_dsn()
    if dsn:
        yield dsn
        return

    pytest.skip(
        "No hay Postgres disponible en DB_HOST. "
        "Configurá DB_HOST/DB_NAME/DB_USER/DB_PASSWORD con una DB que tenga "
        "restaurado el dump legacy (`make restore-test-db` en una DB test)."
    )


@pytest.fixture
def real_db_conn(real_pg_dsn):
    """Conexión psycopg2 contra la DB real. Rollbackea al final."""
    import psycopg2
    conn = psycopg2.connect(real_pg_dsn)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def migrated_db(real_pg_dsn, monkeypatch):
    """DB con todas las migraciones aplicadas.

    Monkey-patchea el runner para que lea del cluster que controla este
    fixture, corre migrate.py, y deja la DB con schema completo.
    """
    monkeypatch.setenv("DATABASE_URL", real_pg_dsn)
    # Parseamos la URL para exponer DB_HOST/... que es lo que db.py espera
    from urllib.parse import urlparse
    u = urlparse(real_pg_dsn)
    monkeypatch.setenv("DB_HOST", u.hostname or "127.0.0.1")
    monkeypatch.setenv("DB_PORT", str(u.port or 5432))
    monkeypatch.setenv("DB_NAME", u.path.lstrip("/"))
    monkeypatch.setenv("DB_USER", u.username or "")
    monkeypatch.setenv("DB_PASSWORD", u.password or "")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-must-be-at-least-32-chars-long-okay")

    # Re-importar db y migrate para que agarren las env vars nuevas.
    import importlib

    import db
    importlib.reload(db)
    from scripts import migrate
    importlib.reload(migrate)

    # Aplicar migraciones.
    migrate.main([])  # el runner acepta argv; [] = default
    yield real_pg_dsn
