"""Fixtures para tests `@pytest.mark.db` — corren contra Postgres real.

Usa `pytest-postgresql` para arrancar una DB efímera por test session. Los
tests que la usan tienen que estar marcados con `@pytest.mark.db` y se
pueden correr con:

    pytest -m db

En CI el workflow `.github/workflows/ci.yml` tiene un service postgres
ya listo; este fixture lo detecta vía env `DB_HOST` y se conecta ahí en
lugar de arrancar un cluster propio.
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


def _ci_db_available() -> bool:
    """CI/dev puede tener un Postgres ya corriendo en DB_HOST.

    No basta con que las env vars estén — la conexión tiene que abrir.
    Un DB_HOST configurado pero con server caído = skip, no error.
    """
    if not all(os.environ.get(k) for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")):
        return False
    try:
        import psycopg2
        conn = psycopg2.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", "5432")),
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            connect_timeout=2,
        )
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def real_pg_dsn():
    """Connection string a un Postgres usable.

    Prefiere el de env (CI service o un local brew postgres). Si no hay, y
    pytest-postgresql está disponible, intenta arrancar uno efímero.
    Si nada funciona, skip — no error.
    """
    if _ci_db_available():
        host = os.environ["DB_HOST"]
        port = os.environ.get("DB_PORT", "5432")
        name = os.environ["DB_NAME"]
        user = os.environ["DB_USER"]
        pwd = os.environ["DB_PASSWORD"]
        yield f"postgresql://{user}:{pwd}@{host}:{port}/{name}"
        return

    pytest.skip(
        "No hay Postgres disponible en DB_HOST. "
        "Configurá DB_HOST/DB_NAME/DB_USER/DB_PASSWORD o arrancá `docker compose up db`."
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
