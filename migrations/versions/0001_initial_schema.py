"""Initial SupplyLink warehouse schema.

The DDL is written as explicit SQL rather than Alembic's Python operations for
two reasons:

1. Several of the invariants that make this warehouse trustworthy have no
   idiomatic Alembic representation — a GiST exclusion constraint over
   ``tstzrange``, a partial unique index, a stored generated column.
2. The schema is the most reviewable artefact in the project. A data engineer
   should be able to read it in ``psql`` without reading Python.

``supplylink.warehouse.schema`` mirrors this structure as SQLAlchemy Core
metadata for DML. ``tests/integration/test_schema_drift.py`` asserts the two
never diverge.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None


UPGRADE_SQL = """
-- =====================================================================
-- Extensions
-- =====================================================================
-- btree_gist lets a GiST exclusion constraint mix an equality operator on a
-- scalar column (canonical_sku) with an overlap operator on a range. That
-- combination is what makes overlapping SCD-2 validity periods physically
-- impossible rather than merely discouraged.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- =====================================================================
-- Schemas — one per architectural layer, so grants and intent are explicit
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS meta;     -- control plane: sources, contracts, runs
CREATE SCHEMA IF NOT EXISTS staging;  -- source-shaped landing zone, never mutated
CREATE SCHEMA IF NOT EXISTS core;     -- canonical warehouse
CREATE SCHEMA IF NOT EXISTS dq;       -- data quality: quarantine, reconciliation, reports

COMMENT ON SCHEMA meta    IS 'Pipeline control plane. Answers "what ran, against which contract, and how did it end?"';
COMMENT ON SCHEMA staging IS 'Immutable landing zone holding source payloads exactly as received. The replay source of truth.';
COMMENT ON SCHEMA core    IS 'Canonical, source-independent warehouse model.';
COMMENT ON SCHEMA dq      IS 'Data-quality evidence: quarantine, reconciliation results, generated reports.';


-- =====================================================================
-- meta.sources — one row per registered feed
-- =====================================================================
CREATE TABLE meta.sources (
    source_id     TEXT        PRIMARY KEY,
    display_name  TEXT        NOT NULL,
    source_type   TEXT        NOT NULL,
    precedence    SMALLINT    NOT NULL,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT sources_type_ck       CHECK (source_type IN ('csv', 'rest')),
    CONSTRAINT sources_precedence_ck CHECK (precedence BETWEEN 1 AND 999),
    CONSTRAINT sources_id_format_ck  CHECK (source_id ~ '^[a-z][a-z0-9_]{2,63}$')
);

COMMENT ON COLUMN meta.sources.precedence IS
    'Mastering authority; lower wins. When two feeds describe the same SKU, a '
    'less authoritative feed cannot overwrite attributes already mastered by a '
    'more authoritative one. See ADR-007.';


-- =====================================================================
-- meta.contract_versions — published contracts are immutable
-- =====================================================================
CREATE TABLE meta.contract_versions (
    contract_version_id BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id           TEXT        NOT NULL REFERENCES meta.sources (source_id) ON DELETE RESTRICT,
    version             TEXT        NOT NULL,
    content_hash        CHAR(64)    NOT NULL,
    definition          JSONB       NOT NULL,
    effective_from      DATE        NOT NULL,
    registered_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT contract_versions_source_version_uk UNIQUE (source_id, version),
    CONSTRAINT contract_versions_semver_ck CHECK (version ~ '^[0-9]+\\.[0-9]+\\.[0-9]+$'),
    CONSTRAINT contract_versions_hash_ck   CHECK (content_hash ~ '^[0-9a-f]{64}$')
);

COMMENT ON TABLE meta.contract_versions IS
    'Every run references the exact contract that produced it. The stored '
    'definition makes that reference self-contained: a run stays explainable '
    'even after the YAML file on disk has moved on.';
COMMENT ON COLUMN meta.contract_versions.content_hash IS
    'SHA-256 over the canonicalised contract. Re-registering a version with a '
    'different hash is rejected — published versions are immutable.';


