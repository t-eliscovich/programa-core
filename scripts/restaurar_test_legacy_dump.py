"""Restore the sanitized legacy baseline used by DB integration tests."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extensions import make_dsn, parse_dsn

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DUMP = ROOT / "tests" / "fixtures" / "legacy_minimal_dump.sql"


def _dsn_from_env() -> str:
    if database_url := os.environ.get("DATABASE_URL"):
        return database_url

    required = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise SystemExit(f"Missing DB env vars: {', '.join(missing)}")

    return make_dsn(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def _database_name(dsn: str) -> str:
    try:
        return parse_dsn(dsn).get("dbname", "")
    except Exception:
        return ""


def _assert_test_database(dsn: str, *, allow_reset: bool) -> None:
    if not allow_reset and os.environ.get("PROGRAMA_CORE_ALLOW_TEST_DB_RESET") != "1":
        raise SystemExit(
            "Refusing to reset DB without --allow-reset or "
            "PROGRAMA_CORE_ALLOW_TEST_DB_RESET=1."
        )

    db_name = _database_name(dsn)
    if "test" not in db_name.lower():
        raise SystemExit(
            f"Refusing to restore legacy test dump into non-test database {db_name!r}."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dump",
        type=Path,
        default=DEFAULT_DUMP,
        help="SQL dump path to restore.",
    )
    parser.add_argument(
        "--allow-reset",
        action="store_true",
        help="Allow dropping/recreating test schemas in the target database.",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    dump_path = args.dump.resolve()
    if not dump_path.exists():
        raise SystemExit(f"Legacy test dump not found: {dump_path}")

    dsn = _dsn_from_env()
    _assert_test_database(dsn, allow_reset=args.allow_reset)

    sql = dump_path.read_text(encoding="utf-8")
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print(f"Restored sanitized legacy dump into {_database_name(dsn)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
