# Backend Performance Checks

Use this reference for Programa Core's Flask/PostgreSQL backend. Pair these
checks with [measurement-playbook.md](./measurement-playbook.md) and keep all
conclusions measurement-backed.

## Baseline First

Before proposing backend changes, identify the route, report, query path,
script, import, or service function and capture one or more relevant baseline
measurements:

- Route latency under a repeatable request shape
- Query count, SQL text, query plan, or DB helper call path
- Response body size and Jinja render time when pages are large
- Cache hit/miss behavior for repeated requests
- Test runtime or script timing for focused hot paths
- Logs or profiling traces showing blocking I/O, startup work, connection pool
  pressure, or memory growth

Record database state, fixture size, cache state, and whether the service is
running locally, in Docker, or under test.

## Flask and Workflow Behavior

Measure before changing:

- Sequential DB calls that could be combined or moved into one focused query
- Repeated per-request initialization
- Large context assembly inside route handlers
- Background or repair work that should not block a user response
- Multi-step workflows where transaction ordering affects correctness
- Dense report pages that recompute expensive values on every request

Use latency measurements plus correctness tests. Reducing route work can expose
ordering, permission, transaction, and stale-data bugs.

## Database and SQL

Gather evidence before changing query code:

- Query count for the route, report, or script path
- SQL shape and bound parameters for suspicious queries
- `EXPLAIN` or `EXPLAIN ANALYZE` for slow filtering, sorting, joins, or
  aggregation when a realistic database is available
- N+1 behavior caused by per-row lookups
- Missing indexes proven by plans, not intuition
- Pagination and response size under realistic cardinality

Index or query-shape recommendations require a measured slow query or plan. If
the local dataset is too small, label the conclusion provisional and specify
the production-like measurement needed.

## Imports, Scripts, and Integrations

Measure operational paths separately from request paths:

- DBF import/sync runtime and row counts
- Asinfo/Metabase/formulas reader latency and timeout behavior
- One-off repair script runtime, transaction scope, and affected rows
- Monthly/daily scheduled task runtime and idempotence checks
- Health/diagnostic script cost when used in deploy or support workflows

Performance changes in scripts still require correctness checks because these
paths often mutate or reconcile financial data.

## Caching and Payloads

Cache only when there is a measured repeated cost and a correctness strategy:

- Define cache key shape, TTL, invalidation trigger, and stale-data tolerance
- Include user, role, period, date range, account, source, or permission scope
  in cache keys unless data is explicitly shared
- Measure hit rate and latency or load reduction
- Check stale report risk and cache-bypass behavior when correctness requires it
- Keep response payloads scoped to fields and rows the page needs

Reducing payload size should include before/after byte counts or render timing
plus tests that prove templates still receive required fields.

## Backend Correctness Checks

Performance changes must keep these passing when relevant:

- `make lint`
- `make test`
- `.venv/bin/python -m pytest <focused tests> -q`
- `.venv/bin/python scripts/migrate.py --dry-run` for migration/index changes
- `.venv/bin/python scripts/check_salud_dia.py` when a real DB is available and
  financial invariants are in scope

Add focused regression tests for changed query behavior, caching semantics,
pagination, report totals, or response contracts.
