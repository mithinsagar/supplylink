# ADR-003 — SCD Type 2 for the product dimension

**Status:** accepted · **Date:** 2026-01

## Context

Supply-chain questions are frequently historical. Reconciling an invoice needs
the price *at the time of the order*. Explaining a margin movement needs to know
when a SKU was re-sourced. Auditing a discontinued line needs the state before
it was discontinued.

Overwriting the row answers "what is the price?" and permanently destroys the
ability to answer any of those.

## Decision

`core.product_versions` is a Type 2 dimension: a change to a tracked attribute
closes the current version and opens a new one, preserving the old.

Four decisions inside that:

**Change detection is a hash comparison.** `record_hash` is SHA-256 over the
contract's `tracked_attributes`, canonicalised. Equal hash, no new version. This
is what makes re-delivering the same file produce zero churn, however many times
it arrives.

**Measures are excluded.** `quantity_available` is not tracked. It moves on every
extract, so versioning it would open a new dimension row nightly for *every* SKU
and bury the price and naming changes that actually matter. Inventory is written
to `core.inventory_snapshots` as a fact instead. The contract model refuses to
let a contract track a measure, so this cannot be undone by configuration.

**One load timestamp per batch.** Every version a run closes closes at the same
instant every version it opens begins. Validity periods abut exactly: no gaps,
no overlaps, and a point-in-time query for any instant lands in exactly one
version. Intervals are half-open — `[effective_from, effective_to)` — so the
boundary itself is unambiguous.

**The database enforces it.** A GiST exclusion constraint forbids overlapping
periods per SKU; a partial unique index permits one open version; `is_current` is
a generated column so the flag cannot disagree with the window. Correctness does
not depend on this module being right.

## Alternatives considered

**Type 1 (overwrite).** Simplest, and destroys the history the business needs.

**Type 4 (a separate history table).** Keeps the current table narrow and fast.
It also means two tables that can disagree, dual writes, and a point-in-time
query that has to `UNION` current and historical rows. A partial index on
`is_current` gets the same read performance from one table.

**Type 6 (hybrid).** Adds current-value columns to every historical row for
"as-was versus as-is" reporting. Real value, real cost: every column stored
twice and updated on every change. Not a question this platform is asked.

**Event sourcing.** Store every change event and fold state on read. Maximum
fidelity, and it turns every ordinary query into a fold. Wrong shape for a
dimension that is read far more often than it changes.

## Consequences

**Good.** Point-in-time queries are a single indexed predicate, provably
returning one row. The change history is queryable — `sql/product_history.sql`
uses `LAG` to report which attributes changed at each step. Every version carries
the run and raw record that produced it.

**Costs.** The table grows with change volume, not SKU count; a volatile feed
needs a retention policy. Every consumer must remember `WHERE is_current` — the
`core.products_current` view exists so most never have to. Loading is a
close-then-insert rather than an update, which is why closes and inserts are
ordered inside one transaction.

**Verification.** `tests/integration/test_scd.py` asserts that each tracked
attribute opens a version, that a changed measure does not, that periods abut
exactly, that a point-in-time query returns exactly one row on either side of a
boundary, and that the database rejects overlapping or empty periods written
directly in SQL.
