# ADR-006 — Two-layer idempotency

**Status:** accepted · **Date:** 2026-02

## Context

Files get re-delivered. A scheduler retries, an operator re-runs a failed job, a
supplier sends Tuesday's drop twice. If the second delivery duplicates warehouse
records, the platform is unsafe to operate — and people respond by being careful
instead of by fixing it, which does not scale.

"Process each file once" is also not quite the requirement. Files overlap
partially: a full extract shares most of its rows with yesterday's. The
guarantee needed is that *identical content produces no change*, whatever
container it arrives in.

## Decision

Two independent mechanisms at two different grains.

**Batch fingerprint — a fast guard.** SHA-256 over the per-record payload hashes
in source order. Recorded on the run, and enforced by a partial unique index:

```sql
CREATE UNIQUE INDEX pipeline_runs_batch_idem_uk
    ON meta.pipeline_runs (source_id, batch_fingerprint)
    WHERE status = 'SUCCEEDED' AND batch_fingerprint IS NOT NULL AND NOT is_replay;
```

A re-delivered payload is detected before any work happens and the run is marked
`SKIPPED`. Because the fingerprint is over content, renaming the file does not
defeat it.

**Record hash — the actual guarantee.** SHA-256 over each record's tracked
attributes. Equal hash, no new dimension version. This holds regardless of how
records were batched, so a partially-overlapping file — a different fingerprint,
so the batch guard does not fire — still produces zero churn for the rows that
have not changed.

The batch guard is an optimisation; the record hash is correctness.

**Enforcement is in the database, not in a check.** The naive approach —
`SELECT`, decide, `INSERT` — is a time-of-check/time-of-use race that two
concurrent runs of the same file both pass. Here the run is created first and the
fingerprint recorded once the batch is read; the unique index then makes the
*second* run's transition to `SUCCEEDED` fail. The pre-check that runs before it
exists only to produce a friendly message early.

`--force` marks the run a replay, which the index's `NOT is_replay` predicate
excludes — so a deliberate reload is possible, is explicitly labelled, and is
distinguishable in the run history from an ordinary load.

## Alternatives considered

**Filename or modification time.** Breaks the moment a supplier renames a file or
a copy resets the timestamp. Metadata about content is not content.

**A processed-files table.** Same idea as the fingerprint but at file grain, so it
misses partial overlaps entirely and says nothing about records.

**Upsert everything and let the database sort it out.** Works for Type 1. With
Type 2 the question is not "does this key exist?" but "has anything I track
changed?", which needs the hash comparison regardless.

**Application-level locking.** Solves the concurrency race and adds an
availability problem, plus lock lifetime management. A unique index is a lock
that cannot leak.

## Consequences

**Good.** Re-running is always safe, which means retries can be automatic.
Concurrent duplicate loads are impossible rather than unlikely. A forced replay
is auditable. `tests/integration/test_pipeline.py` covers all four cases:
identical payload skipped, rename still skipped, forced replay produces
`unchanged`, and a partially-overlapping file inserts only what is new.

**Costs.** The fingerprint requires reading and hashing the whole batch before
the guard can fire, so a duplicate is detected after ingestion rather than before
it — cheap here, and at much larger scale a cheap pre-check on file size or a
manifest would come first. A source that legitimately re-sends identical content
expecting a fresh run must pass `--force`; the `SKIPPED` status and its reason
make that discoverable.
