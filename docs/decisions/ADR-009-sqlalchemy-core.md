# ADR-009 — SQLAlchemy Core, not the ORM

**Status:** accepted · **Date:** 2026-01

## Context

Something has to construct SQL, bind parameters safely and map results. The
options run from raw `psycopg` at one end to a full ORM at the other, and the
choice shapes how readable the interesting part of this project — the SQL — will
be.

The workload is set-based: bulk staging inserts, a dimension merge, aggregate
reconciliation queries, analytical reads. There are almost no
load-object-mutate-save cycles.

## Decision

**SQLAlchemy Core** for DML, with a deliberate split:

* **Statement construction** (inserts, the SCD merge, lookups) uses Core
  expressions — composable, dialect-aware, parameters always bound.
* **Analytical SQL** (reconciliation, point-in-time, lineage, run summaries)
  lives as files under `sql/` and is loaded through `load_sql`. These can be run
  in `psql` and reviewed by a data engineer who does not read Python.
* **DDL** lives in Alembic migrations as readable SQL, because the constraints
  that carry the design — a GiST exclusion constraint, a partial unique index, a
  stored generated column — have no idiomatic Alembic representation.

`warehouse/schema.py` mirrors the schema as Core metadata for DML. That is a
duplication, so `tests/integration/test_schema.py` reflects the migrated database
and asserts the two never diverge — turning the duplication from a liability into
a checked guarantee.

## Alternatives considered

**The SQLAlchemy ORM.** Identity map, unit of work, relationship loading — all
machinery for object graphs, none of which this workload has. It would add a
layer between the reader and the SQL, in a project whose SQL is the point. Bulk
operations would need the bulk API anyway, which is Core with extra steps.

**Raw psycopg.** Maximum transparency and maximum string handling. Composing a
`WHERE` clause conditionally becomes string concatenation, which is where SQL
injection lives. Core keeps parameters bound without hiding the statement.

**A query builder like PyPika.** Solves construction, not connections, pooling,
transactions or type adaptation — all of which would then need assembling
separately.

**All SQL in files, no builder.** Attractive for readability. It falls down on
genuinely dynamic statements: the target-side checksum's column list comes from
the contract, and would have to be templated into a file — which is both less
safe and less clear than generating it in Python from a validated allow-list.

## Consequences

**Good.** The SQL is visible. Bulk inserts use multi-row `INSERT … RETURNING`
with `sort_by_parameter_order`, so ids come back matched to their records in one
round trip. Transaction boundaries are explicit context managers, which is what
made the boundary design in `docs/pipeline-lifecycle.md` expressible.

**Costs.** Two schema representations, kept honest by the drift test. Results are
`Row` objects rather than domain models, so callers index by attribute — fine
here, and it is why the API has explicit response models rather than serialising
rows directly. Core's expression language is its own dialect to learn.

**A trap worth recording.** SQLAlchemy scans a whole SQL file for
colon-prefixed bind parameters, **including inside comments** — a colon-prefixed
word written casually in a comment becomes a required parameter and the statement
fails at runtime with a confusing error. It also skips a placeholder immediately
followed by a colon, so the `::type` cast shorthand leaves the parameter unbound;
`CAST(... AS type)` is used instead. Both are noted in `db.load_sql`.
