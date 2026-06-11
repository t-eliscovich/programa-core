# Backend Performance — Programa Core

Use this reference when reviewing Flask endpoint, DB query, and connection pool
performance.

## Baseline First

Before any recommendation:

1. Identify the specific endpoint or operation to measure
2. Capture a baseline: response time, query count, query time, row count
3. Form a hypothesis about the bottleneck
4. Apply the change
5. Re-measure with the same method
6. Report the delta

## Flask Endpoint And WSGI Behavior

**Sequential blocking I/O** — Waitress serves requests on a thread pool
(default: 4 threads). If a single request holds a DB connection for a long
time, other concurrent requests queue. Look for:

- Route handlers that open a connection, do a slow query, and then do
  additional Python work before releasing
- Multiple `db.get_conn()` calls in sequence where a single connection with
  multiple queries would suffice
- Any `time.sleep()` or blocking external call (HTTP to SRI, SFTP) inside a
  request handler — these stall a Waitress thread

**Connection pool exhaustion** — `db.py` manages a psycopg2 pool. If pool size
is smaller than peak concurrency, requests queue waiting for a connection.
Check pool configuration against actual concurrent user count.

**Measurement:**
```bash
# Time a specific endpoint with curl
time curl -s -o /dev/null -w "%{http_code} %{time_total}s" http://localhost:5000/bancos

# Time under mild concurrency
for i in $(seq 1 5); do curl -s -o /dev/null -w "%{time_total}\n" http://localhost:5000/bancos & done; wait
```

## Database And psycopg2

**Query count** — The most common performance problem is N+1: a loop in Python
that issues one SQL query per row fetched in an outer query.

Detection:
```bash
# Enable statement logging in PostgreSQL (dev only)
# Set in postgresql.conf: log_min_duration_statement = 50

# Or add timing instrumentation to db.get_conn() temporarily
```

Check for patterns like:
```python
rows = conn.execute("SELECT id FROM clientes").fetchall()
for row in rows:
    detail = conn.execute("SELECT ... WHERE id=%s", (row[0],)).fetchone()
    # ← this is N+1
```

**Missing indexes** — Use `EXPLAIN ANALYZE` in psql:
```sql
EXPLAIN ANALYZE SELECT * FROM movimientos_bancarios
WHERE no_banco = 10 AND fecha >= '2026-01-01'
ORDER BY fecha DESC;
```

Look for `Seq Scan` on large tables when a filtered query is slow.
Common columns that need indexes: `no_banco`, `fecha`, `cliente_id`,
`proveedor_id`, `stat`, foreign key columns used in JOINs.

**Query shape** — Check that SUM queries always include the
`COALESCE(stat, '') NOT IN ('X', 'Y')` filter on financial tables. A missing
filter is both a correctness bug and a performance hazard (reads more rows).

**Large result sets** — Pages that render all rows (full cartera, full historial)
can return thousands of rows. Check whether pagination or a date-range default
filter is applied. `fetchall()` on thousands of rows bloats both DB server memory
and Jinja2 render time.

**Measurement:**
```sql
-- Run in psql against the dev/test DB
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
<your query here>;
```

```bash
# Run a specific test and measure its DB time
pytest tests/test_bancos.py -v --durations=10
```

## Jinja2 Template Render Time

Jinja2 render is synchronous and single-threaded. Slow render usually means:

- A template loop over thousands of rows with per-row formatting
- A Jinja2 filter (defined in `filters.py`) that is inefficient
- Multiple template includes that each trigger a DB lookup (avoid this pattern —
  all data should be fetched in the route and passed to the template)

**Measurement:**
```python
# Add temporarily to a route handler to isolate render time
import time
start = time.perf_counter()
result = render_template(...)
elapsed = time.perf_counter() - start
app.logger.info(f"render_template took {elapsed:.3f}s")
```

## Connection Pool And Concurrency

**Pool size** — If concurrent requests exceed pool size, they queue. Check
the pool configuration in `db.py` against the Waitress thread count.
A good starting rule: pool_size ≥ Waitress threads.

**Long-held connections** — A connection held across a slow Python computation
(between SQL calls) blocks the pool slot. Keep DB connection scope tight:
open → query → close, not open → compute → query → close.

**Measurement:**
```bash
# Check active connections in PostgreSQL
psql -c "SELECT count(*), state FROM pg_stat_activity WHERE datname='intela' GROUP BY state;"
```

## Caching

Programa Core does not currently use a cache layer. If caching is proposed:

- Correctness must be proven first — financial data requires cache invalidation
  that is tied to the transaction that changed it
- Per-user isolation must be guaranteed — never share a cached saldo across users
- TTL-based caches are dangerous for financial data — prefer event-driven
  invalidation or accept stale reads only for non-financial reporting data

**Rule:** Label caching proposals as **Hypothesis** until correctness and
isolation are demonstrated with a concrete invalidation design.

## Backend Correctness Checks

After any performance change:

```bash
make ci          # full lint + test-coverage
make test-db     # DB-backed tests
ruff check .     # linter
```

Verify that the performance change did not alter observable behavior by running
the relevant test suite, not just eyeballing the code.
