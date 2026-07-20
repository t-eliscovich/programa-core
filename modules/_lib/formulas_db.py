"""Pool read-only contra la DB de formulas_app.

formulas_app vive en el MISMO cluster RDS que Programa Core, pero en una
DB distinta (`postgres` vs `intela`). Por eso no podemos usar el pool de
`db.py` — necesita otra conexión. Latencia esperada: <10 ms (mismo VPC,
mismo cluster).

Convenciones duras:
    - NUNCA escribir desde acá. El rol DB es SELECT-only por contrato; este
      módulo NO expone execute/commit. Si necesitás escribir, eso vive en
      formulas_app (otra app, otra responsabilidad).
    - Si FORMULAS_DATABASE_URL no está seteada, init_pool() no abre nada y
      cada helper devuelve []/None con un log a WARNING. Programa Core sigue
      vivo aunque formulas_app esté caída o este bridge no esté configurado.
    - Fail-soft: cualquier excepción se loguea y devuelve []/None — el host
      nunca se rompe por culpa del bridge.

Env vars que lee:
    FORMULAS_DATABASE_URL  postgresql://programa_core_reader:...@host/postgres?sslmode=require
    FORMULAS_POOL_MIN      default 1
    FORMULAS_POOL_MAX      default 4
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager

from psycopg2 import pool
from psycopg2.extras import RealDictCursor

_log = logging.getLogger("programa_core.formulas_db")
# ThreadedConnectionPool (2026-07-18): el warmup y los ThreadPool de las
# vistas pueden tocar formulas desde hilos — Simple NO era thread-safe.
_pool: pool.ThreadedConnectionPool | None = None


def init_pool() -> None:
    """Inicializa el pool si FORMULAS_DATABASE_URL está seteada.

    Llamar una vez desde create_app(), después de db.init_pool().
    Idempotente: re-llamar no abre un segundo pool.
    """
    global _pool
    if _pool is not None:
        return
    url = os.environ.get("FORMULAS_DATABASE_URL", "").strip()
    if not url:
        _log.info("FORMULAS_DATABASE_URL vacío — bridge a formulas_app deshabilitado")
        return
    try:
        _pool = pool.ThreadedConnectionPool(
            minconn=int(os.environ.get("FORMULAS_POOL_MIN", "1")),
            maxconn=int(os.environ.get("FORMULAS_POOL_MAX", "4")),
            dsn=url,
        )
        _log.info("formulas_db pool inicializado")
    except Exception as e:
        _log.warning("formulas_db init_pool falló: %s — bridge deshabilitado", e)
        _pool = None


def disponible() -> bool:
    """True si el pool está abierto. No hace I/O."""
    return _pool is not None


@contextmanager
def _conn():
    if _pool is None:
        yield None
        return
    c = _pool.getconn()
    try:
        yield c
    finally:
        _pool.putconn(c)


def fetch_all(sql: str, params: tuple | list = ()) -> list[dict]:
    """SELECT múltiple. Devuelve [] si el bridge no está activo o falla."""
    if _pool is None:
        return []
    try:
        with _conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception as e:
        _log.warning("formulas_db.fetch_all falló: %s", e)
        return []


def fetch_one(sql: str, params: tuple | list = ()) -> dict | None:
    """SELECT de una fila. Devuelve None si el bridge no está activo o falla."""
    if _pool is None:
        return None
    try:
        with _conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchone()
    except Exception as e:
        _log.warning("formulas_db.fetch_one falló: %s", e)
        return None


def healthcheck() -> bool:
    """SELECT 1. True si el pool y la DB responden. Útil en /healthz."""
    if _pool is None:
        return False
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("SELECT 1")
            return cur.fetchone() is not None
    except Exception as e:
        _log.warning("formulas_db.healthcheck falló: %s", e)
        return False
