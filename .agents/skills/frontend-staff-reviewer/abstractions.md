# Abstractions

Programa Core has server-rendered Flask/Jinja screens, shared UI partials,
Tailwind/CSS, and small static JavaScript enhancements. Prefer strengthening
that shape over adding generic frontend architecture.

## Default Boundaries

- `templates/base.html` owns global shell, sidebar/topbar, theme, and common
  page structure.
- `templates/_ui.html`, `templates/_inputs.html`, `_csv_upload.html`, and
  other root partials own reusable Jinja primitives.
- `modules/<domain>/templates/` owns feature-local screens, forms, tables, and
  workflow copy.
- `static/app.js` and `static/sortable-tables.js` own progressive enhancement,
  not core business correctness.
- `static/tailwind.css` and `static/css/design_system.css` own shared styling.
- `filters.py` and display helpers own formatting semantics.

Do not force every repeated UI detail into a root partial. Module-local UI
should stay local until reuse is real, and shared templates should not become a
home for business workflow logic.

## When To Extract

Extract shared UI when one or more of these is true:

- The same form field, table control, status badge, empty/error state, or action
  group appears in multiple modules with the same meaning.
- Multiple templates repeat the same layout scaffold and the shared shape is
  stable.
- The same formatting, label, or state mapping appears in templates and Python.
- A template is large because it mixes layout, repeated row rendering, and
  feature-specific decision logic.
- A static JS behavior is duplicated across pages and can be written as a
  narrow, progressive enhancement.

## When Not To Extract

Avoid extraction when:

- The UI has one real caller.
- The shared-looking markup has different financial meaning in each module.
- The new macro would need many flags to represent original call sites.
- The extraction would hide destructive-action warnings or permission context.
- The behavior belongs in Python route/query code, not in a template macro or
  JavaScript.

## Preferred Refactor Shapes

- Extract a focused Jinja macro or partial for repeated, stable UI.
- Extract formatting or label logic into Python helpers when templates repeat
  business mappings.
- Keep module-specific screens close to their route and query code.
- Split very large templates into named partials when it improves scanability.
- Keep JavaScript enhancements data-attribute driven and safe when scripts fail.

## Avoid

- Generic macros with unclear contracts
- Catch-all `_components.html` growth
- Flag-heavy macros that erase domain meaning
- Moving domain rules into static JavaScript
- Hiding server-side validation behind client-only behavior