-- =====================================================================
-- meta.pipeline_runs — the audit spine
-- =====================================================================
CREATE TABLE meta.pipeline_runs (
    run_id                UUID        PRIMARY KEY,
    source_id             TEXT        NOT NULL REFERENCES meta.sources (source_id),
    contract_version_id   BIGINT      NOT NULL REFERENCES meta.contract_versions (contract_version_id),
    trigger               TEXT        NOT NULL,
    status                TEXT        NOT NULL,
    batch_fingerprint     CHAR(64),
    is_replay             BOOLEAN     NOT NULL DEFAULT FALSE,

    started_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at           TIMESTAMPTZ,

    records_received      INTEGER     NOT NULL DEFAULT 0,
    records_valid         INTEGER     NOT NULL DEFAULT 0,
    records_quarantined   INTEGER     NOT NULL DEFAULT 0,
    rows_inserted         INTEGER     NOT NULL DEFAULT 0,
    rows_updated          INTEGER     NOT NULL DEFAULT 0,
    rows_unchanged        INTEGER     NOT NULL DEFAULT 0,
    rows_superseded       INTEGER     NOT NULL DEFAULT 0,

    reconciliation_status TEXT,
    error_type            TEXT,
    error_message         TEXT,

    CONSTRAINT pipeline_runs_trigger_ck CHECK (trigger IN ('cli', 'api', 'test', 'demo')),
    CONSTRAINT pipeline_runs_status_ck  CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED')),
    CONSTRAINT pipeline_runs_recon_ck   CHECK (reconciliation_status IS NULL
                                               OR reconciliation_status IN ('PASS', 'WARN', 'FAIL')),
    CONSTRAINT pipeline_runs_fingerprint_ck CHECK (batch_fingerprint IS NULL
                                                   OR batch_fingerprint ~ '^[0-9a-f]{64}$'),

    -- A terminal run must have an end time; a running one must not.
    CONSTRAINT pipeline_runs_finish_ck CHECK (
        (status = 'RUNNING' AND finished_at IS NULL)
        OR (status <> 'RUNNING' AND finished_at IS NOT NULL)
    ),
    CONSTRAINT pipeline_runs_duration_ck CHECK (finished_at IS NULL OR finished_at >= started_at),

    -- Accounting identities, enforced only for runs that claim success. If the
    -- loader ever loses a record these constraints turn a silent data bug into
    -- a loud transaction failure.
    CONSTRAINT pipeline_runs_intake_balance_ck CHECK (
        status <> 'SUCCEEDED'
        OR records_received = records_valid + records_quarantined
    ),
    CONSTRAINT pipeline_runs_load_balance_ck CHECK (
        status <> 'SUCCEEDED'
        OR records_valid = rows_inserted + rows_updated + rows_unchanged + rows_superseded
    ),
    CONSTRAINT pipeline_runs_counts_nonneg_ck CHECK (
        LEAST(records_received, records_valid, records_quarantined,
              rows_inserted, rows_updated, rows_unchanged, rows_superseded) >= 0
    )
);

-- Idempotency, enforced by the database rather than by a hopeful SELECT.
-- At most one non-replay successful load may exist per (source, exact payload).
-- A deliberate reload is admitted only by labelling it a replay.
CREATE UNIQUE INDEX pipeline_runs_batch_idem_uk
    ON meta.pipeline_runs (source_id, batch_fingerprint)
    WHERE status = 'SUCCEEDED' AND batch_fingerprint IS NOT NULL AND NOT is_replay;

CREATE INDEX pipeline_runs_source_started_idx ON meta.pipeline_runs (source_id, started_at DESC);
CREATE INDEX pipeline_runs_status_idx         ON meta.pipeline_runs (status) WHERE status = 'RUNNING';

COMMENT ON COLUMN meta.pipeline_runs.batch_fingerprint IS
    'SHA-256 over the canonicalised source payload. Batch-level idempotency guard; '
    'record-level correctness still rests on core.product_versions.record_hash. See ADR-006.';


-- =====================================================================
-- meta.run_stages — per-stage timing and metrics
-- =====================================================================
CREATE TABLE meta.run_stages (
    run_id        UUID        NOT NULL REFERENCES meta.pipeline_runs (run_id) ON DELETE CASCADE,
    stage         TEXT        NOT NULL,
    status        TEXT        NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    metrics       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,

    PRIMARY KEY (run_id, stage),
    CONSTRAINT run_stages_stage_ck CHECK (stage IN (
        'ingestion', 'mapping', 'validation', 'canonical_load', 'reconciliation', 'quality_report'
    )),
    CONSTRAINT run_stages_status_ck CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED'))
);


