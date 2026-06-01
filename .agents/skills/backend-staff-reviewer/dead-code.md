# Dead Code

Treat dead-code work conservatively. In this repo, uncertain cases should be
reported as candidate dead code until local inspection proves otherwise.

## Candidate Signals

- Modules, helpers, templates, or scripts with no import, route, command, or
  documentation references
- Blueprint modules not registered from `app.py`
- Old config or DB helpers left behind after wiring changes
- Repair/import scripts superseded by newer scripts
- Templates no longer reachable from an active route
- Tests that only exercise obsolete helper structure instead of behavior

## Verification Workflow

1. Search for imports, call sites, route names, template names, and script
   references with `rg`.
2. Check blueprint registration in `app.py`.
3. Inspect module `__init__.py` files, route endpoints, template includes, and
   `url_for` references.
4. Check tests, fixtures, docs, runbooks, cron/deploy notes, and migration
   references for indirect or operational usage.
5. Consider env-gated, production-only, DBF-sync, scheduled, and one-off repair
   behavior before removal.

If usage is still uncertain, report it as candidate dead code and explain what
still needs verification.

## Be Careful With

- Migrations, which are historical artifacts and usually should not be removed
- Repair/backfill scripts, which may be retained for auditability
- Templates included indirectly through shared partials
- Jinja macros and filters referenced by string name
- `url_for` endpoint strings, permission strings, and role config entries
- Scripts that are intended to be run manually during deploy, sync, or repair
- Legacy dBase parity code whose purpose is not obvious without docs or PRG
  comparison

## Removal Standard

- Remove only after local evidence shows there is no runtime, test, template,
  migration, documentation, or operational usage.
- When removing code, also remove stale imports, route registrations, template
  links, tests, and docs references that still point at it.
- If a file exists mainly as operational history or a future boundary, flag the
  maintenance cost rather than deleting it blindly.
