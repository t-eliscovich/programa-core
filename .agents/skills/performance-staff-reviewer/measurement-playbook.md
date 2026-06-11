# Measurement Playbook — Programa Core

Repeatable measurement patterns for performance work. Use these before and
after any performance-motivated change.

## Measurement Loop

1. **Define the target** — which endpoint, query, or operation is slow?
2. **Capture the baseline** — run the measurement command and record the result
3. **Form a hypothesis** — what is the bottleneck and why?
4. **Apply the change** — one change at a time
5. **Re-measure** — same method, same conditions
6. **Compare** — report delta (not just direction)
7. **Validate correctness** — run `make ci` after every change

Never skip step 2. Never skip step 7.

## Report Template

```
Target:       <endpoint or operation>
Environment:  <dev/staging/prod, DB row counts>
Baseline:     <command + result>
Hypothesis:   <bottleneck description — labeled "Hypothesis">
Post-change:  <command + result>
Delta:        <e.g., "450ms → 90ms (−80%)">
Correctness:  <make ci result — PASS / FAIL>
```

## Endpoint Timing

```bash
# Single request timing
curl -s -o /dev/null -w "status=%{http_code} time=%{time_total}s\n" \
  http://localhost:5000/bancos

# Multiple requests, report all timings
for i in $(seq 1 10); do
  curl -s -o /dev/null -w "%{time_total}\n" http://localhost:5000/bancos
done

# Mild concurrency (5 parallel requests)
for i in $(seq 1 5); do
  curl -s -o /dev/null -w "%{time_total}\n" http://localhost:5000/bancos &
done; wait
```

## Database Query Timing

```sql
-- Full execution plan with timing and buffer usage
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT ...;

-- Just the execution time without the plan
\timing
SELECT ...;
```

```bash
# psql shortcut for the dev DB
psql -d intela -c "\timing" -c "SELECT ..."

# Check what queries are running right now
psql -d intela -c "
  SELECT pid, now() - query_start AS duration, state, query
  FROM pg_stat_activity
  WHERE datname='intela' AND state != 'idle'
  ORDER BY duration DESC;"
```

## Pytest Timing

```bash
# Show the 10 slowest tests
pytest --durations=10

# Show slowest tests in a specific module
pytest tests/test_bancos.py --durations=5 -v

# Profile a specific test
pytest tests/test_informes.py -v --durations=0 -s
```

## Template Render Timing

Add temporarily to a route handler to isolate render time from DB time:

```python
import time

@bp.route("/bancos")
def index():
    # DB phase
    t0 = time.perf_counter()
    data = _fetch_data(conn)
    db_ms = (time.perf_counter() - t0) * 1000

    # Render phase
    t1 = time.perf_counter()
    result = render_template("bancos/index.html", **data)
    render_ms = (time.perf_counter() - t1) * 1000

    current_app.logger.info(f"bancos/index: db={db_ms:.1f}ms render={render_ms:.1f}ms")
    return result
```

Remove the instrumentation after measuring.

## Index Discovery

```sql
-- Find tables with sequential scans on large row counts
SELECT schemaname, tablename, seq_scan, seq_tup_read, idx_scan
FROM pg_stat_user_tables
WHERE seq_tup_read > 10000
ORDER BY seq_tup_read DESC;

-- Find missing indexes on FK columns
SELECT c.conrelid::regclass, a.attname, c.confrelid::regclass
FROM pg_constraint c
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey)
WHERE c.contype = 'f'
  AND NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = c.conrelid AND a.attnum = ANY(i.indkey)
  );
```

## Connection Pool Check

```bash
# Check active connections by state
psql -d intela -c "
  SELECT count(*), state
  FROM pg_stat_activity
  WHERE datname='intela'
  GROUP BY state;"

# Check pool configuration in db.py
grep -E "pool|minconn|maxconn" db.py
```

## Guardrails

- Always compare **like with like** — same DB state, same row counts, same
  server load. A measurement taken right after a DB restore is not comparable
  to one taken with a warm cache.
- Run `make ci` after every performance change. Performance wins that break
  correctness are not wins.
- If a query plan changes between runs (different row estimates), run `ANALYZE
  <tablename>` first to update statistics, then re-measure.
- Do not measure on the production DB unless there is no other option. Use the
  test DB with a realistic data snapshot (e.g., restored from `intela12042026.sql`).