-- =====================================================================
-- staging.raw_records — immutable landing zone
-- =====================================================================
CREATE TABLE staging.raw_records (
    raw_record_id    BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id           UUID        NOT NULL REFERENCES meta.pipeline_runs (run_id) ON DELETE CASCADE,
    source_id        TEXT        NOT NULL REFERENCES meta.sources (source_id),
    ordinal          INTEGER     NOT NULL,
    source_record_id TEXT,
    payload          JSONB       NOT NULL,
    payload_hash     CHAR(64)    NOT NULL,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT raw_records_run_ordinal_uk UNIQUE (run_id, ordinal),
    CONSTRAINT raw_records_ordinal_ck     CHECK (ordinal >= 0),
    CONSTRAINT raw_records_hash_ck        CHECK (payload_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX raw_records_run_idx           ON staging.raw_records (run_id);
CREATE INDEX raw_records_source_record_idx ON staging.raw_records (source_id, source_record_id);

COMMENT ON TABLE staging.raw_records IS
    'Source payloads as received, before any mapping. Nothing in this table is '
    'ever updated. It is both the lineage anchor for every downstream row and '
    'the source for replaying a run without re-reading the original file.';
COMMENT ON COLUMN staging.raw_records.ordinal IS
    'Position within the batch (CSV line, API array index). Guarantees deterministic, '
    'reproducible processing order and makes an error message point at a real line.';
COMMENT ON COLUMN staging.raw_records.source_record_id IS
    'Natural identifier as it appeared in the source. Nullable: a malformed record '
    'may not carry one, and it is still worth keeping.';


-- =====================================================================
-- core.suppliers — reference dimension (Type 1)
-- =====================================================================
CREATE TABLE core.suppliers (
    supplier_key  BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    supplier_id   TEXT        NOT NULL UNIQUE,
    supplier_name TEXT        NOT NULL,
    country       CHAR(2),
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Lets dependants carry the readable natural key alongside the surrogate
    -- while a composite foreign key guarantees the pair stays consistent.
    CONSTRAINT suppliers_key_id_uk    UNIQUE (supplier_key, supplier_id),
    CONSTRAINT suppliers_id_format_ck CHECK (supplier_id ~ '^SUP-[0-9]{3}$'),
    CONSTRAINT suppliers_country_ck   CHECK (country IS NULL OR country ~ '^[A-Z]{2}$')
);

COMMENT ON TABLE core.suppliers IS
    'Reference data, managed as Type 1 (overwrite). Supplier attribute history is '
    'not a question this platform is asked, so versioning it would be cost without value.';


-- =====================================================================
-- core.products — durable SKU registry (the hub)
-- =====================================================================
CREATE TABLE core.products (
    canonical_sku     TEXT        PRIMARY KEY,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    first_seen_run_id UUID        NOT NULL REFERENCES meta.pipeline_runs (run_id),
    last_seen_run_id  UUID        NOT NULL REFERENCES meta.pipeline_runs (run_id),

    CONSTRAINT products_sku_format_ck CHECK (canonical_sku ~ '^[A-Z0-9]{3,32}$'),
    CONSTRAINT products_seen_order_ck CHECK (last_seen_at >= first_seen_at)
);

COMMENT ON TABLE core.products IS
    'One durable row per canonical SKU. Gives facts and versions a stable foreign-key '
    'target, which a Type-2 table cannot provide because its business key repeats.';


-- =====================================================================
-- core.product_versions — Type 2 slowly changing dimension
-- =====================================================================
CREATE TABLE core.product_versions (
    product_version_id BIGINT        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_sku      TEXT          NOT NULL REFERENCES core.products (canonical_sku) ON DELETE RESTRICT,
    version_number     INTEGER       NOT NULL,

    -- Tracked descriptive attributes. A change to any of these opens a new version.
    product_name       TEXT          NOT NULL,
    category           TEXT          NOT NULL,
    supplier_key       BIGINT        NOT NULL,
    supplier_id        TEXT          NOT NULL,
    unit_price         NUMERIC(14,4) NOT NULL,
    currency           CHAR(3)       NOT NULL,
    status             TEXT          NOT NULL DEFAULT 'ACTIVE',

    record_hash        CHAR(64)      NOT NULL,

    effective_from     TIMESTAMPTZ   NOT NULL,
    effective_to       TIMESTAMPTZ,
    is_current         BOOLEAN       GENERATED ALWAYS AS (effective_to IS NULL) STORED,

    -- Lineage: every version points back at the exact input row that created it.
    source_id          TEXT          NOT NULL REFERENCES meta.sources (source_id),
    source_precedence  SMALLINT      NOT NULL,
    source_record_id   TEXT          NOT NULL,
    raw_record_id      BIGINT        NOT NULL REFERENCES staging.raw_records (raw_record_id),
    run_id             UUID          NOT NULL REFERENCES meta.pipeline_runs (run_id),
    created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT product_versions_supplier_fk FOREIGN KEY (supplier_key, supplier_id)
        REFERENCES core.suppliers (supplier_key, supplier_id),

    CONSTRAINT product_versions_sku_version_uk UNIQUE (canonical_sku, version_number),
    CONSTRAINT product_versions_version_ck     CHECK (version_number > 0),
    CONSTRAINT product_versions_price_ck       CHECK (unit_price >= 0),
    CONSTRAINT product_versions_currency_ck    CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT product_versions_status_ck      CHECK (status IN ('ACTIVE', 'DISCONTINUED')),
    CONSTRAINT product_versions_hash_ck        CHECK (record_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT product_versions_validity_ck    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

-- Exactly one open version per SKU. Doubles as the access path for every
-- current-state query, which is the overwhelmingly common read.
CREATE UNIQUE INDEX product_versions_current_uk
    ON core.product_versions (canonical_sku)
    WHERE is_current;

-- No SKU may have two versions whose validity periods overlap. This is the
-- invariant that makes a point-in-time query provably return exactly one row.
ALTER TABLE core.product_versions
    ADD CONSTRAINT product_versions_no_overlap_ex
    EXCLUDE USING gist (
        canonical_sku WITH =,
        tstzrange(effective_from, effective_to) WITH &&
    );

CREATE INDEX product_versions_pit_idx      ON core.product_versions (canonical_sku, effective_from DESC);
CREATE INDEX product_versions_run_idx      ON core.product_versions (run_id);
CREATE INDEX product_versions_supplier_idx ON core.product_versions (supplier_key) WHERE is_current;

COMMENT ON TABLE core.product_versions IS
    'Type 2 dimension. Descriptive attributes only — on-hand quantity is a measure '
    'and lives in core.inventory_snapshots, because tracking it here would open a '
    'new version on every single run and drown the real changes. See ADR-003.';
COMMENT ON COLUMN core.product_versions.record_hash IS
    'SHA-256 over the canonicalised tracked attributes. Equal hash means no change, '
    'so a re-delivered file produces no new version.';
COMMENT ON COLUMN core.product_versions.is_current IS
    'Generated from effective_to, so the flag and the validity window can never disagree.';
COMMENT ON COLUMN core.product_versions.source_precedence IS
    'Mastering authority of the feed that wrote this version, captured at write time '
    'so a later precedence change cannot rewrite history.';


-- =====================================================================
-- core.inventory_snapshots — measure, one observation per SKU per run
-- =====================================================================
CREATE TABLE core.inventory_snapshots (
    snapshot_id        BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_sku      TEXT        NOT NULL REFERENCES core.products (canonical_sku) ON DELETE RESTRICT,
    run_id             UUID        NOT NULL REFERENCES meta.pipeline_runs (run_id) ON DELETE CASCADE,
    source_id          TEXT        NOT NULL REFERENCES meta.sources (source_id),
    quantity_available INTEGER     NOT NULL,
    observed_at        TIMESTAMPTZ NOT NULL,
    raw_record_id      BIGINT      NOT NULL REFERENCES staging.raw_records (raw_record_id),

    CONSTRAINT inventory_snapshots_sku_run_uk UNIQUE (canonical_sku, run_id),
    CONSTRAINT inventory_snapshots_qty_ck     CHECK (quantity_available >= 0)
);

CREATE INDEX inventory_snapshots_sku_time_idx ON core.inventory_snapshots (canonical_sku, observed_at DESC);
CREATE INDEX inventory_snapshots_run_idx      ON core.inventory_snapshots (run_id);


-- =====================================================================
-- dq.quarantined_records — one row per rejected record
-- =====================================================================
CREATE TABLE dq.quarantined_records (
    quarantine_id    BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id           UUID        NOT NULL REFERENCES meta.pipeline_runs (run_id) ON DELETE CASCADE,
    source_id        TEXT        NOT NULL REFERENCES meta.sources (source_id),
    raw_record_id    BIGINT      NOT NULL REFERENCES staging.raw_records (raw_record_id),
    source_record_id TEXT,
    ordinal          INTEGER     NOT NULL,
    raw_payload      JSONB       NOT NULL,
    payload_truncated BOOLEAN    NOT NULL DEFAULT FALSE,
    failure_count    SMALLINT    NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT quarantined_records_raw_uk       UNIQUE (raw_record_id),
    CONSTRAINT quarantined_records_failures_ck  CHECK (failure_count > 0)
);

CREATE INDEX quarantined_records_run_idx ON dq.quarantined_records (run_id);

COMMENT ON TABLE dq.quarantined_records IS
    'The record that was rejected, kept whole so it can be investigated and replayed. '
    'Paired with dq.validation_failures at a finer grain — see ADR-008.';


-- =====================================================================
-- dq.validation_failures — one row per (record, rule) violation
-- =====================================================================
CREATE TABLE dq.validation_failures (
    failure_id       BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    quarantine_id    BIGINT      REFERENCES dq.quarantined_records (quarantine_id) ON DELETE CASCADE,
    run_id           UUID        NOT NULL REFERENCES meta.pipeline_runs (run_id) ON DELETE CASCADE,
    source_id        TEXT        NOT NULL REFERENCES meta.sources (source_id),
    raw_record_id    BIGINT      NOT NULL REFERENCES staging.raw_records (raw_record_id),
    source_record_id TEXT,
    field_name       TEXT,
    rule_name        TEXT        NOT NULL,
    rule_definition  JSONB       NOT NULL,
    expected         TEXT        NOT NULL,
    actual_value     TEXT,
    message          TEXT        NOT NULL,
    severity         TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT validation_failures_severity_ck CHECK (severity IN ('error', 'warning')),
    -- An error quarantines its record; a warning is recorded but lets it through.
    CONSTRAINT validation_failures_quarantine_ck CHECK (
        (severity = 'error' AND quarantine_id IS NOT NULL)
        OR (severity = 'warning' AND quarantine_id IS NULL)
    )
);

CREATE INDEX validation_failures_run_rule_idx  ON dq.validation_failures (run_id, rule_name);
CREATE INDEX validation_failures_run_field_idx ON dq.validation_failures (run_id, field_name);
CREATE INDEX validation_failures_quarantine_idx ON dq.validation_failures (quarantine_id);

COMMENT ON TABLE dq.validation_failures IS
    'Grain is one rule violation, not one bad record. A record that fails three rules '
    'produces three rows, which is what makes "top failure categories" a plain GROUP BY '
    'instead of JSON archaeology.';


-- =====================================================================
-- dq.reconciliation_results — one row per check per run
-- =====================================================================
CREATE TABLE dq.reconciliation_results (
    reconciliation_id BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id            UUID        NOT NULL REFERENCES meta.pipeline_runs (run_id) ON DELETE CASCADE,
    check_name        TEXT        NOT NULL,
    check_kind        TEXT        NOT NULL,
    dimension         TEXT        NOT NULL DEFAULT '*',
    source_value      TEXT,
    target_value      TEXT,
    delta             NUMERIC(20,6),
    delta_pct         NUMERIC(10,4),
    status            TEXT        NOT NULL,
    threshold         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    details           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    computed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT reconciliation_results_run_check_uk UNIQUE (run_id, check_name, dimension),
    CONSTRAINT reconciliation_results_status_ck    CHECK (status IN ('PASS', 'WARN', 'FAIL')),
    CONSTRAINT reconciliation_results_kind_ck      CHECK (check_kind IN ('volume', 'content'))
);

CREATE INDEX reconciliation_results_run_idx ON dq.reconciliation_results (run_id);

COMMENT ON COLUMN dq.reconciliation_results.check_kind IS
    'volume = "did we lose rows?"; content = "did the values survive intact?". '
    'Only content checks can detect a transformation that silently corrupted data. See ADR-004.';


-- =====================================================================
-- dq.quality_reports — LLM narrative, plus the exact numbers it was given
-- =====================================================================
CREATE TABLE dq.quality_reports (
    report_id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id            UUID        NOT NULL REFERENCES meta.pipeline_runs (run_id) ON DELETE CASCADE,
    status            TEXT        NOT NULL,
    provider          TEXT        NOT NULL,
    model             TEXT        NOT NULL,
    prompt_version    TEXT        NOT NULL,
    temperature       NUMERIC(3,2),
    input_metrics     JSONB       NOT NULL,
    summary_markdown  TEXT        NOT NULL,
    numeric_guard     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    latency_ms        INTEGER,
    error_message     TEXT,
    generated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT quality_reports_run_uk    UNIQUE (run_id),
    CONSTRAINT quality_reports_status_ck CHECK (status IN ('GENERATED', 'FALLBACK', 'FAILED'))
);

COMMENT ON COLUMN dq.quality_reports.input_metrics IS
    'The complete payload handed to the model. Stored so any number in the narrative '
    'can be traced back to a metric the pipeline actually computed.';
COMMENT ON COLUMN dq.quality_reports.numeric_guard IS
    'Result of verifying every numeric literal in the generated text against the '
    'metrics above. An unverifiable figure downgrades the report to FALLBACK. See ADR-005.';


-- =====================================================================
-- Views
-- =====================================================================
CREATE VIEW core.products_current AS
SELECT
    pv.canonical_sku,
    pv.product_name,
    pv.category,
    pv.supplier_id,
    s.supplier_name,
    s.country            AS supplier_country,
    pv.unit_price,
    pv.currency,
    pv.status,
    inv.quantity_available,
    pv.version_number,
    pv.effective_from,
    pv.source_id,
    pv.run_id            AS last_changed_by_run,
    pv.record_hash
FROM core.product_versions pv
JOIN core.suppliers s USING (supplier_key)
LEFT JOIN LATERAL (
    SELECT i.quantity_available
    FROM core.inventory_snapshots i
    WHERE i.canonical_sku = pv.canonical_sku
    ORDER BY i.observed_at DESC, i.snapshot_id DESC
    LIMIT 1
) inv ON TRUE
WHERE pv.is_current;

COMMENT ON VIEW core.products_current IS
    'Current canonical state: the open dimension version joined to the most recent '
    'inventory observation. A view rather than a table, so it cannot drift.';


CREATE VIEW dq.run_failure_summary AS
SELECT
    vf.run_id,
    vf.rule_name,
    vf.field_name,
    vf.severity,
    count(*)                                  AS failure_count,
    count(DISTINCT vf.raw_record_id)          AS affected_records,
    min(vf.message)                           AS sample_message,
    (array_agg(vf.actual_value ORDER BY vf.failure_id))[1:3] AS sample_values
FROM dq.validation_failures vf
GROUP BY vf.run_id, vf.rule_name, vf.field_name, vf.severity;

COMMENT ON VIEW dq.run_failure_summary IS
    'Failure categories per run, ranked by count. Feeds both the CLI failure table '
    'and the metrics handed to the quality-report model.';
"""


DOWNGRADE_SQL = """
DROP VIEW IF EXISTS dq.run_failure_summary;
DROP VIEW IF EXISTS core.products_current;
DROP TABLE IF EXISTS dq.quality_reports;
DROP TABLE IF EXISTS dq.reconciliation_results;
DROP TABLE IF EXISTS dq.validation_failures;
DROP TABLE IF EXISTS dq.quarantined_records;
DROP TABLE IF EXISTS core.inventory_snapshots;
DROP TABLE IF EXISTS core.product_versions;
DROP TABLE IF EXISTS core.products;
DROP TABLE IF EXISTS core.suppliers;
DROP TABLE IF EXISTS staging.raw_records;
DROP TABLE IF EXISTS meta.run_stages;
DROP TABLE IF EXISTS meta.pipeline_runs;
DROP TABLE IF EXISTS meta.contract_versions;
DROP TABLE IF EXISTS meta.sources;
DROP SCHEMA IF EXISTS dq;
DROP SCHEMA IF EXISTS core;
DROP SCHEMA IF EXISTS staging;
DROP SCHEMA IF EXISTS meta;
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    op.execute(DOWNGRADE_SQL)
