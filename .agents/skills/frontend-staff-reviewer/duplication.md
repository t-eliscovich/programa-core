# Duplication

Not all duplication is equally expensive. Remove duplication that causes drift,
inconsistent workflow meaning, or review overhead.

## What To Look For

- Repeated page scaffolds, action bars, table wrappers, filters, upload forms,
  and confirmation dialogs
- Repeated loading, empty, pending, warning, or error-state UI
- Repeated Tailwind class blocks across related module templates
- Repeated label/status mappings in templates instead of Python helpers
- Repeated navigation metadata or route-derived labels
- Repeated static JS selectors or event handlers for the same interaction

## How To Classify It

### Incidental duplication

Small repeated syntax with little maintenance cost. Usually leave it alone.

### Structural duplication

The same behavior, copy, or shape transformation appears in multiple places and
must stay consistent. This is the best extraction target.

### Near-duplication with meaningful variants

The screens share a core but intentionally diverge in accounting, permission,
or workflow semantics. Extract only the shared core if the differences stay
explicit.

## Search Tactics

- Start with `rg` over repeated labels, button text, permission names, table
  headings, and state names.
- Search for repeated macros/includes and large repeated Tailwind blocks.
- Compare similar module templates before assuming they need identical behavior.
- Use lint and route smoke output as clues for accidental drift, not as proof
  that an extraction is required.

## Preferred Consolidation

- Shared Jinja macro or partial with explicit parameters
- Shared Python formatter/label helper for repeated business mappings
- Shared static JS behavior behind data attributes
- Module-level partial when reuse is only local to one domain

## Avoid Over-Correcting

- Do not merge flows whose financial or permission meaning differs.
- Do not create macros with many boolean flags or mode strings.
- Do not move critical business behavior into client-side JavaScript.
- Do not erase useful module naming just to reduce line count.
