# Programa Core Design Context

Read this file before designing a major Programa Core flow or critiquing a
large surface. Use `README.md`, `docs/PLAN_UX_PANTALLAS.md`, and relevant
module docs as supporting sources when a request needs more detail.

## Product Posture

- Build for Intela's internal operating team replacing a long-lived
  dBase/Clipper system.
- Optimize for speed, trust, auditability, and dense financial operations.
- Default to desktop-first, with mobile support for review and urgent actions.
- The UI language is Spanish and should preserve the business terms users know.
- Prefer quiet, utilitarian, table-forward surfaces over marketing-style
  layouts.

## Core Experience Principles

1. Show operational state before visual flourish.
2. Make totals, balances, source, and status easy to verify.
3. Separate read, edit, confirm, execute, and reverse actions.
4. Keep destructive or financial actions visibly guarded.
5. Design for partial legacy data, imports, reconciliation, and delayed sources.
6. Make audit trails and provenance legible where trust matters.

## Surface Map

- Auth and permissions: login, users, roles, two-factor, Google auth
- Operations: menu, action bar, caja, bancos, cheques, facturas, compras,
  cobranzas, retenciones, capital, retiros, posdat, proveedores, clientes
- Analysis: dashboard, informes, balance, flujo, cartera, ventas, gastos,
  estado de cuenta, stock
- Integration/admin: admin_dbase, Asinfo, formulas_app/costos_ot, SRI,
  conciliacion, health/diagnostics
- Shared shell: sidebar, topbar, sortable tables, dark mode, shared form and UI
  partials

## Shell Expectations

- Desktop: persistent sidebar, compact top action bar, dense tables, clear
  section grouping, and visible totals/status.
- Tablet/mobile: preserve critical actions, collapse dense tables carefully, and
  avoid hiding confirmation or reversal context.
- High-risk workflows must keep user, permission, affected record, amount,
  date, source, and expected result visible before submission.

## Workflow Expectations

- First screen after login should lead directly to work, not explanation.
- Every financial workflow needs clear before/after state, confirmation, error,
  success, and audit implications.
- Reversar/anular actions should read as serious, traceable operations.
- Reconciliation surfaces should make unmatched, suggested, confirmed, and
  ignored states distinct.
- Imports and external-source data should show freshness or unavailability
  without blocking unrelated work.

## Skill Sequencing

1. `product-designer` for design conversation, critique, and handoff
2. `frontend-staff-reviewer` for template/static UI maintainability
3. `backend-staff-reviewer` for route/query/workflow implications
4. `frontend-design` for visual execution when a substantial UI is built
5. `web-design-guidelines` for standards and accessibility review
