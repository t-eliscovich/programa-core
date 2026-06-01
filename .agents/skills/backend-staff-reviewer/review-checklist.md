# Backend Review Checklist

Use this checklist to review Programa Core backend code with a staff-level
maintainability lens.

## Boundary Checks

- Are Flask view functions doing SQL, accounting orchestration, or parser work
  that belongs in `queries.py` or a focused service/helper?
- Is `db.py` carrying feature-specific behavior instead of low-level pool and
  query helpers?
- Are module query files mixing unrelated workflows or hiding side effects?
- Are migrations ordered, idempotent, and compatible with the current database
  state?
- Are mutating scripts explicit about risk, target database, and transaction
  behavior?
- Are permission strings and role config changes coordinated with routes and
  templates?

## Correctness Checks

- Do writes happen inside one transaction when the user-visible operation must
  be atomic?
- Are reversos/anulaciones linked through `mov_doble` where the history UI
  depends on it?
- Are saldo recomputations anchored and scoped?
- Are `stat`/estado filters consistent with the intended report/listing
  semantics?
- Are closed periods enforced server-side, not only in the UI?
- Are legacy dBase parity assumptions checked before changing reports?

## Duplication Checks

- Is the same SQL predicate, date rule, permission check, or accounting delta
  repeated in multiple modules?
- Are similar route handlers recreating the same validation, response shaping,
  or transaction sequence with only minor variation?
- Is the same domain rule implemented both in a template/JavaScript path and in
  backend query code?

## Cohesion Checks

- Does the file have more than one clear responsibility?
- Is a route mixing transport concerns, validation, DB access, and domain
  decisions?
- Would a new engineer struggle to explain why this module owns the behavior in
  one sentence?

Likely review hotspots include caja, bancos, cheques, compras, facturas,
posdat, historial, conciliacion, informes, DBF sync/import scripts, and
integration readers. Treat them as hotspots, not automatic findings.

## Test Checks

- If route or DB logic is extracted, is there behavior-level coverage that
  still proves the original contract?
- If shared helpers are introduced, do tests cover both the shared core and the
  meaningful variants?
- Are migration, financial workflow, and report changes protected where
  regressions would be easy to miss?
- Do tests make architectural intent clearer, or do they only lock in an
  incidental helper shape?

## Reporting Standard

- Prefer concrete findings over broad "clean this up" feedback.
- Cite the module and the specific structural problem.
- Recommend the smallest change that removes real correctness or maintenance
  risk.
