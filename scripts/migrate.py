"""Migration runner for Programa Core.

Reads every file in ``migrations/`` that matches ``NNNN_*.{sql,py}`` and
applies anything not yet recorded in ``seguridad.migraciones_aplicadas``.

Conventions:
    - Filenames start with a 4-digit version: ``0001_init.sql``, ``0015_add_xxx.py``.
    - ``.sql`` migrations are executed as-is, each in its own transaction.
      They MUST be idempotent (use ``IF NOT EXISTS``, ``DO $$`` guards) so a
      re-run of a partial failure is safe.
    - ``.py`` migrations must expose ``def run(conn): ...`` — a single function
      that receives an open psycopg2 connection (autocommit OFF) and does its
      work. The runner commits if ``run`` returns cleanly, rolls back on any
      exception.
    - The tracker table sits in ``seguridad`` to keep all meta-tables together.
    - Ordering is purely alphabetical on the filename, which is why the 4-digit
      prefix matters.

Usage:

    python scripts/migrate.py              # apply pending
    python scripts/migrate.py --status     # show applied / pending
    python scripts/migrate.py --dry-run    # print what WOULD run, don't apply
    python scripts/migrate.py --force 0002 # re-apply a specific migration
                                           # (drops its row and runs again)

This is the canonical way to change schema. Never run ad-hoc DDL on prod —
write a migration, commit it, ship it.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Put project root on the path so `import db` works.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

MIGRATIONS_DIR = ROOT / "migrations"
VERSION_RE = re.compile(r"^(\d{4})_[a-zA-Z0-9_]+\.(sql|py)$")

# Marker que una .sql puede poner en su header para correr SIN transacción.
# Necesario para CREATE INDEX CONCURRENTLY, VACUUM, REINDEX CONCURRENTLY,
# ALTER TYPE ... ADD VALUE (en versiones viejas), etc. — todo lo que PG
# rechaza adentro de un BEGIN/COMMIT. Ver migrations/0030 como referencia.
NO_TX_MARKER = "migrate:no-transaction"


# ---------------------------------------------------------------------------
# Tracker table — bootstrap before anything else.
# ---------------------------------------------------------------------------
TRACKER_DDL = """
CREATE SCHEMA IF NOT EXISTS seguridad;

