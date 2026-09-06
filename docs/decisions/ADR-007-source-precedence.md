# ADR-007 — Source precedence for multi-source mastering

**Status:** accepted · **Date:** 2026-02

## Context

Three feeds describe overlapping product catalogues. They disagree. The ERP says
a SKU is `DISCONTINUED`; ACME's drop has no lifecycle column and asserts
`ACTIVE`. The ERP has the properly-cased product name; ACME lower-cases
everything and the casing cannot be reconstructed.

With no mastering rule, whichever feed ran most recently wins. The nightly ERP
extract and the weekly supplier drop would then overwrite each other on
alternating runs, and `core.product_versions` would fill with version churn that
records nothing about the products and everything about the schedule.

## Decision

Every contract declares a `precedence` — lower is more authoritative. The ERP is
10, supplier drops are 20, the vendor API is 30.

When a feed's record differs from the current version, the loader compares its
precedence with the precedence recorded **on that version**:

* more or equally authoritative → the change is applied, opening a new version;
* less authoritative → the record is counted as **superseded** and skipped.

A less authoritative feed can still introduce a SKU the others have not seen —
which is most of the value of ingesting it. It simply cannot overwrite what a
more authoritative feed has already mastered.

Precedence is stored on each version (`source_precedence`), captured at write
time, so changing a contract's precedence later cannot retroactively rewrite the
interpretation of history.

`rows_superseded` is a first-class run counter, included in the accounting
identity the database enforces. Suppression is visible, not silent.

## Alternatives considered

**Last writer wins.** No configuration, and the flip-flop described above.

**Attribute-level mastering** — the ERP masters price, the supplier masters lead
time. Genuinely better for a mature master-data platform, and considerably more
machinery: per-attribute precedence, per-attribute lineage, and a much harder
question about what a "version" then means. Record-level precedence gets most of
the benefit at a fraction of the complexity, and the upgrade path is open.

**Merge non-null values from every source.** Sounds accommodating, produces
records that never existed in any system and that nobody can reconcile against a
source.

**Quarantine conflicts for human review.** Correct for a small number of
high-value conflicts, unworkable at feed volume — the demo alone produces 28
supersessions from one supplier drop.

## Consequences

**Good.** Deterministic regardless of run order. Feeds enrich rather than fight.
The demo shows it concretely: ACME contributes one SKU the ERP feed could not
supply, while 28 of its records are correctly superseded. Precedence is one
number in a contract, reviewable in a diff.

**Costs.** Someone must decide the ordering, and it is a genuine data-governance
decision rather than a technical one. A high supersession count can look alarming
without context — it usually means healthy overlap, not a problem, which is why
the counter is surfaced and explained rather than hidden. Content reconciliation
excludes superseded records, since their warehouse value is *meant* to differ
from what the feed sent.
