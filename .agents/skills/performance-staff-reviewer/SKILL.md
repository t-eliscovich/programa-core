---
name: performance-staff-reviewer
description: Measurement-first staff-level performance review and optimization workflow for Programa Core's Flask/Jinja/PostgreSQL app. Use for audits, optimization planning, SQL/query behavior, route latency, report rendering, DBF/import costs, caching, payload size, static asset cost, and validating whether a performance hypothesis improved real measured behavior.
metadata:
  short-description: Measured Flask and Postgres performance review
---

# Performance Staff Reviewer

Use this skill as the measurement-first performance lens for Programa Core.
Ground every claim in current-code evidence, and distinguish audit or planning
work from implemented optimization review. Treat unmeasured ideas as
hypotheses, not findings.

Apply it for:

- Flask route, report, import/sync, script, or full-stack performance audits
- Optimization plans that need measurable acceptance criteria
- SQL, index, query-count, transaction, and pagination review
- Jinja render cost, dense table performance, static asset cost, payload size,
  caching, background work, or resource-usage investigations
- Review of performance-sensitive implementation changes

Do not use it as the primary owner for:

- Pure maintainability reviews without a performance question
- Visual design review unless rendering, payload, asset, or interaction
  performance is involved
- Product-flow decisions where [product-designer](../product-designer/SKILL.md)
  should own user intent
- Implementation execution where [software-engineer](../software-engineer/SKILL.md)
  should own code changes

## Core Rule

Never guess. Do not present a recommendation as a performance improvement until
it has been measured under comparable conditions. For audits and plans, measure
the current behavior, state the hypothesis, and name the follow-up measurement
that would prove or disprove the proposed change. For implemented changes,
require comparable before/after measurements. If measurement is blocked, state
the blocker and keep the conclusion provisional.

Acceptable language:

- "Hypothesis: narrowing this SQL predicate should reduce report latency."
- "Measured: `/informes/balance` dropped from X ms to Y ms on the same DB."
- "Not proven: the local database is too small to validate the index impact."

Unacceptable language:

- "This will be faster" without measurement.
- "This is probably slow" without a supporting trace, metric, query count, or
  reproducible observation.

## Operating Modes

- **Audit or planning:** ground in the code path, capture a baseline or explain
  why it cannot be captured, identify measured risks, and define the measurement
  and correctness checks needed for any proposed change.
- **Implementation review:** compare baseline and post-change measurements under
  equivalent inputs, report the delta, and confirm correctness checks. A faster
  broken path is a regression.
- **Blocked measurement:** keep the output provisional and specify the exact
  local command, trace, database fixture, log, or production-like data needed.

## Programa Core Review Order

Prioritize measurable risks in this order:

1. User-visible latency on important routes, reports, and operational workflows
2. SQL query count, query shape, indexes, N+1 behavior, pagination, and
   transaction boundaries
3. Expensive report calculations, legacy parity checks, and large table
   rendering
4. DBF import/sync, integration readers, one-off scripts, and scheduled tasks
5. Cache hit rate, invalidation correctness, key shape, and stale-data risk
6. Response size, Jinja render cost, static JS/CSS asset cost, and browser-side
   interaction cost
7. Startup work, blocking I/O, connection pool pressure, memory growth, and
   resource pressure
8. Missing measurement, regression tests, or observability

## Measurement Requirements

For every performance task, record the evidence appropriate to the mode:

- Exact command or tool used
- Route, report, SQL path, script, fixture, or user flow measured
- Relevant environment details such as local/Docker/prod-like mode, database
  state, cache state, and commit or diff context
- Baseline value for audit or planning work
- Baseline value, post-change value, and delta for implemented changes
- Correctness validation run alongside implemented changes, or the correctness
  checks required before shipping a proposed change

For before/after implementation data, use small tables when practical. If a
metric is noisy, repeat the measurement and report the range or median instead
of a single lucky run.

## Related References

Load only the reference needed for the task:

- [backend.md](./backend.md) for Flask route, SQL, migration, script, import,
  caching, and resource performance checks
- [frontend.md](./frontend.md) for Jinja render, static asset, table, form, and
  browser interaction performance checks
- [measurement-playbook.md](./measurement-playbook.md) for audit and
  implementation measurement patterns, reporting templates, and command
  selection

## Related Skills

Use these alongside this skill when appropriate:

- [backend-staff-reviewer](../backend-staff-reviewer/SKILL.md) for backend
  boundaries, DB/domain/view separation, and maintainability risks
- [frontend-staff-reviewer](../frontend-staff-reviewer/SKILL.md) for template,
  static asset, and UI maintainability risks
- [supabase-postgres-best-practices](../supabase-postgres-best-practices/SKILL.md)
  for Postgres query, index, and schema guidance
- [agent-browser](../agent-browser/SKILL.md) for browser-based performance QA,
  screenshots, interaction checks, and route-flow measurements when a running
  app is needed

## Output Expectations

Default to findings backed by evidence:

- Findings first, ordered by measured impact or severity
- Each finding includes evidence, hypothesis, recommended change, and the
  measurement needed to validate it if not already run
- After implementation review, include before/after measurements and whether
  the hypothesis was confirmed
- Call out correctness tests and performance-regression coverage
- Keep unmeasured ideas in a separate "Hypotheses" section
