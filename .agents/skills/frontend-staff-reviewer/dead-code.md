# Dead Code

Treat dead-code work conservatively. In this repo, uncertain cases should be
reported as candidate dead code until local inspection proves otherwise.

## Candidate Signals

- Templates, macros, static JS/CSS, or helper functions with no route, include,
  import, or asset reference
- Scaffold screens or placeholder helpers left behind after a real workflow
  lands
- Navigation entries, labels, action buttons, or badges that no longer match
  real routes
- Old upload, report, or dashboard fragments superseded by newer flows
- Tests that only exercise obsolete implementation details instead of user
  behavior

## Verification Workflow

1. Search for imports, includes, `extends`, macro calls, `url_for`, route names,
   static filenames, and CSS/JS selectors with `rg`.
2. Check route reachability through `app.py` blueprint registration and
   `modules/<domain>/views.py`.
3. Check navigation/sidebar/action-bar wiring in templates and config.
4. Inspect tests and docs for indirect usage, intended rollout shape, and
   behavior coverage.
5. Consider permission-gated, env-gated, and manually invoked admin/debug
   surfaces before concluding a file is unused.

If usage is still uncertain, report it as candidate dead code and explain what
still needs verification.

## Be Careful With

- Jinja macros and partials referenced by include/import strings
- Templates rendered by endpoint names rather than direct filename search
- Static scripts referenced from `base.html`
- Permission-gated admin and debug pages
- CSV/XLSX upload templates shared across modules
- Report templates whose routes are rarely used but operationally important

## Removal Standard

- Remove only after local evidence shows there is no runtime, test, template,
  navigation, documentation, or operational usage.
- When removing shared UI, also remove stale route links, copy, tests, static
  selectors, and docs references.
- If a page might still be part of an incomplete rollout or admin/debug flow,
  flag the risk rather than deleting it blindly.
