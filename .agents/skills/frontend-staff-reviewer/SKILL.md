---
name: frontend-staff-reviewer
description: Staff-level UI implementation lens and review gate for Programa Core's Flask/Jinja/Tailwind frontend. Use directly for audits or with software-architect/software-engineer whenever templates, shared UI partials, static JavaScript, CSS, forms, tables, navigation, or operational workflows are planned or changed.
user-invocable: true
---

# Frontend Staff Reviewer

Use this skill as the UI staff-engineering lens for Programa Core. It can be
used directly for frontend maintainability work, by
[software-architect](../software-architect/SKILL.md) to inform a plan, or by
[software-engineer](../software-engineer/SKILL.md) before and after UI
implementation.

Apply it for:

- Jinja template, Tailwind, static JS, form, table, and navigation review
- Maintainability or architecture audits
- Cleanup and refactor planning
- Duplicate UI, copy, macro, table, filter, and state detection
- Dead-code discovery across templates, static assets, and route wiring
- Oversized templates, partials, macros, scripts, or CSS files
- Accessibility and operational clarity risks in Spanish business workflows

Do not use it as the primary owner for:

- Backend-only or schema-only work
- Pure visual exploration where maintainability is not the main goal
- Greenfield product-flow design where [product-designer](../product-designer/SKILL.md)
  should own user flow, hierarchy, actions, or states
- Implementation execution where [software-engineer](../software-engineer/SKILL.md)
  should own code changes
- Refactors that only rename symbols without changing structure

## Tandem Workflow Role

- Apply [staff-engineering-principles.md](../.shared/staff-engineering-principles.md)
  during UI planning, implementation, and review.
- When called during planning, identify template, route/context, shared UI,
  progressive-enhancement, accessibility, and test risks that the final plan
  must resolve.
- When called during implementation, act as a lens for decisions about Jinja
  templates, shared partials/macros, forms, tables, navigation, CSS, static JS,
  and tests.
- Before completion of any UI-changing implementation, run a final review pass
  on the changed UI surface and report findings for the engineer to fix
  forward.
- Produce findings and recommendations. Do not take implementation ownership
  unless the user separately invokes this skill for a UI cleanup task.

## Programa Core UI Shape

Ground the review in the current structure before making judgments.

- `templates/base.html` owns the global shell, sidebar/topbar, theme behavior,
  and shared page frame.
- `templates/_ui.html`, `templates/_inputs.html`, `_informe_frame.html`, and
  related partials own reusable Jinja UI primitives.
- `modules/<domain>/templates/` owns feature-specific screens and forms.
  Feature logic should stay near the owning route until reuse is proven.
- `static/tailwind.css`, `static/css/design_system.css`, `static/app.js`, and
  `static/sortable-tables.js` own shared styling and progressive enhancement.
- `filters.py`, `labels.py`, and Jinja filters/macros shape display semantics
  for numbers, dates, money, labels, and status.
- Native validation is primarily `make lint`, `make test`, route smoke tests,
  and browser/manual checks for dense UI flows. Missing automated coverage is a
  review concern when behavior moves or workflow state changes.

## Review Order

Prioritize findings in this order:

1. UI changes that hide, mislabel, or confuse financial state, destructive
   actions, reversals, pending states, or permission boundaries
2. Wrong route/template/shared-partial/static-asset boundaries
3. Duplicated table, form, empty/error, navigation, macro, or copy logic
4. Progressive enhancement that breaks server-rendered workflows or creates
   inconsistent state
5. Candidate dead code from abandoned templates, static assets, routes, or
   legacy UI fragments
6. Missing tests or smoke coverage around extracted or consolidated behavior

Keep findings severity-ordered and cite files directly.

## Review Workflow

1. Inspect the relevant route, template, shared partial, static JS/CSS, and
   matching tests before proposing structure changes.
2. Check whether a nearby template or macro already establishes a sound
   pattern before inventing a new one.
3. Verify that forms, tables, links, confirmations, and destructive actions
   remain clear in Spanish and keep server-side safeguards visible.
4. Look for repeated page scaffolds, table controls, upload forms, empty/error
   states, labels, and action bars before introducing new abstractions.
5. Treat uncertain unused UI as candidate dead code until route reachability,
   imports/includes, `url_for` references, static asset references, docs, and
   tests have been checked locally.
6. Prefer the smallest refactor that improves cohesion or removes drift risk.

## Commands To Prefer

Use repo-native commands to validate findings and follow-up refactors:

- `rg` for route endpoints, template includes, `url_for`, macros, labels,
  permissions, and duplicate copy
- `find templates modules static -type f | sort`
- `find templates modules -name '*.html' -exec wc -l {} + | sort -n`
- `make lint`
- `make test`
- `.venv/bin/python -m pytest tests/test_routes_smoke.py -q`
- Browser checks against `python run.py` or `./launcher.sh` when visual layout,
  forms, or table interactions matter

## Related Skills

Use these alongside this skill when the task needs them:

- [frontend-design](../frontend-design/SKILL.md) for substantial visual
  implementation or aesthetic direction
- [product-designer](../product-designer/SKILL.md) for flow, hierarchy, action,
  state, and copy decisions
- [web-design-guidelines](../web-design-guidelines/SKILL.md) for focused UI and
  accessibility review

## Guidance Files

- [review-checklist.md](./review-checklist.md) for concrete review prompts
- [abstractions.md](./abstractions.md) for boundary and extraction rules
- [duplication.md](./duplication.md) for consolidation heuristics
- [dead-code.md](./dead-code.md) for conservative removal criteria

## Output Expectations

Default to a review mindset:

- Findings first, ordered by severity
- Explain the user risk or likely regression, not just the style concern
- Call out missing or necessary tests for refactors
- Keep summary short and secondary to findings
