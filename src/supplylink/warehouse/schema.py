"""SQLAlchemy Core metadata for the warehouse.

This module describes the *shape* of the schema so that DML can be written as
composable, parameterised Core statements. It is deliberately **not** the source
of truth for DDL — that lives in ``migrations/versions/`` as readable SQL, where
partial indexes, generated columns and GiST exclusion constraints can be
expressed directly.

Two definitions of one schema is a drift risk, so
``tests/integration/test_schema_drift.py`` reflects the migrated database and
asserts that every table and column here matches it exactly. The duplication is
therefore checked rather than trusted.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Computed,
    Date,
    DateTime,
    Identity,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

METADATA = MetaData()


def _ts(name: str, **kwargs: object) -> Column:
    """A timezone-aware timestamp column. Everything is stored in UTC."""
    return Column(name, DateTime(timezone=True), **kwargs)  # type: ignore[arg-type]


def _sha256(name: str, **kwargs: object) -> Column:
    """A hex-encoded SHA-256 digest."""
    return Column(name, String(64), **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# meta — control plane
# ---------------------------------------------------------------------------

sources = Table(
    "sources",
    METADATA,
    Column("source_id", Text, primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("source_type", Text, nullable=False),
    Column("precedence", SmallInteger, nullable=False),
    Column("is_active", Boolean, nullable=False),
    _ts("created_at", nullable=False),
    _ts("updated_at", nullable=False),
    schema="meta",
)

contract_versions = Table(
    "contract_versions",
    METADATA,
    Column("contract_version_id", BigInteger, Identity(always=True), primary_key=True),
    Column("source_id", Text, nullable=False),
    Column("version", Text, nullable=False),
    _sha256("content_hash", nullable=False),
    Column("definition", JSONB, nullable=False),
    Column("effective_from", Date, nullable=False),
    _ts("registered_at", nullable=False),
    schema="meta",
)

pipeline_runs = Table(
    "pipeline_runs",
    METADATA,
    Column("run_id", UUID(as_uuid=True), primary_key=True),
    Column("source_id", Text, nullable=False),
    Column("contract_version_id", BigInteger, nullable=False),
    Column("trigger", Text, nullable=False),
    Column("status", Text, nullable=False),
    _sha256("batch_fingerprint"),
    Column("is_replay", Boolean, nullable=False),
    _ts("started_at", nullable=False),
    _ts("finished_at"),
    Column("records_received", Integer, nullable=False),
    Column("records_valid", Integer, nullable=False),
    Column("records_quarantined", Integer, nullable=False),
    Column("rows_inserted", Integer, nullable=False),
    Column("rows_updated", Integer, nullable=False),
    Column("rows_unchanged", Integer, nullable=False),
    Column("rows_superseded", Integer, nullable=False),
    Column("reconciliation_status", Text),
    Column("error_type", Text),
    Column("error_message", Text),
    schema="meta",
)

run_stages = Table(
    "run_stages",
    METADATA,
    Column("run_id", UUID(as_uuid=True), primary_key=True),
    Column("stage", Text, primary_key=True),
    Column("status", Text, nullable=False),
    _ts("started_at", nullable=False),
    _ts("finished_at"),
    Column("metrics", JSONB, nullable=False),
    Column("error_message", Text),
    schema="meta",
)


# ---------------------------------------------------------------------------
# staging — immutable landing zone
# ---------------------------------------------------------------------------

raw_records = Table(
    "raw_records",
    METADATA,
    Column("raw_record_id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("source_id", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("source_record_id", Text),
    Column("payload", JSONB, nullable=False),
    _sha256("payload_hash", nullable=False),
    _ts("ingested_at", nullable=False),
    schema="staging",
)


# ---------------------------------------------------------------------------
# core — canonical warehouse
# ---------------------------------------------------------------------------

suppliers = Table(
    "suppliers",
    METADATA,
    Column("supplier_key", BigInteger, Identity(always=True), primary_key=True),
    Column("supplier_id", Text, nullable=False, unique=True),
    Column("supplier_name", Text, nullable=False),
    Column("country", String(2)),
    Column("is_active", Boolean, nullable=False),
    _ts("created_at", nullable=False),
    _ts("updated_at", nullable=False),
    schema="core",
)

products = Table(
    "products",
    METADATA,
    Column("canonical_sku", Text, primary_key=True),
    _ts("first_seen_at", nullable=False),
    _ts("last_seen_at", nullable=False),
    Column("first_seen_run_id", UUID(as_uuid=True), nullable=False),
    Column("last_seen_run_id", UUID(as_uuid=True), nullable=False),
    schema="core",
)

product_versions = Table(
    "product_versions",
    METADATA,
    Column("product_version_id", BigInteger, Identity(always=True), primary_key=True),
    Column("canonical_sku", Text, nullable=False),
    Column("version_number", Integer, nullable=False),
    Column("product_name", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("supplier_key", BigInteger, nullable=False),
    Column("supplier_id", Text, nullable=False),
    Column("unit_price", Numeric(14, 4), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("status", Text, nullable=False),
    _sha256("record_hash", nullable=False),
    _ts("effective_from", nullable=False),
    _ts("effective_to"),
    # Generated in the database from effective_to, so the flag can never drift.
    Column("is_current", Boolean, Computed("effective_to IS NULL", persisted=True)),
    Column("source_id", Text, nullable=False),
    Column("source_precedence", SmallInteger, nullable=False),
    Column("source_record_id", Text, nullable=False),
    Column("raw_record_id", BigInteger, nullable=False),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    _ts("created_at", nullable=False),
    schema="core",
)

inventory_snapshots = Table(
    "inventory_snapshots",
    METADATA,
    Column("snapshot_id", BigInteger, Identity(always=True), primary_key=True),
    Column("canonical_sku", Text, nullable=False),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("source_id", Text, nullable=False),
    Column("quantity_available", Integer, nullable=False),
    _ts("observed_at", nullable=False),
    Column("raw_record_id", BigInteger, nullable=False),
    schema="core",
)


# ---------------------------------------------------------------------------
# dq — data-quality evidence
# ---------------------------------------------------------------------------

quarantined_records = Table(
    "quarantined_records",
    METADATA,
    Column("quarantine_id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("source_id", Text, nullable=False),
    Column("raw_record_id", BigInteger, nullable=False),
    Column("source_record_id", Text),
    Column("ordinal", Integer, nullable=False),
    Column("raw_payload", JSONB, nullable=False),
    Column("payload_truncated", Boolean, nullable=False),
    Column("failure_count", SmallInteger, nullable=False),
    _ts("created_at", nullable=False),
    schema="dq",
)

validation_failures = Table(
    "validation_failures",
    METADATA,
    Column("failure_id", BigInteger, Identity(always=True), primary_key=True),
    Column("quarantine_id", BigInteger),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("source_id", Text, nullable=False),
    Column("raw_record_id", BigInteger, nullable=False),
    Column("source_record_id", Text),
    Column("field_name", Text),
    Column("rule_name", Text, nullable=False),
    Column("rule_definition", JSONB, nullable=False),
    Column("expected", Text, nullable=False),
    Column("actual_value", Text),
    Column("message", Text, nullable=False),
    Column("severity", Text, nullable=False),
    _ts("created_at", nullable=False),
    schema="dq",
)

reconciliation_results = Table(
    "reconciliation_results",
    METADATA,
    Column("reconciliation_id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("check_name", Text, nullable=False),
    Column("check_kind", Text, nullable=False),
    Column("dimension", Text, nullable=False),
    Column("source_value", Text),
    Column("target_value", Text),
    Column("delta", Numeric(20, 6)),
    Column("delta_pct", Numeric(10, 4)),
    Column("status", Text, nullable=False),
    Column("threshold", JSONB, nullable=False),
    Column("details", JSONB, nullable=False),
    _ts("computed_at", nullable=False),
    schema="dq",
)

quality_reports = Table(
    "quality_reports",
    METADATA,
    Column("report_id", BigInteger, Identity(always=True), primary_key=True),
    Column("run_id", UUID(as_uuid=True), nullable=False),
    Column("status", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_version", Text, nullable=False),
    Column("temperature", Numeric(3, 2)),
    Column("input_metrics", JSONB, nullable=False),
    Column("summary_markdown", Text, nullable=False),
    Column("numeric_guard", JSONB, nullable=False),
    Column("prompt_tokens", Integer),
    Column("completion_tokens", Integer),
    Column("latency_ms", Integer),
    Column("error_message", Text),
    _ts("generated_at", nullable=False),
    schema="dq",
)


ALL_TABLES = (
    sources,
    contract_versions,
    pipeline_runs,
    run_stages,
    raw_records,
    suppliers,
    products,
    product_versions,
    inventory_snapshots,
    quarantined_records,
    validation_failures,
    reconciliation_results,
    quality_reports,
)
