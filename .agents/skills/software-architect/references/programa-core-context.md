# Programa Core Context

Use this file to ground plans in the repo's actual shape before asking the user
to fill gaps.

## Current Repo Shape

- `app.py` is the Flask app factory and blueprint registration point.
- `db.py` owns psycopg2 pool lifecycle, transaction helpers, and low-level DB
  helpers.
- `modules/<domain>/` holds Flask blueprints, `views.py`, `queries.py`,
  templates, and occasional focused services/helpers.
- `templates/` holds global layout and shared Jinja partials.
- `static/` holds Tailwind/CSS, shared JavaScript, charts, and images.
- `migrations/` holds ordered SQL/Python migrations run by `scripts/migrate.py`.
- `scripts/` holds diagnostics, DBF sync/import, repair, deploy, and recurring
  operational tasks.
- `tests/` holds unit and integration tests. DB integration tests are marked and
  may require a real Postgres database.

## Current Application Reality

- Programa Core is a Flask + PostgreSQL rewrite of Intela's legacy
  dBase/Clipper system.
- The app is operational and Spanish-first, with modules for login, menu,
  dashboard, informes, facturas, cheques, retenciones, compras, provisiones,
  bancos, caja, capital, clientes, proveedores, proformas, posdat, activos,
  stock, historial, bitacora, conciliacion, SRI, Asinfo, and formulas_app
  integration.
- Financial correctness depends on server-side workflow logic, transaction
  boundaries, saldos, `mov_doble`, reversos/anulaciones, period locks, and
  dBase parity for reports.
- External/source-of-truth data includes Postgres `scintela.*`, DBF/legacy
  inputs, Asinfo via Metabase, formulas_app via read-only Postgres, and SRI
  integration.

## Planning Bias For Programa Core

- Treat Programa Core as a modular monolith by default.
- Prefer module boundaries inside the existing Flask app before proposing a
  separate deployable.
- Keep backend plans aligned with clear boundaries between Flask views,
  query/domain modules, DB helpers, migrations, scripts, and integrations.
- Keep UI plans aligned with Jinja templates, shared partials, static assets,
  and server-rendered financial workflows.
- Preserve explicit SQL and operational scripts unless a change clearly reduces
  risk or drift.
- Treat legacy parity and auditability as product requirements, not optional
  implementation detail.

## Documents To Read Early

Read these before making high-level architecture decisions:

1. `README.md`
2. `TESTING_PLAN.md`
3. Relevant docs under `docs/`
4. Relevant route, query, template, migration, script, and test files near the
   requested feature
