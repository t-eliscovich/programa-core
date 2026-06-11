# Backend Abstractions

Rules for when to extract shared logic and when to leave duplication in place,
applied to Programa Core's Python/Flask modular monolith.

## Default Boundaries

- `app.py` — Flask factory, blueprint registration, extension init. No business logic.
- `db.py` — psycopg2 pool, `get_conn()`, `tx()`. No application-level queries.
- `bank_helpers.py` — financial document sign rules, movimiento_bancario insertion,
  saldo recomputation. The source of truth for bank-side accounting rules.
- `caja_helpers.py` — cash register sign rules and saldo recomputation. Same rule.
- `modules/<name>/__init__.py` — routes, helper functions, and queries for one domain.
- `templates/<name>/` — Jinja2 templates for the module. No Python logic.
- `migrations/` — raw SQL, one numbered file per schema change.
- `tests/` — pytest, real DB where behavior requires it.

## When To Extract

Extract shared logic into a helper or cross-cutting module when:

- The same SQL fragment (especially sign rules or saldo computation) appears in
  two or more modules — consolidate in the appropriate `_helpers.py`.
- A route handler exceeds ~80 lines of substantive logic — split into a route
  function (HTTP concerns only) and a helper function (domain logic).
- The same validation pattern (date parsing, importe normalization, concepto
  cleaning) is repeated in three or more modules — extract into `filters.py`
  or a new shared helper.
- A cross-cutting concern (e.g., permission check, audit log write) is
  re-implemented per-module — extract once and call it.
- A module's `__init__.py` has grown past ~500 lines — consider a `helpers.py`
  or `queries.py` sub-module alongside `__init__.py`.

## When Not To Extract

Do not extract when:

- Only one module calls the logic. A helper with a single caller is indirection
  with no payoff.
- The "duplication" is incidental — two modules do similar things for
  structurally different reasons (different DB tables, different sign rules,
  different rollback semantics).
- The abstraction would require passing so much context that the call site is
  harder to read than the inlined code.
- The shared helper would need to handle special cases for each caller —
  prefer leaving the logic in each caller and revisiting when a third caller
  appears.

## Preferred Refactor Shapes

When extraction is justified:

- **Helper function in the same module**: `def _build_movimiento(...)` — start
  here before reaching for a shared module.
- **New `queries.py` alongside `__init__.py`**: for modules where SQL queries
  have grown large and benefit from isolation from route handlers.
- **New entry point in `bank_helpers.py` or `caja_helpers.py`**: only for
  logic that truly belongs to the financial accounting layer, not module-specific
  presentation or validation.
- **New `shared/` or `helpers/` module at the package root**: only when three
  or more modules share the same logic and it does not belong in an existing
  helper. This is a high bar — avoid premature shared modules.

## Avoid

- Wrapper functions that just rename or passthrough an existing helper.
- Abstract base classes or mixins for what is essentially shared utility code.
- A generic `utils.py` that collects unrelated helpers — prefer
  purpose-named helpers (`date_utils.py`, `formatting.py`).
- Extracting DB query logic into a data-access layer that mirrors the module
  structure — keep queries close to the routes that use them unless the module
  is too large.
