# Programa Core Staff Engineering Principles

Use these principles whenever planning, implementing, or reviewing Programa
Core code.
They are active during the work, not a post-implementation checklist.

## Always-On Standards

- Keep ownership boundaries explicit. API, persistence, domain logic,
  presentation, orchestration, and runtime wiring should each have a clear
  home.
- Prefer cohesive modules with narrow responsibilities over broad utility
  layers or premature frameworks.
- Add abstractions only when they remove meaningful duplication, clarify data
  flow, make testing easier, or protect a real extension point.
- Make data flow visible. Avoid hidden side effects, implicit global coupling,
  and behavior that is hard to trace from inputs to outputs.
- Design for testability as part of the implementation. Do not leave brittle
  code in place and compensate with brittle tests.
- Preserve local conventions unless there is a concrete reason to improve the
  pattern.

## Backend Lens

- Keep Flask route handlers focused on HTTP concerns, permissions, forms, and
  response rendering.
- Keep database lifecycle and low-level query helpers in `db.py`; do not let it
  become a business-service layer.
- Keep domain behavior in focused modules under `modules/<domain>/`, usually
  split between `views.py`, `queries.py`, and narrow service/helper modules.
- Use psycopg2, explicit SQL, migrations in `migrations/`, Ruff, and pytest in
  the repo's established style.
- Pair persistence, schema, financial workflow, or reporting changes with tests
  and migrations when the behavior requires them.
- Preserve accounting invariants around saldos, `mov_doble`, reversos,
  anulaciones, periods, and legacy dBase parity.

## Frontend Lens

- Preserve Flask/Jinja boundaries. Templates render UI; route handlers assemble
  context; query modules own SQL.
- Keep shared layout and UI macros in `templates/base.html`,
  `templates/_ui.html`, `templates/_inputs.html`, and related partials. Do not
  hide module-specific workflow rules in shared templates.
- Keep feature-specific templates under the owning module until reuse is
  proven.
- Treat `static/app.js`, `static/sortable-tables.js`, and CSS as progressive
  enhancement. Core accounting flows should remain server-rendered and
  understandable without a broad client-side state layer.
- Keep Spanish operational copy, dense data tables, and visible financial
  status semantics consistent across modules.

## Coordination

- `software-architect` uses these principles to produce implementation-ready
  plans and name the affected backend, UI, database, integration, and
  operations surfaces.
- `software-engineer` uses these principles while implementing every approved
  plan.
- `backend-staff-reviewer` and `frontend-staff-reviewer` provide
  surface-specific lenses before and during implementation, then review the
  changed surface before completion.
