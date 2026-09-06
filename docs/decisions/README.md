# Architectural decision records

Each record captures one decision: the situation that forced it, what was
chosen, what else was considered, and what the choice costs. They are written in
the past tense and are not revised — a decision that turns out badly gets a new
record superseding the old one, because the reasoning at the time is the useful
part.

| ADR | Decision | Why it matters |
| --- | --- | --- |
| [001](ADR-001-postgresql-as-the-warehouse.md) | PostgreSQL as the canonical warehouse | Invariants live in the schema rather than in application code |
| [002](ADR-002-declarative-yaml-contracts.md) | Declarative YAML data contracts | Onboarding a supplier is configuration, not a new pipeline |
| [003](ADR-003-scd-type-2.md) | SCD Type 2 for the product dimension | "What did this cost in March?" stays answerable |
| [004](ADR-004-reconciliation-strategy.md) | Volume and content reconciliation | Row counts cannot detect silent corruption |
| [005](ADR-005-llm-placement.md) | LLM interprets metrics, never computes them | A narrative that cannot misreport, and cannot fail a load |
| [006](ADR-006-idempotency.md) | Two-layer idempotency | Re-delivery is safe at both batch and record grain |
| [007](ADR-007-source-precedence.md) | Source precedence for multi-source mastering | Feeds enrich each other instead of fighting |
| [008](ADR-008-quarantine-grain.md) | Two-table quarantine model | Both "why was this rejected?" and "what fails most?" stay cheap |
| [009](ADR-009-sqlalchemy-core.md) | SQLAlchemy Core, not the ORM | The SQL stays visible and set-based |
