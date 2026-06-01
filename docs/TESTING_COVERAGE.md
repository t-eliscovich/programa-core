# Testing and coverage

Programa Core uses pytest, Ruff, and coverage.py. The required coverage gate is
100% line and branch coverage for the explicit Python scope in `.coveragerc`.

## Relevant coverage scope

Python line/branch coverage applies to maintained runtime code that is feasible
to test deterministically without a production Postgres snapshot or browser:

- CSV parsing/upload helpers, humanized error handling, exports, extensions,
  role constants, and IP allowlisting
- fail-soft integration helpers such as `modules/_lib/formulas_db.py`
- pure business helpers such as reconciliation matching, recent-items tracking,
  tintura service mapping, Costos OT service math, and 2FA core helpers
- health/diagnostic JSON endpoints whose behavior is deterministic under Flask
  test clients

Python line coverage intentionally excludes large Flask route modules,
SQL-heavy query modules, generated/cached files, templates/static assets, data
snapshots, SQL migrations, archived diagnostics, and one-off repair/debug
scripts. Those surfaces are validated by route smoke tests, template/static
contract tests, migration tests, and DB integration tests where line coverage is
not the meaningful signal.

Jinja templates, static asset references, and route rendering are covered by
smoke/contract tests:

- `tests/test_routes_smoke.py` walks static GET routes with a fake logged-in
  owner user and fails on unexpected 500s.
- `tests/test_static_asset_references.py` verifies template references to
  shared static assets resolve to files in `static/`.

## Local commands

First-time setup:

```bash
make setup
```

Common local coverage gate:

```bash
make test
```

Coverage without DB-marked tests:

```bash
make test-unit
```

DB-backed coverage requires Postgres and the usual test env vars:

```bash
DB_HOST=127.0.0.1 \
DB_PORT=5432 \
DB_NAME=programa_core_test \
DB_USER=app \
DB_PASSWORD=app_local_only \
SECRET_KEY=test-secret-key-must-be-at-least-32-chars-long-okay \
make test-db
```

Full local CI equivalent:

```bash
make ci
```

Ruff remains available as a separate check:

```bash
make lint
```

The current CI workflow runs Ruff as a non-blocking legacy check until the
existing lint backlog is cleaned up. The required gate is pytest + coverage.

Reports are written to:

- terminal: missing lines and branch gaps
- `coverage.xml`
- `htmlcov/index.html`

## 100% policy

`COVERAGE_FAIL_UNDER` defaults to `100`. Do not lower it to merge new code.
When adding a file to the line-coverage scope, add it to `.coveragerc` only with
tests that keep the combined line and branch report at 100%.

If a surface cannot produce meaningful line coverage, document the reason and
cover it through the appropriate smoke, contract, migration, or integration
test instead.

## Skip and xfail policy

Tests should not be hidden with `collect_ignore_glob`. If a test is known stale,
mark it `xfail` with a current reason so pytest output keeps the debt visible.

The current legacy-stub and drift debt is centralized in `tests/conftest.py` as
`LEGACY_STUB_DEBT_FILES` and `KNOWN_TEST_DRIFT_NODEIDS`. Each entry should
eventually move to one of three states:

- updated and passing
- deleted because the behavior is obsolete
- converted to a narrower explicit `xfail` on only the still-invalid cases

DB integration tests are marked `@pytest.mark.db`; CI provides Postgres and
runs them as part of the required gate.
