# Frontend Review Checklist

Use this checklist to review Programa Core UI code with a staff-level
maintainability lens.

## Boundary Checks

- Is a route template carrying workflow logic that should live in the Flask
  route, query module, or a Python helper?
- Is module-specific behavior being pushed into root templates or shared macros
  before reuse is proven?
- Is static JavaScript doing validation, permission, or accounting work that
  must be enforced server-side?
- Is a shared partial carrying too many unrelated responsibilities?
- Is CSS or Tailwind usage drifting into one-off patterns that duplicate an
  existing shared primitive?

## Interaction Checks

- Are primary, secondary, and destructive actions visually and semantically
  clear?
- Are reversar, anular, depositar, aplicar, transferir, and other high-risk
  actions protected by confirmation and server-side checks?
- Do dense tables remain scannable with sorted headers, totals, status labels,
  and currency/date formatting intact?
- Are empty, error, pending, stale, and success states explicit where users make
  operational decisions?

## Duplication Checks

- Is the same table, form, upload, confirmation, status badge, or action bar
  recreated across modules?
- Are related templates repeating large class literals or copy blocks instead
  of sharing a narrow partial?
- Are route handlers, templates, and JavaScript all transforming the same state
  separately?

## Cohesion Checks

- Does the template have one clear job?
- Is it mixing presentation, data shaping, domain decisions, and progressive
  enhancement?
- Would a new engineer struggle to explain why this markup lives where it does?

Likely review hotspots include dashboard, informes, bancos, caja, cheques,
compras, facturas, posdat, conciliacion, admin_dbase, and shared templates.
Treat them as hotspots, not automatic findings.

## Test Checks

- If UI logic is extracted, is there unit, route, smoke, or browser coverage
  that still proves the user-visible behavior?
- If duplicated paths are consolidated, do tests cover both the shared core and
  the meaningful variants?
- Are form and route behaviors protected by tests or explicit manual validation
  where automation is not in place?

## Reporting Standard

- Prefer concrete findings over broad "clean this up" feedback.
- Cite the module/template and the specific structural problem.
- Recommend the smallest change that removes real user or maintenance risk.
