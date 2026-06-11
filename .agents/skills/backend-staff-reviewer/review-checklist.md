# Backend Review Checklist

Use this checklist to review Programa Core backend code with a staff-level
maintainability lens.

## Boundary Checks

- Are route handlers in `modules/<name>/__init__.py` doing inline SQL, sign
  computations, or saldo recalculations that should delegate to `bank_helpers`,
  `caja_helpers`, or a module-level helper function?
- Is `db.py` being bypassed — raw psycopg2 connections opened outside
  `db.get_conn()` or `db.tx()`?
- Is `bank_helpers.signo_documento`, `insert_movimiento_bancario`, or
  `recompute_saldos_desde` being reimplemented inside a module instead of called?
- Is `caja_helpers` sign logic being duplicated in module code?
- Are cross-cutting concerns (auth, filters, concepto parsing) being
  re-implemented per-module instead of using `auth.py`, `filters.py`,
  `concepto_parser.py`?
- Are `templates/` Jinja2 templates accumulating business logic (computations,
  formatting decisions) that belongs in the Python layer?
- Are module helpers growing so large that they should be split into focused
  sub-modules or extracted into a shared helper?

## Duplication Checks

- Is the same SQL query (or near-identical query) written in multiple modules?
- Are multiple modules building the same response dict shape independently?
- Are similar route handlers recreating the same auth checks, form validation,
  or error handling with only minor variation?
- Is the same `COALESCE(stat, '') NOT IN ('X', 'Y')` filter (or its absence)
  inconsistent across SUM queries that should all exclude anuladas?
- Is the same date-parsing, importe-rounding, or concepto-formatting logic
  repeated across modules?

## Cohesion Checks

- Does the module file mix routing concerns, DB access, domain rules, and
  presentation logic without clear separation?
- Would a new engineer struggle to explain why this helper function lives
  where it does in one sentence?
- Is a single `__init__.py` file over ~500 lines with no obvious sub-file
  decomposition?
- Is `app.py` accumulating per-module configuration or business logic that
  belongs in the module's blueprint setup?

Likely review hotspots in the current repo include modules with heavy financial
calculations (bancos, caja, cartera, cobranzas, facturas, informes), any new
module that reimplements reverso logic, and migration files that alter columns
involved in saldo recomputation. Treat them as hotspots, not automatic findings.

## Migration Checks

- Does the migration file follow the `NNNN_*.sql` naming convention?
- Does the migration handle both `UP` and `DOWN` (or is it intentionally
  irreversible with a comment explaining why)?
- If a column involved in `recompute_saldos_desde` or `recompute_saldo_caja`
  is altered, is there a data migration or recomputation step included?
- Is the migration safe to run against a live database (no full-table locks on
  large tables without a comment acknowledging the risk)?

## Test Checks

- If route or DB logic is extracted, is there behavior-level coverage that
  still proves the original contract?
- If shared helpers are introduced, do tests cover both the shared core and the
  meaningful variants?
- Are reverso flows tested end-to-end (insert → reverse → verify saldo net zero)?
- Are sign-convention edge cases (CH vs NC, E vs S) explicitly tested?
- Do tests make architectural intent clearer, or do they only lock in an
  incidental helper shape?

## Reporting Standard

- Prefer concrete findings over broad "clean this up" feedback.
- Cite the module file and the specific structural problem.
- Recommend the smallest change that removes real maintenance risk.
