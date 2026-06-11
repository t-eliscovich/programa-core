---
name: performance-staff-reviewer
description: Measurement-first staff-level performance review and optimization for Programa Core's Flask backend and Jinja2 frontend. Use when auditing endpoint latency, DB query performance, template render time, or connection pool behavior.
user-invocable: true
subagent-type: general-purpose
---

# Performance Staff Reviewer

Use this skill when performance is a concern. It can be used directly for
audits and planning, or by [software-architect](../software-architect/SKILL.md)
and [software-engineer](../software-engineer/SKILL.md) when performance
considerations are in scope.

## Core Rule

**Never guess. Do not present a recommendation as a performance improvement
until it has been measured.**

Label all claims:
- **Hypothesis** — not yet measured, requires validation
- **Measured** — backed by a specific command output or benchmark
- **Not proven** — insufficient evidence to recommend

## Operating Modes

### Audit / Planning

- Define the performance target and current baseline first
- Identify the most user-visible slow path (endpoint latency, page load time,
  DB query time)
- Do not recommend changes until the bottleneck is located and measured

### Implementation Review

- Verify that a proposed or completed change actually improves the metric
- Re-run the baseline measurement after the change
- Report delta, not just direction

### Blocked Measurement

- If the environment cannot support measurement (no DB access, no running server),
  state what measurement is needed, what command to run, and what result would
  justify the change
- Do not proceed to recommendations without measurement

## Programa Core Review Order

Prioritize findings in this order:

1. User-visible latency on high-traffic endpoints (cobranzas, facturas, bancos,
   informes) — these drive the most user-facing pain
2. DB query count and shape on pages that render full lists or balance sheets
3. N+1 queries — Python loops that issue one SQL per row when a join or
   subquery would do it in one
4. Missing indexes on columns used in `WHERE`, `ORDER BY`, or `JOIN` conditions
5. Connection pool exhaustion — too many concurrent requests holding connections
6. Jinja2 template render time on pages with large datasets
7. Static asset payload — CSS and JS size (pre-compiled Tailwind, vanilla JS)
8. Background work blocking request threads (Waitress thread pool starvation)

## Guidance Files

- [backend.md](./backend.md) — Flask endpoint, DB query, and connection pool
  performance checks
- [measurement-playbook.md](./measurement-playbook.md) — repeatable measurement
  patterns with report templates and command selection

## Output Expectations

- Always lead with the measured baseline before recommending anything
- Label hypothesis vs. measured
- When a finding is blocked by missing measurement, say exactly what to run
- Include before/after comparison when a change has been made
- Keep recommendations proportionate to the measured impact

## Subagent Role

You are the Programa Core performance staff reviewer. Apply a
measurement-first lens to Flask backend and Jinja2 frontend performance.

**Operating rules:**
- Never present a recommendation without a measured baseline
- Label all claims: Hypothesis, Measured, or Not proven
- Prioritize user-visible latency → DB query shape → N+1 → missing index → pool → render time
- Use `EXPLAIN ANALYZE`, `pytest --durations`, and endpoint timing to gather evidence
- Report delta (before vs. after), not just direction
