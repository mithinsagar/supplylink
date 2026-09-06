"""API response models.

Every endpoint declares its response shape. That is not ceremony: it makes the
generated OpenAPI document accurate, it stops an internal column rename from
silently becoming a breaking API change, and it guarantees no field leaves the
process that was not deliberately put in a response model — which is the simplest
form of output sanitisation there is.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="ignore")


class HealthComponent(ApiModel):
    ok: bool
    detail: str


class HealthResponse(ApiModel):
    status: str = Field(description="'ok' when every component is healthy, else 'degraded'.")
    version: str
    database: HealthComponent
    contracts: HealthComponent
    llm_configured: bool = Field(
        description="False means quality reports use the deterministic fallback."
    )


class SourceResponse(ApiModel):
    source_id: str
    display_name: str
    source_type: str
    precedence: int = Field(description="Mastering authority; lower wins.")
    is_active: bool
    contract_version: str | None = None
    contract_effective_from: date | None = None
    last_run_at: datetime | None = None
    last_run_status: str | None = None


class CanonicalFieldResponse(ApiModel):
    name: str
    type: str
    role: str
    required: bool
    description: str


class RunResponse(ApiModel):
    run_id: UUID
    source_id: str
    source_name: str | None = None
    contract_version: str
    trigger: str
    status: str
    is_replay: bool = False
    reconciliation_status: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    records_received: int
    records_valid: int
    records_quarantined: int
    quarantine_rate_pct: float | None = None
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    rows_superseded: int
    error_type: str | None = None
    error_message: str | None = None


class RunDetailResponse(RunResponse):
    stage_timings: dict[str, Any] | None = Field(
        default=None, description="Per-stage status and duration in milliseconds."
    )


class FailureCategoryResponse(ApiModel):
    rule_name: str
    field_name: str | None
    severity: str
    failure_count: int
    affected_records: int
    share_of_failures_pct: float
    message: str
    expected: str
    sample_actual_values: list[str] | None = None
    sample_source_records: list[str] | None = None


class QuarantinedRecordResponse(ApiModel):
    quarantine_id: int
    source_record_id: str | None
    source_line: int
    failure_count: int
    payload_truncated: bool
    raw_payload: dict[str, Any]
    failures: list[dict[str, Any]]


class ReconciliationCheckResponse(ApiModel):
    check_name: str
    dimension: str
    check_kind: str = Field(
        description="'volume' asks whether rows were lost; 'content' whether values survived."
    )
    status: str
    source_value: str | None
    target_value: str | None
    delta: Decimal | None
    delta_pct: Decimal | None
    threshold: dict[str, Any]
    details: dict[str, Any]


class ReconciliationResponse(ApiModel):
    run_id: UUID
    overall_status: str | None
    checks: list[ReconciliationCheckResponse]


class QualityReportResponse(ApiModel):
    run_id: UUID
    status: str = Field(description="GENERATED, FALLBACK, or FAILED.")
    provider: str
    model: str
    prompt_version: str
    temperature: Decimal | None
    summary_markdown: str
    numeric_guard: dict[str, Any] = Field(
        description="Whether every figure in the narrative matched a computed metric."
    )
    input_metrics: dict[str, Any] = Field(
        description="Exactly the metrics the model was given. Every figure is traceable to these."
    )
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    error_message: str | None
    generated_at: datetime


class ProductResponse(ApiModel):
    canonical_sku: str
    version_number: int
    product_name: str
    category: str
    supplier_id: str
    supplier_name: str | None = None
    unit_price: Decimal
    currency: str
    status: str
    effective_from: datetime
    effective_to: datetime | None
    is_current: bool
    source_id: str
    run_id: UUID


class ProductVersionResponse(ApiModel):
    canonical_sku: str
    version_number: int
    product_name: str
    category: str
    supplier_id: str
    unit_price: Decimal
    currency: str
    status: str
    effective_from: datetime
    effective_to: datetime | None
    is_current: bool
    source_id: str
    run_id: UUID
    changed_attributes: list[str] = Field(
        default_factory=list, description="Tracked attributes that differ from the prior version."
    )
    price_change_pct: Decimal | None = None


class LineageStepResponse(ApiModel):
    canonical_sku: str
    version_number: int
    unit_price: Decimal
    currency: str
    product_name: str
    effective_from: datetime
    effective_to: datetime | None
    source_id: str
    source_record_id: str
    source_line: int
    source_payload: dict[str, Any]
    payload_hash: str
    ingested_at: datetime
    run_id: UUID
    run_started_at: datetime
    run_status: str
    contract_version: str
    contract_hash: str
    reconciliation_status: str | None
    content_checksum_status: str | None
    quality_report_status: str | None


class RunRequest(BaseModel):
    """Body for triggering a run."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    force: bool = Field(
        default=False, description="Reload a payload that already loaded successfully."
    )
    skip_quality_report: bool = False


class RunTriggerResponse(ApiModel):
    run_id: UUID
    source_id: str
    status: str
    reconciliation_status: str | None
    duration_seconds: float
    records_received: int
    records_valid: int
    records_quarantined: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    rows_superseded: int
    quality_report_status: str | None = None
    skipped_reason: str | None = None


class ErrorResponse(ApiModel):
    error: str = Field(description="Exception class name.")
    detail: str = Field(description="Message safe to show a caller.")
