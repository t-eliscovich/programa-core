---
name: pr-reviewer
description: Ruthless staff-level GitHub pull request and local branch review for Programa Core. Use when asked to review a PR, audit a diff, or provide a high-signal review with focus on correctness, financial invariants, architecture, testing, repo standards, complexity cost, and code quality.
user-invocable: true
---

# PR Reviewer

Use this skill when the task is review-first rather than implementation-first.

## Review Posture

- Treat review as a quality gate, not a courtesy pass. "Works on the happy
  path" is not enough for merge readiness.
- Bias toward blocking unclear, over-complex, brittle, duplicated, or poorly
  shaped code when it materially raises correctness, maintenance, testing, or
  product-quality risk.
- Require evidence for claims and cite files directly.
- Demand code that is easy to read, hard to misuse, locally conventional, and
  proportionate to the problem being solved.

## Review Modes

- GitHub pull request review via `gh`
- Local branch review against a base ref
- Review of an existing bundle under `.pr-review/`, `.plan-verify/`, or a user
  supplied directory

## Confirm Context

1. If the user gave a PR number or URL, review that PR.
2. If the user wants a local branch review, resolve the base ref before reading
   the diff.
3. If `gh` auth is missing for PR review, ask the user to run `gh auth login`.

## Gather Review Data

Prefer repo helpers if they exist:

- `./scripts/pr-review-bundle.sh <pr-number|url>`
- `./scripts/local-review-bundle.sh <base-ref>`
- `./scripts/post-plan-verify.sh --base <base-ref>`

If helpers are unavailable or insufficient, gather the same data manually:

- `gh pr view <PR> --json number,title,author,baseRefName,headRefName,labels,reviewDecision,files,additions,deletions,body,url,createdAt,updatedAt,mergedAt,closedAt,commits,statusCheckRollup`
- `gh pr diff <PR>`
- `gh pr checks <PR>`
- `gh pr view <PR> --comments`
- `gh pr checkout <PR>` when file-level inspection is needed

For local branch review:

- `git merge-base HEAD <base-ref>`
- `git diff --name-status <merge-base>`
- `git diff <merge-base>...HEAD`

If a saved bundle already exists, read its metadata and diff first. Treat
verification logs as supporting evidence, not as a substitute for code review.

## Load Repo Standards

- Read `README.md`, `TESTING_PLAN.md`, relevant `docs/`, and any repo-local
  instructions if present.
- Route backend-heavy changes through `backend-staff-reviewer`.
- Route template/static UI-heavy changes through `frontend-staff-reviewer`.
- Route measured latency/query/render changes through `performance-staff-reviewer`.
- Keep `Makefile`, `docker-compose.yml`, `scripts/`, `.agents/`, `migrations/`,
  and deploy/runbook changes under explicit workflow and runtime review, even
  when there are no product-code changes.

## Route By Changed Area

- `modules/`, `app.py`, `db.py`, `auth.py`, `config/`, `migrations/`: Flask,
  SQL, auth, schema, accounting workflows, transaction behavior, and tests
- `templates/`, `modules/*/templates/`, `static/`, `filters.py`, `labels.py`:
  Jinja UI, formatting, navigation, forms, tables, accessibility, and Spanish
  operational copy
- `scripts/`: migrations, DBF sync/import, repair scripts, diagnostics, deploy
  helpers, mutation risk, and idempotence
- `docs/`: accuracy, drift from implemented behavior, and stale commands
- `.agents/`, `skills/`, `.skills_draft/`: agent workflow, review tooling, and
  instruction drift

Mixed PRs should still end as one severity-ordered review.

## Staff-Level Review Focus

- Correctness, edge cases, and regression risk
- Financial invariants: saldos, reversos, anulaciones, `mov_doble`, period
  locks, report totals, and legacy dBase parity
- API/route, schema, migration, and compatibility behavior
- Security boundaries, roles, permission strings, session handling, and secret
  handling
- Architecture drift and maintainability
- SQL/query, report, import/sync, and developer-workflow regressions
- Missing tests, weak assertions, or missing runtime validation
- Unnecessary abstraction, cleverness, indirection, oversized files, mixed
  responsibilities, hidden coupling, and unclear data flow
- UI quality when templates are touched: hierarchy, density, spacing,
  interaction polish, accessibility, visual consistency, and operational
  clarity

## Complexity Bar

- Every added layer, helper, abstraction, state path, dependency, or workflow
  branch must buy correctness, extensibility, performance, security, or major
  duplication removal.
- Complexity without a clear payoff is a review finding, not a nit. Call out
  its maintenance cost and the simpler shape that would preserve the behavior.
- Prefer removal, simplification, tighter ownership boundaries, or clearer data
  flow over adding framework, indirection, or helper layers.
- Do not accept tests as a substitute for understandable code. Brittle or
  contorted tests are evidence that the implementation shape needs scrutiny.

## Severity Calibration

- `P0`: ships a known production-stopping failure, severe security exposure,
  irreversible data loss, or unusable critical path.
- `P1`: can cause serious financial regression, data corruption, security risk,
  unusable operational workflow, or long-term inability to evolve a core
  surface.
- `P2`: unnecessary complexity, boundary drift, duplication, brittle tests,
  unclear data flow, or poor UI shape materially raises maintenance cost.
- `P3`: localized naming, polish, readability, or test clarity issues that
  should be fixed but do not block alone unless they accumulate.

## Commands To Prefer

- `gh pr view <PR> --json number,title,headRefName,baseRefName,url,state`
- `gh pr view <PR> --json number,title,author,baseRefName,headRefName,labels,reviewDecision,files,additions,deletions,body,url,createdAt,updatedAt,mergedAt,closedAt,commits,statusCheckRollup`
- `gh pr diff <PR>`
- `gh pr checks <PR>`
- `git status --short`
- `git diff --name-status <merge-base>`
- `make lint`
- `make test`
- `.venv/bin/python scripts/migrate.py --dry-run`
- `.venv/bin/python -m pytest <focused tests> -q`
- `.venv/bin/python scripts/check_salud_dia.py` when a real DB is available
  and financial invariants are in scope

## Review Output Format

Provide a full audit with findings ordered by severity.

- Findings: each issue should include severity (`P0`-`P3`), location, impact,
  and a concrete fix
- When relevant, name the complexity/readability cost, simpler fix, and
  required test or validation
- Questions: only if needed to resolve ambiguity
- Summary: short overall risk/readiness assessment after the findings

## Quality Bar

- Prefer evidence over speculation and cite files directly.
- Call out missing tests explicitly.
- Do not file subjective style complaints unless they connect to readability,
  maintainability, accessibility, correctness, or product-quality risk.
- If the PR is mergeable only after simplification or polish, say so plainly.
- If there are no issues, say that clearly and mention residual risks or gaps in
  validation.
