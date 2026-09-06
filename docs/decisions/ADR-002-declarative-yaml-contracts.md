# ADR-002 — Declarative YAML data contracts

**Status:** accepted · **Date:** 2026-01

## Context

The failure mode this platform exists to avoid is one pipeline per supplier.
That path is easy to start down — the first supplier gets `load_acme.py`, the
second gets `load_globalparts.py` — and within a year there are forty scripts
that each parse, rename, validate and load slightly differently. Fixing a
validation bug then means finding it forty times, and nobody can answer "what
rules apply to this feed?" without reading code.

## Decision

A source is described entirely by one YAML contract: where to read it, how its
columns map onto the canonical model, what makes a record valid, which
attributes are worth versioning, and how tightly it must reconcile. The engine
interprets contracts at runtime. **No supplier-specific Python exists anywhere in
the codebase.**

Onboarding is: write a contract, drop the data, run the pipeline.

Two properties keep this from decaying:

**Contracts are validated exhaustively at load time.** `extra="forbid"` turns a
typo like `requred: true` into a startup error naming the key, rather than a
silently disabled rule. Cross-field checks — that mapped fields are canonical,
that transforms exist, that tracked attributes are not measures, that thresholds
are known keys — all run before a single record is read.

**Contract versions are immutable.** The content hash is computed over the parsed
structure, so reformatting and comments are free but a semantic change is not.
Re-registering a version with different content is refused. A run records which
contract version produced it, and that reference is worthless if the definition
behind it can change.

## Alternatives considered

**Python configuration objects.** Type-checked and refactorable, but it puts
supplier onboarding back behind a deployment and inside a language most data
stewards do not write. It also makes "show me every rule applied to ACME" a code
reading exercise.

**JSON Schema.** A real standard with good tooling, and genuinely strong for the
validation section. It has nothing to say about mapping, transforms, lookups,
mastering precedence or reconciliation thresholds, so half the contract would
end up outside it.

**A database-backed rules table with a UI.** The right end state at scale. It is
not the right *start*: contracts in Git get review, diffs, blame and rollback for
free, and those are worth more than a form for the first several dozen sources.

**Great Expectations or a similar framework.** Considerably more validation
capability than this needs, and it covers only validation — mapping, SCD
configuration and mastering would still need somewhere to live. The dependency
would be large and the concept count high for the fraction actually used.

## Consequences

**Good.** A new supplier is a reviewable file. All three shipped sources
demonstrate it, including a REST feed whose field names share nothing with the
ERP's. The contract is documentation that cannot drift from behaviour, because
it *is* the behaviour. `supplylink contracts validate` checks every contract in
CI.

**Costs.** YAML has real sharp edges — Norway problem, tabs, indentation — which
strict parsing and a clear error format mitigate but do not remove. A genuinely
novel normalisation still needs a registered transform, which is a code change;
this is deliberate, since the alternative is an expression language inside the
mapping section and eventually a programming language nobody designed. Contract
validation errors have to be excellent, because they are the entire developer
experience of onboarding.

**Boundary.** Contracts describe *sources*. The canonical model itself is code
(`supplylink/canonical.py`), because it is the shared vocabulary every contract
is written against — a contract that could redefine it would not be canonical.