CREATE TABLE IF NOT EXISTS seguridad.migraciones_aplicadas (
    version        varchar(4)  PRIMARY KEY,
    nombre         varchar(200) NOT NULL,
    aplicada_en    timestamp    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    duracion_ms    integer,
    checksum       varchar(64)
);
"""


def _ensure_tracker() -> None:
    """Create the tracker table if missing. Safe to re-run."""
    with db.get_conn() as c:
        try:
            with c.cursor() as cur:
                cur.execute(TRACKER_DDL)
            c.commit()
        except Exception:
            c.rollback()
            raise


def _checksum(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()[:64]


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def discover() -> list[tuple[str, str, Path]]:
    """Return [(version, name, path)] sorted by version."""
    if not MIGRATIONS_DIR.exists():
        return []
    out: list[tuple[str, str, Path]] = []
    for p in sorted(MIGRATIONS_DIR.iterdir()):
        if not p.is_file():
            continue
        m = VERSION_RE.match(p.name)
        if not m:
            continue
        version = m.group(1)
        out.append((version, p.stem, p))
    return out


def applied_set() -> set[str]:
    rows = db.fetch_all("SELECT version FROM seguridad.migraciones_aplicadas")
    return {r["version"] for r in rows}


# ---------------------------------------------------------------------------
# Runners — one per migration type
# ---------------------------------------------------------------------------
def _apply_sql(conn, path: Path) -> None:
    """Execute a .sql file inside the given connection. Caller commits."""
    sql = path.read_text(encoding="utf-8")
    # Use a bare cursor (no parameter substitution — PL/pgSQL format() uses
    # %I / %s which psycopg2 would otherwise try to interpret).
    with conn.cursor() as cur:
        cur.execute(sql)


def _has_no_tx_marker(path: Path) -> bool:
    """¿La migración pide correr SIN transacción?

    Buscamos NO_TX_MARKER en las primeras ~10 líneas — suficiente para que
    quede en el bloque de header y no afecte si aparece dentro de un literal.
    """
    head = path.read_text(encoding="utf-8").splitlines()[:10]
    return any(NO_TX_MARKER in line for line in head)


def _split_sql_statements(sql: str) -> list[str]:
    """Partir un script SQL en statements individuales.

    Sólo necesario para el modo no-transacción: PG rechaza CREATE INDEX
    CONCURRENTLY si comparte un batch multi-statement con otros (la simple
    query protocol los corre dentro de una transacción implícita).
    Mandando cada statement por separado con autocommit=True cada uno corre
    en su propia tx, que es lo que CONCURRENTLY exige.

    Maneja:
        - comentarios ``-- ...`` y ``/* ... */``
        - strings ``'...'`` con escape ``''``
        - identifiers ``"..."`` con escape ``""``
        - dollar-quotes ``$$...$$`` y ``$tag$...$tag$``
    Splits SOLO en ``;`` fuera de esos contextos. Vacíos descartados.
    """
    out: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    dollar_tag_re = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # -- line comment
        if ch == "-" and nxt == "-":
            j = sql.find("\n", i)
            if j == -1:
                buf.append(sql[i:]); i = n
            else:
                buf.append(sql[i:j + 1]); i = j + 1
            continue

        # /* block comment */
        if ch == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                buf.append(sql[i:]); i = n
            else:
                buf.append(sql[i:j + 2]); i = j + 2
            continue

        # 'single-quoted string' con escape ''
        if ch == "'":
            buf.append(ch); i += 1
            while i < n:
                c = sql[i]; buf.append(c); i += 1
                if c == "'":
                    if i < n and sql[i] == "'":
                        buf.append("'"); i += 1
                    else:
                        break
            continue

        # "double-quoted identifier" con escape ""
        if ch == '"':
            buf.append(ch); i += 1
            while i < n:
                c = sql[i]; buf.append(c); i += 1
                if c == '"':
                    if i < n and sql[i] == '"':
                        buf.append('"'); i += 1
                    else:
                        break
            continue

        # $tag$ dollar-quoted body $tag$
        if ch == "$":
            m = dollar_tag_re.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                if end == -1:
                    buf.append(sql[i:]); i = n
                else:
                    buf.append(sql[i:end + len(tag)]); i = end + len(tag)
                continue

        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch); i += 1

    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE = re.compile(r"--[^\n]*")


def _is_only_comments(stmt: str) -> bool:
    """¿El statement quedó vacío después de sacar comentarios?

    Pasa cuando el split deja un trailing chunk que era todo header/footer.
    psycopg2 lo manda y PG no se rompe, pero saltearlo evita ruido en logs.
    """
    s = _COMMENT_BLOCK.sub("", stmt)
    s = _COMMENT_LINE.sub("", s)
    return not s.strip()


def _apply_sql_no_tx(conn, path: Path) -> None:
    """Correr una migración con autocommit=True, statement por statement.

    Asume que el caller ya puso ``conn.autocommit = True``. No usamos
    transacciones — si un statement falla, los anteriores ya están
    persistidos. La migración tiene que ser idempotente (todas nuestras
    .sql lo son: ``IF EXISTS`` / ``IF NOT EXISTS`` / ``DO $$ ... IF NOT
    FOUND``) para que un re-run después de un fallo termine bien.
    """
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        for stmt in _split_sql_statements(sql):
            if _is_only_comments(stmt):
                continue
            cur.execute(stmt)


def _apply_py(conn, path: Path) -> None:
    """Load the file as a module and call its `run(conn)` function."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No pude cargar la migración Python: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "run"):
        raise RuntimeError(f"{path.name} debe exponer una función `run(conn)`")
    mod.run(conn)


