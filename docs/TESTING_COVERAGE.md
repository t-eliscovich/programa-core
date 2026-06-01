# Testing and coverage

Programa Core uses pytest, Ruff, and coverage.py. The coverage gate is staged:
the environment reports line and branch coverage now, and `COVERAGE_FAIL_UNDER`
is the ratchet knob that moves the repo toward 100%.

## Relevant coverage scope

Python coverage applies to maintained runtime and operational code:

- root Flask/runtime modules such as `app.py`, `auth.py`, `db.py`, filters,
  parsers, helpers, side effects, and accounting workflow helpers
- `config/`
- `modules/`
- maintained operational scripts under `scripts/`, including migration runner,
  DBF import/sync, snapshots, monthly jobs, lint/check scripts, and health
  diagnostics

Python coverage intentionally excludes tests, generated/cached files,
templates/static assets, data snapshots, SQL migrations, archived diagnostics,
and one-off repair/debug scripts. SQL migrations are validated by migration and
DB integration tests, not per-line Python coverage.

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
COVERAGE_FAIL_UNDER=0 make ci
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

## Ratchet policy

`COVERAGE_FAIL_UNDER` starts at `0` so the first implementation pass can record
the real baseline without blocking CI setup. Raise it only after the report is
green at the new value. The final target is 100% line and branch coverage for
the relevant Python scope above.

Do not lower the ratchet to merge new code. Add tests or narrow a justified
exclusion in `.coveragerc`.

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
