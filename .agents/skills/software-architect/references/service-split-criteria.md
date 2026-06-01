# Service Split Criteria

Default to the modular monolith. Propose a separate service only when there is a concrete reason that an in-process module boundary will not hold.

## Good Reasons To Split

- materially different scaling profile
- strong isolation requirement for security, compliance, or blast radius
- runtime or dependency model that does not fit the main app
- independently durable worker lifecycle or operational envelope
- ownership boundary that cannot stay clean inside the monolith

## Evidence To Look For

- distinct latency or throughput needs
- different failure tolerance or availability target
- clear API or event contract with bounded data ownership
- operational tasks that would otherwise distort the main application
- a migration path that is simpler than keeping the capability in-process

## Weak Reasons To Split

- the code feels large
- the team wants cleaner folders
- a future org chart might justify it later
- "microservices" is treated as architecture quality by itself
- the current module boundary is messy and nobody wants to clean it up

## Output Requirement

When a service split is discussed, state:

1. why the monolith option is insufficient
2. who owns the new boundary
3. what the interface contract is
4. where the source of truth lives
5. what operational overhead the split introduces
