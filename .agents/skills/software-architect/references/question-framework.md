# Question Framework

Use questions to eliminate hidden assumptions, not to outsource repo discovery.

## Discover First

Inspect the repo before asking about:

- existing routes, modules, and ownership boundaries
- current types, schemas, and API shapes
- local naming patterns and package structure
- whether a similar flow or abstraction already exists
- current tests and validation surfaces

## Ask The User About Intent

Ask when the answer materially changes the plan:

- the real goal and success criteria
- the primary users or operators
- scale, latency, throughput, or freshness expectations
- data ownership and source of truth
- external integrations and side effects
- security, permissions, or compliance needs
- compatibility, migration, or rollout constraints
- what is explicitly out of scope

## High-Leverage Question Areas

### Outcome

- What user or business outcome must this feature change?
- How will we know the design is successful?

### Boundaries

- Which part of the system should own this behavior?
- Is the request actually for a new service, or for a cleaner module boundary?

### Data And Execution

- What state must be stored, derived, or synchronized?
- Does the work happen inline, asynchronously, or as a long-running workflow?

### Performance

- What request volume, data volume, or latency target matters here?
- Where would failure or slowness be most expensive?

### Risk

- What mistakes would be hard to reverse later?
- What compatibility or migration constraints already exist?

## Assumption Policy

- If an unanswered question would change architecture, stop and ask.
- If an unanswered question only affects a low-risk implementation detail, choose a strong default and record it as a default.
- Always label assumptions explicitly. Never present them as confirmed facts.
