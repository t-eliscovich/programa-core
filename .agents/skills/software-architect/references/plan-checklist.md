# Plan Checklist

Use this checklist before finalizing a plan.

## Problem Framing

- Is the goal explicit?
- Are success criteria concrete?
- Are non-goals named when scope could sprawl?

## System Shape

- Is ownership of each major responsibility clear?
- Is the modular-monolith-vs-service decision explicit and justified when
  relevant?
- Are new abstractions justified by concrete pressure?
- Are affected surfaces named as `backend`, `ui`, `database`, `integration`,
  `scripts/ops`, `cross-cutting`, or `neither`?
- If backend or UI code is affected, is the required staff-reviewer lens named
  for implementation and final review?

## Interfaces And State

- Are route, schema, migration, template, script, integration, or workflow
  changes spelled out?
- Is data ownership and source of truth clear?
- Are background jobs, async work, or external side effects accounted for?

## Performance And Risk

- Are the main latency, throughput, caching, or hot-path concerns addressed?
- Are important failure modes called out?
- Are migration, rollout, or compatibility constraints included when relevant?

## Test Plan

- Are the key unit, integration, and end-to-end scenarios named?
- Are acceptance criteria concrete enough to verify behavior?

## Assumptions And Defaults

- Are confirmed decisions separated from defaults?
- Are any remaining risks called out explicitly?
- Could an implementer execute the plan without inventing major decisions?
