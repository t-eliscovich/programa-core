---
name: frontend-design
description: Create distinctive, production-grade frontend interfaces with high design quality for Programa Core. Use when implementing new screens, redesigning existing templates, or when the task requires more than functional correctness — it requires visual and UX quality.
user-invocable: true
subagent-type: general-purpose
---

# Frontend Design

Use this skill when implementation requires genuine design quality — not just
working code, but interfaces that feel intentional and polished.

## The Core Constraint

Avoid generic "AI slop" aesthetics. The result should look like a human made
a deliberate choice — not like a template was applied.

## Design Thinking First

Before touching code, answer:

- **Purpose**: What is the user trying to accomplish on this screen?
- **Tone**: What feeling should the interface convey? (efficient, trustworthy, calm, precise)
- **Constraints**: What data, actions, and states must be present?
- **Differentiation**: What makes this screen feel intentional rather than assembled?

## Programa Core Design Context

The existing UI uses:
- **Color palette**: sky/slate (Tailwind) with dark mode support — preserve this
- **Layout**: collapsible sidebar with localStorage state, action bar pattern
- **Typography**: pre-compiled Tailwind CSS at `static/tailwind.css` — no npm build step
- **JavaScript**: vanilla JS only (`static/app.js`, `static/sortable-tables.js`) — no React, no npm
- **Templates**: Jinja2 in `templates/` with shared partials (`_ui.html`, `_inputs.html`, etc.)
- **Language**: Spanish — all labels, buttons, placeholders, and error messages in Spanish

Work with this foundation, not against it. Extend patterns that already exist
in `_ui.html` and `_inputs.html` before inventing new ones.

## Frontend Aesthetics Guidelines

### Typography

- Use fonts that have character. The existing Tailwind config is the starting
  point — but when adding new display text, choose weights and sizes
  deliberately.
- Size hierarchy must be obvious. Users should know at a glance what is a
  heading, a label, a value, and supporting text.
- Do not use font size alone to convey hierarchy — weight, color, and spacing
  all contribute.

### Color & Theme

- The sky/slate palette is the foundation. Accents should come from within
  that system unless there is a strong reason (e.g., a status color that needs
  to stand out against both light and dark backgrounds).
- Use semantic color — success, warning, danger, neutral — consistently. Do
  not use raw Tailwind color names for status indicators.
- Dark mode is enabled by default. Every new element must work in both modes.

### Motion

- Transitions should be fast (100–200ms) and purposeful.
- Use CSS transitions on hover states, focus rings, and disclosure panels.
- Do not animate for decoration. Animate to communicate state change.

### Spatial Composition

- Generous whitespace communicates that information is organized, not packed.
- Group related items visually. Separate unrelated items clearly.
- Tables and lists should have consistent row heights and readable line spacing.
- Forms should have a clear visual flow — top to bottom, label above input or
  aligned left at consistent width.

### Backgrounds & Details

- Cards and panels should have a clear visual boundary — border, shadow, or
  background contrast.
- Use `ring` utilities for focus states consistently across all interactive elements.
- Error states, empty states, and loading states deserve the same design
  attention as the happy path.

## Jinja2 Template Patterns

When building new screens:

```html
{# Extend the base layout #}
{% extends "base.html" %}

{% block content %}
{# Use existing UI partials from _ui.html and _inputs.html #}
{% from "_ui.html" import card, badge, action_bar %}
{% from "_inputs.html" import field, text_input, select_input %}
{% endblock %}
```

- Check `_ui.html` and `_inputs.html` for existing macros before building new HTML
- Use `templates/<module>/` directories for module-specific templates
- Partials that are reused across modules belong in `templates/` root

## Anti-Patterns to Avoid

- Generic color schemes (all-gray, all-blue with no accent hierarchy)
- Tables with no visual breathing room
- Forms with inconsistent label alignment
- Buttons with no clear primary/secondary distinction
- Error messages that look identical to normal text
- Empty states with no explanation or next action
- Status badges that are not accessible (color alone, no text/icon)

## Validation

Before marking a design task complete:

1. Open the page in `make run` (`http://localhost:5000`)
2. Toggle dark mode — verify both modes look correct
3. Check all interactive states: hover, focus, active, disabled, error
4. Check the page at narrow viewport (mobile-ish width) — the sidebar collapses
5. Verify Spanish text fits in all labels without truncation
