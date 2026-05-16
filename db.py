"""Single data layer. No SQL anywhere else in the app except modules/*/queries.py.

Conventions:
    - psycopg2 pool opened once at app start.
    - RealDictCursor — rows are dicts.
    - %s placeholders always. Never string concatenation for values.
    - Helpers accept optional conn= for callers that hold a transaction.
    - Queries slower than SLOW_MS (default 200) are logged to stderr.
"""
import logging
import os
import time
from contextlib import contextmanager

from psycopg2 import pool
from psycopg2.extras import RealDictCursor

_log = logging.getLogger("programa_core.db")
SLOW_MS = int(os.environ.get("DB_SLOW_MS", "200"))


def _t(sql: str, params, started: float) -> None:
    """Log a slow query. Truncate the SQL to keep the log readable."""
    ms = (time.perf_counter() - started) * 1000
    if ms >= SLOW_MS:
        one_line = " ".join(sql.split())[:180]
        _log.warning("slow %.0fms  %s", ms, one_line)

_pool: pool.SimpleConnectionPool | None = None


def init_pool() -> None:
    """Create the connection pool. Called once from create_app()."""
    global _pool
    if _pool is not None:
        return
    _pool = pool.SimpleConnectionPool(
        minconn=int(os.environ.get("DB_POOL_MIN", "1")),
        maxconn=int(os.environ.get("DB_POOL_MAX", "10")),
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        options="-c search_path=scintela,seguridad,public",
    )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def get_conn():
    """Borrow a connection from the pool and return it when done.

    NOTE: this does NOT auto-commit. Callers that do INSERT/UPDATE/DELETE
    must either:
        - use execute() / execute_returning() / tx() — which commit
          for you, or
        - call conn.commit() themselves before exit.
    Otherwise psycopg2 rolls the implicit transaction back when the
    connection returns to the pool. This has bitten us before; see the
    2026-04-16 session notes in the programa-core skill.
    """
    if _pool is None:
        init_pool()
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)


@contextmanager
def tx():
    """Multi-statement transaction context manager.

    Usage:

        with db.tx() as conn:
            db.execute("UPDATE factura SET abono = abono + %s WHERE id_factura = %s",
                       (importe, id_f), conn=conn)
            db.execute("INSERT INTO chequesxfact (...) VALUES (...)",
                       params, conn=conn)

    Commits on clean exit, rolls back on any exception. Use this whenever
    you have more than one write and they must all succeed together (cheque
    application, compra + retención, anulaciones, etc.). Every helper here
    accepts a conn= kwarg — pass the one yielded by tx() to them and they
    will NOT commit on their own.
    """
    if _pool is None:
        init_pool()
    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def _exec(cur, sql, params):
    """Cur.execute wrapper que evita pasarle tupla vacia a psycopg2.

    Si pasas `()` o `[]`, psycopg2 igual procesa la SQL buscando placeholders
    `%`, y un porcentaje literal sin escapar (incluso dentro de comentarios SQL
    `-- ...`) tira `IndexError: tuple index out of range`. Pasar `None`
    desactiva la substitucion y se vuelve safe. TMT 2026-05-13.
    """
    if params is None or (hasattr(params, "__len__") and len(params) == 0):
        cur.execute(sql)
    else:
        cur.execute(sql, params)


def fetch_all(sql: str, params: tuple | dict | None = None, conn=None) -> list[dict]:
    """SELECT helper — returns list of dicts."""
    t0 = time.perf_counter()
    try:
        if conn is not None:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _exec(cur, sql, params)
                return list(cur.fetchall())
        with get_conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
            _exec(cur, sql, params)
            return list(cur.fetchall())
    finally:
        _t(sql, params, t0)


def fetch_one(sql: str, params: tuple | dict | None = None, conn=None) -> dict | None:
    t0 = time.perf_counter()
    try:
        if conn is not None:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _exec(cur, sql, params)
                row = cur.fetchone()
                return dict(row) if row else None
        with get_conn() as c, c.cursor(cursor_factory=RealDictCursor) as cur:
            _exec(cur, sql, params)
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        _t(sql, params, t0)


def execute(sql: str, params: tuple | dict | None = None, conn=None) -> int:
    """Write helper. Returns rowcount. Commits when we own the connection."""
    t0 = time.perf_counter()
    try:
        if conn is not None:
            with conn.cursor() as cur:
                _exec(cur, sql, params)
                return cur.rowcount
        with get_conn() as c:
            try:
                with c.cursor() as cur:
                    _exec(cur, sql, params)
                    rc = cur.rowcount
                c.commit()
                return rc
            except Exception:
                c.rollback()
                raise
    finally:
        _t(sql, params, t0)


def execute_returning(sql: str, params: tuple | dict | None = None, conn=None) -> dict | None:
    """Write helper for INSERT/UPDATE/DELETE ... RETURNING.

    Returns a single row as dict and COMMITS when we own the connection.
    Use this instead of fetch_one() for INSERT RETURNING — fetch_one() does
    NOT commit, so the write would be rolled back when the connection returns
    to the pool.
    """
    t0 = time.perf_counter()
    try:
        if conn is not None:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                _exec(cur, sql, params)
                row = cur.fetchone()
                return dict(row) if row else None
        with get_conn() as c:
            try:
                with c.cursor(cursor_factory=RealDictCursor) as cur:
                    _exec(cur, sql, params)
                    row = cur.fetchone()
                    result = dict(row) if row else None
                c.commit()
                return result
            except Exception:
                c.rollback()
                raise
    finally:
        _t(sql, params, t0)
