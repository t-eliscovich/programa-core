# Abstractions

Programa Core is a Flask modular monolith with explicit SQL and strong
accounting invariants. Prefer strengthening the current shape over adding
generic architecture.

## Default Boundaries

- `app.py` owns Flask app creation, extension setup, request hooks, error
  handlers, filters, and blueprint registration.
- `db.py` owns psycopg2 pool lifecycle, `tx()`, and low-level DB helpers.
- `auth.py` and `config/roles.py` own authentication, sessions, roles, and
  permission semantics.
- `modules/<domain>/views.py` owns HTTP forms, redirects, flash messages,
  permissions, and template context.
- `modules/<domain>/queries.py` owns domain SQL, persistence, and read-model
  assembly.
- Narrow `service.py` or helper modules are appropriate when a workflow is too
  large for a view or query file but still belongs to one domain.
- `migrations/` owns schema history.
- `scripts/` owns operational, import, repair, and diagnostic workflows.
- `tests/` owns behavior protection.

Do not let `db.py`, app-root helpers, shared templates, or catch-all scripts
become dumping grounds for domain behavior.

## When To Extract

Extract shared logic when one or more of these is true:

- The same validation, permission, SQL fragment, response shaping, or DB
  orchestration appears in multiple modules.
- A route file is accumulating domain rules or multi-step accounting workflow
  coordination.
- `db.py` or an app-root helper is starting to carry feature-specific behavior.
- A module mixes route handling, SQL, parsing, reporting, and side effects in a
  way that obscures transaction behavior.
- A pure transformation, matcher, parser, mapper, or query helper would make
  the main flow easier to read and test.
- A boundary is materially present in code and tests, rather than only implied
  by comments or future plans.

## When Not To Extract

Avoid extraction when:

- The logic has one real caller and is clear in place.
- The abstraction would only wrap Flask, Jinja, psycopg2, or SQL with
  pass-through code.
- The shared-looking code encodes different financial, legacy, or permission
  rules.
- The new layer would hide transaction ordering or make rollback behavior less
  obvious.
- The boundary is not real yet and moving code would create an empty shell.

## Preferred Refactor Shapes

- Extract focused helpers for repeated validation, SQL predicates, mapping, or
  parser logic.
- Move growing domain logic out of `views.py` into `queries.py` or a narrow
  domain service.
- Split a large module by workflow when each workflow has clear entrypoints and
  tests.
- Centralize repeated permission, flash/error, or metadata assembly only when
  the shared behavior is truly stable.
- Keep migration and repair scripts explicit; abstractions must not hide data
  mutation risk.

## Avoid

- Generic base services or repository layers by default
- Catch-all `utils.py`, `helpers.py`, or `service.py` growth
- Hiding accounting deltas or reverse-link behavior behind vague abstractions
- Mixing template rendering concerns with DB transaction lifecycle
- Building architecture-shaped scaffolding without enforcing behavior
