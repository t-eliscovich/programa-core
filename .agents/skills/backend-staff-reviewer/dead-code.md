# Dead Code

Conservative criteria for identifying and safely removing dead code in
Programa Core's Python/Flask codebase.

## Candidate Signals

These indicate potential dead code — they require verification before removal:

- A route function defined in a module's `__init__.py` that is not registered
  in `app.py` blueprint registration.
- An imported function or module that `rg` finds no call sites for.
- A helper function in `bank_helpers.py`, `caja_helpers.py`, or a module with
  only one call site that itself looks unused.
- An old migration SQL file that references a table or column that no longer
  exists in the schema.
- A `templates/<name>/` file that is never referenced by any `render_template`
  call in the module.
- A `scripts/` file that is not referenced in the `Makefile`, `launcher.sh`,
  `deploy_pc.sh`, or any other script.
- A test file that imports a module that no longer exists.

## Verification Workflow

Before removing anything:

1. **Search for imports**: `rg "from <module> import <name>"` and
   `rg "import <name>"` across the full repo.
2. **Check blueprint registration**: `rg "register_blueprint"` in `app.py` to
   confirm whether the module's blueprint is wired up.
3. **Check template references**: `rg "render_template.*<template-file>"` to
   confirm whether a template is reachable.
4. **Check test coverage**: `rg "<function-name>"` in `tests/` to confirm
   whether there are tests that depend on the code.
5. **Check migration chain**: review `migrations/` sequence numbers to ensure
   removing a migration file doesn't break the migration runner's ordering.

## Removal Standard

Remove code only when:

- Local evidence (search results above) shows zero call sites, template
  references, and test dependencies.
- The code is not gated by a feature flag, environment variable, or
  conditional import that might activate it in production.
- Removing it does not break the migration chain.

When removing:

- Delete the function, import, and any now-unused test that only covered it.
- Remove the blueprint registration line in `app.py` if the whole module is dead.
- Do not leave commented-out code — remove it completely.

## Handle With Extra Care

- **Migration files**: Never remove a migration file even if the table it
  creates was later dropped. The migration runner depends on sequential
  numbering. Mark it with a `-- DEPRECATED` comment if it is truly irrelevant.
- **`bank_helpers.py` and `caja_helpers.py`**: These are called from many
  places. `rg` all entry points before concluding any function is unused.
- **`auth.py` and decorator functions**: Auth decorators may be applied via
  string name or dynamic lookup — verify via `rg "@login_required"` and
  `rg "login_required"` before assuming a decorator is unused.
- **`filters.py`**: Jinja2 filters are registered by name and called from
  templates — search `templates/` for the filter name before removing.
- **Scripts**: Many scripts are called manually, from cron, or from deployment
  procedures not visible in the repo. Confirm with the owner before removing.
