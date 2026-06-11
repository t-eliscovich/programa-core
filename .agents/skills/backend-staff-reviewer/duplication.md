# Backend Duplication

Guidelines for identifying and consolidating duplication in Programa Core's
Python/Flask codebase.

## Three Types of Duplication

### 1. Incidental Duplication — Leave It Alone

Two modules do something that looks similar but for structurally different
reasons: different tables, different sign conventions, different rollback
semantics. Extracting a shared function would require so many parameters or
branches that the shared version is harder to read than each original.

Examples: two modules that both compute a running balance but over different
tables with different sign rules; two route handlers that both validate a date
field but in different formats with different error messages.

**Rule:** Leave it alone. Wait for a third instance before reconsidering.

### 2. Structural Duplication — Best Extraction Target

The same behavior — same SQL shape, same sign computation, same validation
rule — appears in multiple modules with only minor surface variation (different
table name, different field name).

Examples:
- `COALESCE(stat, '') NOT IN ('X', 'Y')` repeated in SUM queries across
  `informes`, `facturas`, `compras` — should be a constant or helper.
- `bank_helpers.signo_documento` logic reimplemented inline in a module route.
- The same `mov_doble.registrar(...)` call pattern duplicated in two different
  reverso handlers.

**Rule:** Extract once into the appropriate `_helpers.py` or the existing
cross-cutting helper (`bank_helpers`, `caja_helpers`). Verify both callers
pass the same tests after extraction.

### 3. Near-Duplication With Meaningful Variants — Extract Shared Core

Two handlers do the same thing but with one parameter that legitimately varies.

Examples: two reverso handlers that share 90% of the logic but differ in which
`documento` type they use for compensation.

**Rule:** Extract the shared core into a helper that accepts the variant as a
parameter. Keep the variant explicit in the call site — do not make it a
runtime dispatch or a class hierarchy.

## Search Tactics

Use `rg` to find structural duplication before deciding:

```bash
# Find repeated SQL patterns across modules
rg "NOT IN \('X', 'Y'\)" modules/

# Find repeated mov_doble patterns
rg "mov_doble" modules/ --include="*.py" -l

# Find repeated bank_helpers calls that might be inline duplicates
rg "signo_documento\|insert_movimiento" modules/

# Compare two similar route handlers line-by-line
diff <(sed -n '/def reversar/,/^def /p' modules/bancos/__init__.py) \
     <(sed -n '/def reversar/,/^def /p' modules/cheques/__init__.py)
```

## Preferred Consolidation

- **Shared helper in the existing helpers file**: add a function to
  `bank_helpers.py`, `caja_helpers.py`, or `concepto_parser.py` when it
  belongs to that domain.
- **Module-level helper function**: `def _helper(...)` in the module's own
  `__init__.py` when only that module needs it and the route handler is too long.
- **SQL constant or fragment**: define repeated SQL conditions as a module-level
  constant when they must stay in sync across queries.

## Avoid

- Extracting every small route helper into a shared module — the call overhead
  of a shared abstraction only pays off at three or more callers.
- Generic registries or factory patterns for what is essentially a list of
  similar but independent operations.
- Merging reverso flows that have different rollback semantics even if their
  surface looks similar — a wrong merge here breaks financial integrity.
- Over-abstracting sign conventions into a class hierarchy — the table in
  `bank_helpers.py` and `caja_helpers.py` is the right level of abstraction.
