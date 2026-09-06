# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-03

First complete release.

### Ingestion
- Adapter interface with CSV and REST implementations; adapters read and never interpret
- CSV: multi-file globbing in a stable order, encoding validation, header
  verification, ragged rows quarantined rather than dropped
- REST: page-number and cursor pagination with a hard page limit, selective
  retries with exponential backoff and full jitter, `Retry-After` honoured and
  capped, credentials from the environment
- Immutable `staging.raw_records` landing zone as the lineage anchor and replay source

### Contracts
- Declarative YAML contracts covering mapping, validation, business rules,
  reconciliation thresholds, SCD configuration and mastering precedence
- Exhaustive cross-field validation at load time — unknown transforms, measures in
  `tracked_attributes`, unknown threshold keys, disallowed referential targets,
  retry lists containing 4xx statuses
- Immutable published versions, enforced by a content hash over the parsed structure
- `${VAR}` and `${VAR:-default}` environment substitution, so contracts name
  credentials without containing them

### Validation
- Field rules: `required`, `min`, `max`, `min_length`, `max_length`, `regex`,
  `allowed_values`, `unique`, `references`
- Cross-field business rules in a restricted AST evaluator; `eval` is never used
- Error and warning severities, with the pairing enforced by a database constraint
- Set-scoped rules resolved in one query per referenced column

### Warehouse
- SCD Type 2 product dimension with hash-based change detection
- Overlapping validity periods made impossible by a GiST exclusion constraint;
  one open version per SKU by a partial unique index; `is_current` a generated column
- Inventory as a fact, so a measure cannot open a dimension version
- Multi-source mastering by declared precedence, with supersession counted
- Two-layer idempotency: a batch fingerprint enforced by a partial unique index,
  and a per-record content hash
- Run accounting identities enforced by check constraints

### Reconciliation
- Volume checks: row count, quarantine rate, duplicate rate, orphan rate, distinct keys
- Content checks: a checksum computed independently in Python and in PostgreSQL,
  and per-column null rates compared run over run
- Deterministic canonicalisation with Postgres-matching decimal rounding
- Results persisted per check per run

### Quality reporting
- Provider-agnostic LLM integration (xAI, OpenAI-compatible)
- A numeric guard verifying every figure in generated output against computed metrics
- A deterministic fallback used whenever generation is unavailable or rejected
- Provider, model, prompt version, temperature, token counts and the exact input
  payload stored with every report

### Interfaces
- FastAPI application with typed response models, sanitised errors, and OpenAPI docs
- Typer CLI with Rich output, `--json` on every command, and meaningful exit codes
- Nine analytical SQL files runnable directly in `psql`

### Operations
- Alembic migrations with a working `downgrade`, verified round-trip in CI
- Multi-stage Dockerfile, non-root runtime, self-migrating Compose stack
- Structured logging with ambient run context, in console and JSON renderings
- GitHub Actions: lint, format, mypy, tests on Python 3.11–3.13 against Postgres
  16, migration round-trip, the full demo, and a Docker stack build

### Documentation
- Nine architectural decision records
- Architecture, data model, contract specification, pipeline lifecycle,
  reconciliation, sample data, and scale/cloud guides
