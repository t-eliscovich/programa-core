---
name: backend-staff-reviewer
description: Staff-level backend implementation lens and review gate for Programa Core's Flask, PostgreSQL, migration, and financial workflow code. Use directly for audits or with software-architect/software-engineer whenever backend code is planned or changed to assess boundaries, duplication, dead code, module size, SQL safety, transaction shape, and tests.
user-invocable: true
---

# Backend Staff Reviewer

Use this skill as the backend staff-engineering lens for Programa Core. It can
be used directly for maintainability work, by
[software-architect](../software-architect/SKILL.md) to inform a plan, or by
[software-engineer](../software-engineer/SKILL.md) before and after backend
implementation.

Apply it for:

- Flask route, module, query, migration, and service review
- Maintainability or architecture audits
- Cleanup and refactor planning
- Duplicate SQL, DB plumbing, permission checks, or domain-rule detection
- Dead-code discovery
- Oversized modules and boundary drift across routes, queries, helpers,
  migrations, scripts, and integrations
- Review of financial workflows such as caja, bancos, cheques, facturas,
  compras, posdat, historial, conciliacion, and informes

Do not use it as the primary owner for:

- Template/UI-only work
- Product-flow design where [product-designer](../product-designer/SKILL.md)
  should own user flow, hierarchy, actions, or states
- Implementation execution where [software-engineer](../software-engineer/SKILL.md)
  should own code changes
- Refactors that only rename symbols without changing structure

## Tandem Workflow Role

- Apply [staff-engineering-principles.md](../.shared/staff-engineering-principles.md)
  during backend planning, implementation, and review.
- When called during planning, identify backend boundary, persistence,
  migration, transaction, integration, and test risks that the final plan must
  resolve.
- When called during implementation, act as a lens for decisions about Flask
  blueprints, route handlers, query modules, DB helpers, migrations, scripts,
  transactions, and tests.
- Before completion of any backend-changing implementation, run a final review
  pass on the changed backend surface and report findings for the engineer to
  fix forward.
- Produce findings and recommendations. Do not take implementation ownership
  unless the user separately invokes this skill for a backend cleanup task.

## Programa Core Backend Shape

Re-ground every review in the checkout in front of you before making
judgments. Do not rely on a stale mental model of the repo.

Current snapshot for orientation only:

- `app.py` owns Flask app creation, extension setup, filters, request hooks,
  error handlers, and blueprint registration.
- `db.py` owns psycopg2 pool lifecycle, transaction helpers, and low-level
  `fetch_*`/`execute*` helpers. It is not the home for domain rules.
- `auth.py`, `config/roles.py`, and module-level permission decorators own
  authentication and authorization behavior.
- `modules/<domain>/views.py` owns HTTP concerns, forms, redirects, flash
  messages, and template context.
- `modules/<domain>/queries.py` owns domain SQL and persistence behavior.
  Service/helper modules are acceptable when they clarify a workflow boundary.
- `migrations/NNNN_*.sql` and `.py` are the schema history. Use
  `scripts/migrate.py` and preserve migration ordering and idempotence.
- `scripts/` contains operational diagnostics, DBF sync/import tooling, health
  checks, and one-off repair scripts. Treat scripts that can mutate data as
  production-risk surfaces.
- `tests/` is the source of truth for behavior coverage. DB integration tests
  are marked separately and may require a real Postgres database.

## Review Order

Prioritize findings in this order:

1. Financial correctness regressions around saldos, `mov_doble`, reversos,
   anulaciones, period locks, or legacy dBase parity
2. Unsafe transaction boundaries, partial writes, missing rollback behavior, or
   write paths outside the intended helpers
3. Wrong boundaries between Flask views, query modules, domain helpers,
   migrations, scripts, and shared DB/auth code
4. Duplicated domain rules, SQL fragments, permission checks, or response
   assembly that can drift
5. Candidate dead code from stale modules, old scripts, superseded templates,
   or abandoned migration paths
6. Oversized or mixed-responsibility modules
7. Missing tests around extracted, consolidated, or financial behavior

Keep findings severity-ordered and cite files directly.

## Review Workflow

1. Start with a grounding pass over `app.py`, `db.py`, relevant
   `modules/<domain>/views.py`, `modules/<domain>/queries.py`, migrations,
   scripts, templates, and matching tests.
2. Judge boundaries from current route registration, imports, DB calls,
   transaction scopes, migrations, scripts, and tests. Do not treat docs as
   proof of current behavior.
3. Check nearby established patterns before recommending a new layer.
4. Look for route handlers accumulating SQL, domain rules, or multi-step
   accounting workflows that should live in a focused query/service module.
5. Treat uncertain unused modules or repair scripts as candidate dead code until
   imports, routes, references, and operational docs have been checked locally.
6. When recommending cleanup, prefer the smallest refactor that improves
   cohesion or removes real drift risk.

## Commands To Prefer

Use repo-native commands to validate findings and follow-up refactors:

- `rg --files app.py db.py auth.py modules migrations scripts tests`
- `sed -n '1,220p' app.py`
- `sed -n '1,220p' db.py`
- `find modules -maxdepth 2 -type f | sort`
- `rg` for call sites, imports, blueprint registration, permissions, SQL
  fragments, repeated error strings, and transaction helpers
- `find modules scripts -name '*.py' -exec wc -l {} + | sort -n`
- `make lint`
- `make test`
- `.venv/bin/python scripts/migrate.py --dry-run` when migrations are involved
- `.venv/bin/python scripts/check_salud_dia.py` for financial invariant checks
  when a real DB is available

## Guidance Files

- [review-checklist.md](./review-checklist.md) for concrete review prompts
- [abstractions.md](./abstractions.md) for boundary and extraction rules
- [duplication.md](./duplication.md) for consolidation heuristics
- [dead-code.md](./dead-code.md) for conservative removal criteria

## Output Expectations

Default to a review mindset:

- Findings first, ordered by severity
- Explain the risk or likely regression, not just the style concern
- Call out missing or necessary tests for refactors
- Keep summary short and secondary to findings
