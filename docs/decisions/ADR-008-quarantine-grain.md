# ADR-008 — A two-table quarantine model

**Status:** accepted · **Date:** 2026-02

## Context

Invalid records must never be silently dropped, and two very different questions
get asked about them:

1. *Why was **this** record rejected?* — an investigation, needing the payload
   whole and every rule it broke.
2. *What is failing **most**?* — a ranking across thousands of records, needing
   aggregation by rule.

A single table serves one of these well and the other badly.

## Decision

Two tables at two grains, joined by a foreign key.

**`dq.quarantined_records` — one row per rejected record.** Holds the payload
whole, the source record id, the line number, and the failure count.

**`dq.validation_failures` — one row per rule violation.** A record failing three
rules produces three rows, each with the field, the rule, the rule's own
definition, what was expected, and what actually arrived.

Storing the rule *definition* as JSONB matters: when a contract later tightens
`max` from 10,000,000 to 1,000,000, historical failures still show the threshold
that was actually applied.

Warnings live in `validation_failures` with a null `quarantine_id` — the signal
is kept, the record still loads. A `CHECK` constraint enforces the pairing, so
severity and quarantine membership cannot disagree:

```sql
CHECK ((severity = 'error'   AND quarantine_id IS NOT NULL)
    OR (severity = 'warning' AND quarantine_id IS NULL))
```

## Alternatives considered

**One row per record, failures in a JSON array.** Compact, and it turns "which
rule fails most often?" into a lateral unnest that cannot use an index. The
ranking query is the one run most often.

**One row per rule failure, payload duplicated on each.** Aggregates well and
stores a payload three times for a record that broke three rules — which is
exactly the record most likely to be large.

**Quarantine to files or object storage.** Cheap and scalable, and it puts the
evidence somewhere you cannot join to a run, a contract version or a
reconciliation result. The lineage story is worth more than the storage saving at
this volume.

**Reject the whole batch on any failure.** Simple, defensible for a small
critical feed, and destructive at supplier scale: 14,000 good records held back
by 3 bad ones. Record-level quarantine is the whole reason the platform can
accept an imperfect feed.

## Consequences

**Good.** Both questions are cheap. `sql/run_failures.sql` is a plain `GROUP BY`
with a window function for shares. `sql/quarantine_detail.sql` aggregates
failures back onto each record, so an investigation returns one row per record
rather than a cartesian smear of payload copies. Every failure links back to its
raw record, and from there to the exact source line.

**Costs.** Two tables and two inserts per rejected record, inside the load
transaction. Payloads are bounded — anything over `max_quarantine_payload_bytes`
is replaced by a marker recording what was dropped and how big it was, so one
pathological file cannot dominate the table. Quarantine grows with failure
volume and needs a retention policy in production; `ON DELETE CASCADE` from the
run makes pruning a single delete.

**Transaction boundary.** Quarantine is written in the *same* transaction as the
canonical load. If the load rolls back, quarantine rolls back with it —
quarantine from a run that never completed would misrepresent what the warehouse
actually saw.
