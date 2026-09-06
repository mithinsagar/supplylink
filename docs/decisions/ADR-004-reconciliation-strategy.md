# ADR-004 — Volume and content reconciliation

**Status:** accepted · **Date:** 2026-02

## Context

Most pipelines reconcile by comparing row counts. It is cheap, it catches
dropped rows, and it is where the effort usually stops.

It is also blind to the failure that matters most. Suppose a mapping bug
divides every price by 100, or a transform silently truncates product names, or
a currency column shifts by one position. Every row arrives. The count matches
exactly. Reconciliation reports green, and the warehouse is wrong.

Counting rows tells you nothing about whether the values survived.

## Decision

Every check is classified as one of two kinds, and both run on every load.

**Volume checks** ask *did we lose rows?* — row count, quarantine rate,
duplicate rate, orphan rate, distinct key count.

**Content checks** ask *did the values survive?* — a checksum over the
contract's declared columns, and per-column null rates compared run over run.

The content checksum is the substantive one, and how it is computed is the
point:

> The source-side digest is computed **in Python** from the mapped records. The
> target-side digest is computed **by PostgreSQL** from what actually landed.

They agree only if the canonicalisation, the decimal rounding mode, the NULL
encoding, the field separator and the column order all match across two
independent implementations. Comparing a checksum against one computed by the
same code over the same in-memory objects proves only that the objects did not
change in between. Two implementations agreeing proves the data survived
mapping, serialisation, the driver, the type system and storage.

Determinism required deciding several things explicitly:

* **NULL** encodes as `\N`, distinguishable from an empty string — otherwise
  "absent" and "empty" would hash alike, and that distinction is exactly what
  reconciliation exists to catch.
* **Decimals** quantise to scale 4 with `ROUND_HALF_UP`, because that is what
  Postgres `NUMERIC` does. Python's default `ROUND_HALF_EVEN` disagrees on exact
  halves — `0.00005` becomes `0.0000` in Python and `0.0001` in Postgres — and
  that single disagreement would report a phantom mismatch.
* **Fields** join with U+001F and rows with U+001E, control characters that
  cannot occur in CSV or JSON payloads, so no escaping is needed.
* **Row digests are sorted then hashed**, making the set checksum independent of
  row order.
* **Never `hash()`** — Python's builtin is randomised per interpreter by
  `PYTHONHASHSEED` and useless for anything persisted. A test runs the hash in a
  subprocess under two different seeds to prove it.

Scope: the content checksum covers the SKUs a run actually applied. Records
superseded by a more authoritative source are excluded deliberately — their
warehouse value is *supposed* to differ from what this feed sent, and including
them would report a mismatch for behaviour working exactly as designed.

## Alternatives considered

**Row counts only.** Cheap and blind, as above.

**Full row-by-row comparison.** Maximum fidelity and maximum cost: every row
shipped back to the client and compared. A checksum gets the same yes/no answer
in one round trip; when it says no, `sql/quarantine_detail.sql` and the lineage
queries narrow it down.

**Sampling.** Compare a random subset. Cheaper, and it converts a definite answer
into a probabilistic one. For an integrity guarantee that is the wrong trade.

**One checksum implementation, used on both sides.** Much simpler, and it proves
almost nothing — the interesting failures are precisely those that occur
*between* the two representations.

**XOR-aggregating digests instead of sorting.** Order-independent in O(1) memory
and the right answer at warehouse scale. Sorting is used here because the volume
is small and sorted-concatenation is stronger against digest collisions; the
scale path is documented in `docs/reconciliation.md`.

## Consequences

**Good.** Silent corruption is detectable, and the test suite proves it: a price
is altered directly in the warehouse, every volume check still passes, and the
content checksum fails. The demo shows the same thing live. Results are persisted
per check per run, so "when did this start?" is a query rather than an
archaeology exercise.

**Costs.** Determinism is fragile in a specific way — anything that changes
canonicalisation changes every digest, which is why checksum column order is
part of a contract's version. Computing the target digest is a full scan of the
affected SKUs. Thresholds must be tuned per source: a supplier drop held to the
system-of-record standard means a permanently red dashboard, which is the
fastest way to make people stop reading it.

**What is not claimed.** A passing reconciliation does not mean the data is
*correct* — the source may be wrong, and the pipeline will faithfully reproduce
that. It means the pipeline did not corrupt what it was given. Those are
different guarantees and only the second is achievable here.
