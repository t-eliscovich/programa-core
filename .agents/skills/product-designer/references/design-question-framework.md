# Design Question Framework

Use questions to eliminate hidden product and UX assumptions, not to outsource repo discovery.

## Discover First

Inspect the repo before asking about:

- the current route, template, or module entry point
- existing shared partials, macros, form patterns, and table patterns
- similar flows already present in operational modules
- product constraints already documented in README or docs
- whether the request is a new flow or a refinement of existing UI

## Ask The User About Intent

Ask when the answer materially changes the design:

- the primary user and moment of use
- the job to be done and success criteria
- the required primary action or decision
- the amount of density, detail, or explanation the surface should carry
- the importance of mobile support for this flow
- the desired tone, terminology, or level of formality
- what is explicitly out of scope

## High-Leverage Question Areas

### Outcome

- What should the user know, decide, or complete by the end of this surface?
- What friction matters most to remove?

### Flow

- Where does the user enter this flow?
- What happens immediately after the primary action?

### Hierarchy

- What must be visible above the fold or before scroll?
- What information can be deferred or progressively disclosed?

### Actions And Guardrails

- What is the primary action?
- What risks, required reviews, or irreversible consequences must be visible first?

### States

- What loading, empty, stale, pending, error, and success states are required?
- Which state is most likely in real usage?

### Tone

- Should the copy feel operational, educational, reassuring, or urgent?
- Which domain terms must stay explicit instead of being softened?

## Assumption Policy

- If an unanswered question would change the flow, hierarchy, or required states, ask.
- If an unanswered question affects only a low-risk detail, choose a strong default and record it as a default.
- Label assumptions explicitly. Never present them as confirmed user intent.