def _apply_one(version: str, name: str, path: Path, *, dry: bool) -> None:
    kind = path.suffix.lstrip(".")
    no_tx = kind == "sql" and _has_no_tx_marker(path)
    tag = f"{kind}, no-tx" if no_tx else kind
    # TMT 2026-05-18 — usar ASCII: Windows cp1252 no encodea '→'.
    print(f"  -> [{version}] {name}  ({tag})", end="", flush=True)
    if dry:
        print("  (dry-run, no ejecuto)")
        return

    t0 = time.perf_counter()
    with db.get_conn() as c:
        prev_autocommit = c.autocommit
        try:
            if no_tx:
                # CREATE INDEX CONCURRENTLY & friends: cada statement va
                # afuera de cualquier transacción.
                c.autocommit = True
                _apply_sql_no_tx(c, path)
            elif kind == "sql":
                _apply_sql(c, path)
            elif kind == "py":
                _apply_py(c, path)
            else:
                raise RuntimeError(f"Extensión desconocida: {kind}")

            ms = int((time.perf_counter() - t0) * 1000)
            with c.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seguridad.migraciones_aplicadas
                        (version, nombre, duracion_ms, checksum)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (version) DO UPDATE
                      SET nombre       = EXCLUDED.nombre,
                          aplicada_en  = CURRENT_TIMESTAMP,
                          duracion_ms  = EXCLUDED.duracion_ms,
                          checksum     = EXCLUDED.checksum
                    """,
                    (version, name, ms, _checksum(path.read_bytes())),
                )
            # En autocommit el INSERT ya se persistió; el commit() es no-op.
            # En modo normal sí necesitamos commit explícito.
            if not c.autocommit:
                c.commit()
            print(f"  OK ({ms} ms)")
        except Exception as e:
            try:
                c.rollback()  # no-op si autocommit=True, pero no rompe.
            except Exception:
                pass
            print(f"  FAIL\n      {type(e).__name__}: {e}")
            raise
        finally:
            # Restaurar el estado original antes de devolver al pool —
            # si no, la próxima migración hereda autocommit=True y se
            # comporta raro.
            c.autocommit = prev_autocommit


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_apply(dry: bool) -> int:
    _ensure_tracker()
    already = applied_set()
    todo = [t for t in discover() if t[0] not in already]
    if not todo:
        print("Todas las migraciones están aplicadas. Nada que hacer.")
        return 0
    print(f"Aplicando {len(todo)} migración(es):")
    for version, name, path in todo:
        _apply_one(version, name, path, dry=dry)
    print("\nListo." if not dry else "\nDry-run terminado.")
    return 0


def cmd_status() -> int:
    _ensure_tracker()
    applied = {
        r["version"]: r for r in db.fetch_all(
            "SELECT version, nombre, aplicada_en, duracion_ms "
            "FROM seguridad.migraciones_aplicadas ORDER BY version"
        )
    }
    all_mig = discover()
    if not all_mig:
        print("No hay archivos en migrations/.")
        return 0
    print(f"{'V':<5}  {'estado':<10}  {'nombre':<40}  aplicada_en")
    print("-" * 80)
    for version, name, _ in all_mig:
        row = applied.get(version)
        if row:
            print(f"{version:<5}  {'aplicada':<10}  {name:<40}  "
                  f"{row['aplicada_en']}  ({row['duracion_ms']} ms)")
        else:
            print(f"{version:<5}  {'PENDIENTE':<10}  {name:<40}")
    # Orphans: applied versions with no file on disk (deleted migration!)
    all_versions = {v for v, _, _ in all_mig}
    orphans = set(applied) - all_versions
    if orphans:
        print("\n[!] Versions aplicadas sin archivo en migrations/:")
        for v in sorted(orphans):
            print(f"    {v}  {applied[v]['nombre']}")
    return 0


def cmd_force(version: str) -> int:
    _ensure_tracker()
    target = next((t for t in discover() if t[0] == version), None)
    if not target:
        print(f"No encuentro migrations/{version}_*")
        return 1
    print(f"Forzando re-aplicación de {target[1]}")
    db.execute(
        "DELETE FROM seguridad.migraciones_aplicadas WHERE version = %s",
        (version,),
    )
    _apply_one(*target, dry=False)
    return 0


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Programa Core migration runner")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true",
                   help="Listar migraciones y su estado")
    g.add_argument("--dry-run", action="store_true",
                   help="Mostrar qué se ejecutaría sin aplicar nada")
    g.add_argument("--force", metavar="VERSION",
                   help="Re-aplicar una migración ya aplicada (borra su fila)")
    args = ap.parse_args(argv)

    # Fail fast if DB env vars are missing.
    for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        if not os.environ.get(k):
            print(f"ERROR: falta variable de entorno {k}. Revisá tu .env.")
            return 2

    if args.status:
        return cmd_status()
    if args.force:
        return cmd_force(args.force)
    return cmd_apply(dry=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
