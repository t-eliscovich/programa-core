# Frontend Performance Checks

Use this reference for Programa Core's Flask/Jinja/Tailwind UI. Pair these
checks with [measurement-playbook.md](./measurement-playbook.md) and keep all
recommendations evidence-backed.

## Baseline First

Before proposing UI performance changes, identify the route or interaction and
capture one or more relevant baseline measurements:

- Route response timing and rendered HTML size
- Browser timing for dense tables, form submission, sorting, or modal flows
- Static asset size and network waterfall
- DOM size for large reports or lists
- Screenshot or DOM inspection only when visual loading or layout shifts matter

Record whether the app ran locally, in Docker, or production-like mode. Do not
compare unrelated databases, auth states, or date ranges as if they were the
same signal.

## Jinja and Route Context

Measure before changing:

- Template loops over large result sets
- Python route context assembly that duplicates query work
- Repeated filters or formatting work inside large loops
- Large shared partials included on pages that do not need them
- Pages rendering hidden data that could be paginated or progressively loaded

Useful evidence:

- Route source and query call sites
- Rendered HTML size and row counts
- Timing around route handler, query, and template render boundaries
- Browser timing for first useful paint and interaction readiness

## Assets and Browser Interaction

Check measured impact before and after:

- Static JS loaded globally but used by only one flow
- Large CSS or duplicated Tailwind classes causing maintainability and payload
  cost
- Chart scripts, table sorting, and upload helpers loaded where not needed
- Images without appropriate dimensions or caching
- Expensive DOM operations on large tables

Prefer page-local data attributes and narrow enhancements only when browser or
asset measurements show a real gain.

## Rendering and State

Use browser timing or targeted instrumentation before claiming UI wins:

- Large lists that block sorting, filtering, or row updates
- Event handlers that scan the full DOM repeatedly
- Client-side formatting that duplicates server formatting
- Layout shifts from variable-width controls or late-injected content
- Tables that should be paginated, filtered server-side, or summarized

Client-side optimization is not a default fix. Add it only when measurement
shows repeated work that exceeds the complexity cost.

## Frontend Correctness Checks

Performance changes must keep these passing when relevant:

- `make lint`
- `make test`
- `.venv/bin/python -m pytest tests/test_routes_smoke.py -q`
- Browser checks against the affected route when table/form/chart behavior
  changes

Use focused tests for behavior changed by caching, pagination, loading, or
state-flow changes. Do not rely on faster manual observations alone.
