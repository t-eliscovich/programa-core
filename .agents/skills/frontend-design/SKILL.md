---
name: frontend-design
description: Execute substantial Programa Core UI implementation once product flow and engineering plan are clear. Use for Flask/Jinja/Tailwind visual restructuring, dense operational screens, dashboards, forms, tables, and shared UI polish that must preserve Spanish financial workflow semantics and existing repo conventions.
license: Complete terms in LICENSE.txt
---

This skill supports production-grade Programa Core frontend implementation.
It is an execution aid, not a substitute for product or engineering planning.
Use it after the workflow, states, and affected templates/static assets are
clear.

Programa Core is a Spanish-first Flask/Jinja/Tailwind application for dense
financial operations. Frontend work should improve clarity, trust, speed, and
maintainability without changing business semantics.

## Implementation Priorities

- Preserve existing product semantics around saldos, reversos, anulaciones,
  pending states, permissions, dates, source/freshness, and audit trails.
- Keep UI quiet, utilitarian, table-forward, and fast to scan.
- Use Spanish operational terminology already present in the repo.
- Prefer server-rendered Jinja and progressive enhancement over broad
  client-side state.
- Reuse `templates/base.html`, `templates/_ui.html`, `templates/_inputs.html`,
  module-local templates, `static/tailwind.css`, `static/css/design_system.css`,
  and existing JavaScript patterns before inventing new structure.
- Preserve the established sky/slate visual language and dark-mode behavior
  unless the approved design explicitly changes it.
- Keep destructive, reversal, and financial submit actions visibly guarded.
- Make empty, error, pending, stale, success, and reconciliation states explicit
  where operators make decisions.

## Design Bar

High-quality Programa Core UI is not decorative. It is:

- Dense but organized
- Consistent with nearby screens
- Clear about totals, dates, affected records, and status
- Accessible through native form controls, readable contrast, focus states, and
  meaningful labels
- Stable under long Spanish labels, wide money values, legacy identifiers, and
  partial data
- Easy to review because template responsibility, route context, and static
  behavior remain obvious

Avoid landing-page composition, novelty typography, heavy animation, decorative
background effects, broad palette shifts, and one-off component systems unless
the user explicitly approves that direction for a non-operational surface.

## Workflow

1. Re-ground in the relevant route, template, shared partial, static CSS/JS, and
   tests before editing.
2. Confirm the intended workflow state and the exact business meaning of labels,
   totals, badges, actions, and form fields.
3. Implement the smallest UI change that improves the approved surface.
4. Check long labels, empty/error states, mobile/tablet constraints when
   relevant, and dark mode for shared surfaces.
5. Run the validation named by `software-engineer` or the approved plan. For
   UI-only changes, prefer `make lint`, `make test`, focused route smoke tests,
   and browser verification when layout or interaction changed.

## Related Skills

- Use [product-designer](../product-designer/SKILL.md) when workflow, hierarchy,
  copy, actions, or states are not settled.
- Use [frontend-staff-reviewer](../frontend-staff-reviewer/SKILL.md) for final
  maintainability and boundary review of templates/static assets.
- Use [software-engineer](../software-engineer/SKILL.md) as the implementation
  owner for plan-driven code changes.
