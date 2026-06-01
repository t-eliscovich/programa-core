# Measurement Playbook

Use this playbook to choose repeatable measurements for performance work. The
goal is not to run every command. The goal is to collect enough evidence to
confirm or reject the specific hypothesis.

## Measurement Loop

Use the full loop for implemented changes. For audit or planning work, complete
steps 1-3, then record the recommendation, the measurement needed after a
future change, and the correctness checks required before shipping it.

1. Define the target: route, report, template, table interaction, SQL path,
   import/sync, or script.
2. Capture a baseline with exact commands, inputs, and environment notes.
3. State the hypothesis in one sentence.
4. Apply or review one scoped change.
5. Re-run the same measurement under comparable conditions.
6. Compare before and after values.
7. Run correctness checks and report residual risk.

If the measurement cannot be repeated, do not use it as proof. Treat it as a
lead for more controlled investigation.

## Report Template

```text
Target:
Environment:
Baseline command/tool:
Baseline result:
Hypothesis:
Change or recommendation:
Validation measurement for audits/plans:
Post-change command/tool: required only for implemented changes
Post-change result: required only for implemented changes
Delta: required only for implemented changes
Correctness checks: run checks for implemented changes; name required checks for audits or plans
Conclusion: confirmed | rejected | inconclusive
```

For multiple metrics, use a table:

```text
Metric | Baseline | Post-change | Delta | Notes
```

## UI Measurements

Use route and browser evidence for UI claims:

- Start the Flask app with `python run.py`, `./launcher.sh`, or Docker.
- Use a consistent URL, user role, date range, and database state.
- Capture route response timing, rendered HTML size, static asset waterfall, or
  browser interaction timing for the specific flow.
- Keep manual impressions separate from measured browser or route timing.

Use browser automation when the user-visible flow matters:

- Open the route, perform the interaction, capture screenshots, inspect timing,
  or run browser-side timing scripts.
- Record the URL, viewport, cache state, role, and interaction steps.

## Backend Measurements

Use focused tests or scripts for route and service-path timing:

- Prefer existing pytest fixtures and focused test cases when available.
- Run enough repetitions to reduce noise for small timings.
- Include database fixture size and cache state.

Use query evidence for database claims:

- Capture query count and SQL text from tests, logs, instrumentation, or
  targeted scripts when appropriate.
- Use `EXPLAIN` or `EXPLAIN ANALYZE` for slow query shape when a realistic
  database is available.
- Compare plans after query or index changes.

Use API/route-level timing when the full request path matters:

- Measure the same method, URL, body, auth state, database/cache state, and date
  range.
- Separate cold-start, cold-cache, warm-cache, and steady-state results.
- Report response size when serialization, Jinja rendering, or payload size is
  part of the hypothesis.

## Useful Repo Commands

Use these as evidence or correctness checks when relevant:

- `rg` for call sites, routes, templates, query construction, cache keys, and
  repeated work
- `make lint`
- `make test`
- `.venv/bin/python -m pytest <focused tests> -q`
- `.venv/bin/python scripts/migrate.py --dry-run`
- `find modules scripts templates static -type f | sort`
- `find modules scripts templates -name '*.py' -o -name '*.html'`
- `find modules scripts -name '*.py' -exec wc -l {} + | sort -n`

Line counts are only discovery signals. They do not prove a performance issue.

## Guardrails

- Compare like with like: same mode, route, input, auth state, date range,
  cache state, and fixture size.
- For noisy metrics, run multiple samples and report median or range.
- Avoid optimizing cold paths unless a measurement shows they matter.
- Keep correctness validation attached to every performance change.
- If a benchmark is synthetic, say what real-world behavior it does and does
  not represent.
- If a production-only concern cannot be measured locally, describe the exact
  telemetry, log, database snapshot, or profiling data needed before
  implementation.
