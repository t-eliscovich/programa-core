# Duplication

Not all duplication is equally expensive. Remove duplication that creates
drift, inconsistent accounting behavior, or review overhead.

## What To Look For

- Repeated SQL predicates for active/anulada states, date windows, balances, or
  owner/account filters
- Repeated transaction sequences for reversos, anulaciones, `mov_doble`, caja,
  bancos, cheques, compras, facturas, or posdat
- Repeated permission checks, flash/error handling, and redirect patterns
- Repeated DB pool/session access beyond `db.py` helpers
- Repeated import/parser/matcher logic across CSV, XLSX, DBF, conciliacion, and
  Asinfo/formulas integrations
- Repeated report calculations split between routes, templates, JavaScript, and
  query modules

## How To Classify It

### Incidental duplication

Small repeated syntax with little maintenance cost. Usually leave it alone.

### Structural duplication

The same behavior or financial contract appears in multiple places and must
stay consistent. This is the best extraction target.

### Near-duplication with meaningful variants

The flows share a core but intentionally diverge in legacy, bank, provider,
permission, or reporting rules. Extract only the shared core if the differences
remain explicit.

## Search Tactics

- Start with `rg` over repeated SQL fragments, permission names, route endpoint
  names, flash messages, and helper names.
- Search for repeated `db.fetch_`, `db.execute`, `with db.tx()`, `url_for`,
  `requiere_permiso`, and status predicates before inventing a new layer.
- Compare nearby modules before assuming similar flows need identical behavior.
- Use lint and test output as clues for accidental drift, not as proof that an
  extraction is required.

## Preferred Consolidation

- Shared SQL predicate/helper with a clear business name
- Shared parser, matcher, mapper, or validation helper
- Shared domain function behind an explicit module interface
- Template macro or partial only when the UI behavior and copy are truly shared
- File split that groups related workflow behavior under a clearer module

## Avoid Over-Correcting

- Do not abstract every small route helper.
- Do not merge flows whose accounting, reversal, or permission semantics differ.
- Do not introduce a generic registry before multiple real use cases exist.
- Do not erase useful module naming just to reduce line count.
