# ADR-001 — PostgreSQL as the canonical warehouse

**Status:** accepted · **Date:** 2026-01

## Context

SupplyLink needs somewhere to hold the canonical product model, its full
version history, the staging landing zone, quarantined records, reconciliation
results and run metadata. The workload is:

* modest volume — hundreds of thousands of SKUs, not billions;
* write-heavy in bursts (a nightly extract), read-light and analytical;
* heavily relational — runs reference contracts, versions reference raw records,
  quarantine references both;
* and above all **integrity-critical**. The whole value proposition is that the
  warehouse can be trusted, so the guarantees have to be real.

## Decision

PostgreSQL 16 is the single store for everything, with logical separation by
schema (`meta`, `staging`, `core`, `dq`) rather than by database.

Crucially, the invariants that make the model trustworthy are expressed **as
schema constraints, not as application code**:

| Invariant | Mechanism |
| --- | --- |
| A SKU has at most one open version | partial unique index `WHERE is_current` |
| A SKU's validity periods never overlap | `EXCLUDE USING gist` over `tstzrange`, via `btree_gist` |
| `is_current` always agrees with `effective_to` | stored generated column |
| A payload loads successfully at most once | partial unique index on `(source_id, batch_fingerprint)` |
| A successful run's counters balance | `CHECK` constraints on `meta.pipeline_runs` |
| An error always has a quarantined record; a warning never does | `CHECK` on `dq.validation_failures` |
| A denormalised supplier code matches its surrogate key | composite foreign key |

## Alternatives considered

**A columnar analytical store (DuckDB, ClickHouse).** Better at wide scans, but
neither offers the constraint vocabulary above. The exclusion constraint in
particular has no equivalent, and it is the reason a point-in-time query is
*provably* unambiguous rather than unambiguous-if-the-loader-is-correct. At this
volume the analytical advantage is theoretical.

**SQLite.** Trivial to run, and genuinely tempting for a project this size. No
exclusion constraints, no `JSONB`, weak typing, and single-writer. Testing
against SQLite and deploying against Postgres would mean the guarantees were
never actually tested.

**Separate databases per layer.** Real isolation, but staging, canonical and
quality data would no longer share a transaction, and the lineage joins that
make this project interesting would become application-side joins across
connections. Schemas give the organisational benefit at none of that cost.

**A document store.** The data is relational and the questions are relational.
Modelling it as documents would push every join into application code.

## Consequences

**Good.** Constraints hold even if the loader is wrong — the integration tests
demonstrate this by attempting invalid writes directly in SQL and watching the
database refuse them. One transaction spans validation, quarantine and canonical
load. `JSONB` stores raw payloads and metrics without a second store. Everything
is standard and operationally familiar.

**Costs.** A single database is a single point of failure; production would need
replication and backups. The exclusion constraint needs the `btree_gist`
extension, which requires superuser to install — so Compose, CI and the local
setup instructions all create it explicitly. Very large loads would eventually
want `COPY` rather than multi-row `INSERT`; the batching layer is already the
place that would change.

**What changes at 100M rows.** Partition `core.product_versions` by
`effective_from` and `staging.raw_records` by run date; move the checksum
aggregate from sorted-digest-concatenation to a Postgres-side `bit_xor`; switch
bulk paths to `COPY`. None of these change the model, only how it is written and
scanned.
