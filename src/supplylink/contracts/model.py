"""The data-contract schema.

A contract is the complete, declarative description of one source feed: where to
read it, how its columns map onto the canonical model, what makes a record
valid, which attributes are worth versioning, and how tightly it should
reconcile. Onboarding a supplier is writing one of these files.

The models below are strict on purpose. ``extra="forbid"`` means a typo like
``requred: true`` fails the contract at load time with the offending key named,
rather than silently disabling a rule and letting bad data through — a failure
mode that is nearly impossible to spot in production.

Validation happens in two passes:

* **Field-level**, by Pydantic, as the YAML is parsed.
* **Cross-field**, in :meth:`Contract.model_post_init` — that transforms exist,
  that mapped fields are canonical, that ``tracked_attributes`` are versionable,
  that rule expressions parse. These are the errors that would otherwise surface
  halfway through a production run.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from supplylink.canonical import (
    BUSINESS_KEY,
    CANONICAL_FIELD_NAMES,
    CANONICAL_FIELDS_BY_NAME,
    DEFAULT_TRACKED_ATTRIBUTES,
    VERSIONABLE_FIELDS,
    CanonicalType,
)
from supplylink.errors import ContractError
from supplylink.reconciliation.metrics import THRESHOLD_KEYS
from supplylink.transformation.functions import available_transforms
from supplylink.validation.expressions import Expression
from supplylink.validation.references import describe_targets, is_referenceable

Severity = Literal["error", "warning"]
SemVer = Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Source access
# ---------------------------------------------------------------------------


class CsvSourceConfig(_Strict):
    """How to find and parse a CSV drop."""

    path_glob: str = Field(description="Glob relative to the data directory, or absolute.")
    encoding: str = "utf-8"
    delimiter: str = Field(default=",", min_length=1, max_length=1)
    quotechar: str = Field(default='"', min_length=1, max_length=1)
    required_headers: list[str] = Field(
        default_factory=list,
        description=(
            "Headers that must be present. A missing header is a pipeline failure, "
            "not a record failure: the file is not the file we were promised, and "
            "loading it would silently null out a column."
        ),
    )
    skip_blank_lines: bool = True
    max_field_size_bytes: int = Field(default=1_000_000, ge=1_024)


class RestAuthConfig(_Strict):
    """Authentication for a REST feed. Secrets are named, never inlined."""

    type: Literal["none", "bearer", "header"] = "none"
    token_env: str | None = Field(
        default=None,
        description="Environment variable holding the credential. The value never appears in YAML.",
    )
    header_name: str = "Authorization"


class RestPaginationConfig(_Strict):
    """Pagination strategy. ``none`` means the feed returns everything at once."""

    style: Literal["none", "page_number", "cursor"] = "none"
    page_param: str = "page"
    size_param: str = "pageSize"
    page_size: int = Field(default=100, ge=1, le=10_000)
    first_page: int = 1
    cursor_param: str = "cursor"
    next_cursor_path: str | None = Field(
        default=None, description="Dotted path to the next cursor in the response body."
    )
    total_pages_path: str | None = Field(
        default=None, description="Dotted path to the total page count."
    )
    max_pages: int = Field(
        default=1_000,
        ge=1,
        description="Hard stop. A feed whose cursor never advances must not loop forever.",
    )


class RestSourceConfig(_Strict):
    """How to call a vendor API, including its failure behaviour."""

    base_url: str
    path: str = "/"
    method: Literal["GET", "POST"] = "GET"
    records_path: str | None = Field(
        default=None,
        description="Dotted path to the record array. Omit when the body is the array itself.",
    )
    query: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    auth: RestAuthConfig = Field(default_factory=RestAuthConfig)
    pagination: RestPaginationConfig = Field(default_factory=RestPaginationConfig)
    timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    max_attempts: int = Field(
        default=4,
        ge=1,
        le=10,
        description="Total attempts per request, including the first.",
    )
    backoff_initial_seconds: float = Field(default=0.5, gt=0)
    backoff_max_seconds: float = Field(default=8.0, gt=0)
    retry_on_status: list[int] = Field(
        default=[408, 425, 429, 500, 502, 503, 504],
        description=(
            "Only transient statuses. A 401 or a 404 will not fix itself, and "
            "retrying it wastes time and hammers the vendor."
        ),
    )

    @field_validator("retry_on_status")
    @classmethod
    def _reject_permanent_statuses(cls, statuses: list[int]) -> list[int]:
        permanent = {400, 401, 403, 404, 405, 409, 410, 422}
        offenders = sorted(set(statuses) & permanent)
        if offenders:
            raise ValueError(
                f"refusing to retry permanently-failing statuses {offenders}; "
                "these indicate a bad request or bad credentials, not a transient fault"
            )
        return statuses


class SourceConfig(_Strict):
    """Identity and access configuration for one feed."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    display_name: str = Field(min_length=1)
    type: Literal["csv", "rest"]
    precedence: int = Field(
        ge=1,
        le=999,
        description=(
            "Mastering authority; lower wins. When two feeds describe the same SKU, "
            "a higher number cannot overwrite what a lower number wrote."
        ),
    )
    description: str | None = None
    csv: CsvSourceConfig | None = None
    rest: RestSourceConfig | None = None

    @model_validator(mode="after")
    def _require_matching_block(self) -> SourceConfig:
        if self.type == "csv" and self.csv is None:
            raise ValueError("source.type is 'csv' but no source.csv block was provided")
        if self.type == "rest" and self.rest is None:
            raise ValueError("source.type is 'rest' but no source.rest block was provided")
        if self.type == "csv" and self.rest is not None:
            raise ValueError("source.type is 'csv' but a source.rest block was also provided")
        if self.type == "rest" and self.csv is not None:
            raise ValueError("source.type is 'rest' but a source.csv block was also provided")
        return self


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


class FieldMapping(_Strict):
    """How one canonical field is derived from the source record."""

    source: str | None = Field(
        default=None,
        alias="from",
        description="Source column or JSON key. Omit when the value is a constant.",
    )
    constant: Any = Field(
        default=None, description="A fixed value, for feeds that do not carry the field at all."
    )
    default: Any = Field(default=None, description="Used when the source value resolves to null.")
    transforms: list[str] = Field(
        default_factory=list, description="Applied in order, before the lookup and the default."
    )
    lookup: str | None = Field(
        default=None, description="Name of a table in the contract's `lookups` section."
    )
    lookup_passthrough: bool = Field(
        default=False,
        description=(
            "When true an unmapped value passes through unchanged. When false — the "
            "default — it becomes null, so an unrecognised code is caught by the "
            "required rule instead of quietly entering the warehouse."
        ),
    )

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="after")
    def _require_a_value_source(self) -> FieldMapping:
        if self.source is None and self.constant is None:
            raise ValueError("mapping needs either 'from' (a source column) or 'constant'")
        if self.source is not None and self.constant is not None:
            raise ValueError("mapping cannot set both 'from' and 'constant'")
        return self


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class ForeignKeyRef(_Strict):
    """A referential-integrity target in the warehouse."""

    table: str = Field(pattern=r"^(meta|core|dq|staging)\.[a-z_]+$")
    column: str = Field(pattern=r"^[a-z_]+$")


class FieldValidation(_Strict):
    """The rules that apply to one canonical field."""

    required: bool = False
    type: CanonicalType | None = Field(
        default=None,
        description=(
            "Declared for documentation and cross-checked against the canonical "
            "model at load time. Enforcement happens during mapping, where coercion "
            "to the canonical type either succeeds or produces a 'type' failure."
        ),
    )
    min: Decimal | None = None
    max: Decimal | None = None
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=1)
    regex: str | None = None
    allowed_values: list[str] | None = Field(default=None, min_length=1)
    unique: bool = False
    references: ForeignKeyRef | None = None
    severity: Severity = "error"

    @field_validator("regex")
    @classmethod
    def _must_compile(cls, pattern: str | None) -> str | None:
        if pattern is None:
            return None
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regular expression {pattern!r}: {exc}") from exc
        return pattern

    @model_validator(mode="after")
    def _bounds_must_be_ordered(self) -> FieldValidation:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"min ({self.min}) is greater than max ({self.max})")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError(f"min_length ({self.min_length}) is greater than max_length")
        return self


class BusinessRule(_Strict):
    """A cross-field rule, evaluated per record after field validation."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    expr: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: Severity = "error"


# ---------------------------------------------------------------------------
# Reconciliation and history
# ---------------------------------------------------------------------------


class Threshold(_Strict):
    """Warn and fail levels for one reconciliation metric.

    ``warn`` must not exceed ``fail`` — a threshold pair that cannot ever warn is
    almost always a typo, and a silent one.
    """

    warn: Decimal = Field(ge=0)
    fail: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def _warn_before_fail(self) -> Threshold:
        if self.warn > self.fail:
            raise ValueError(f"warn ({self.warn}) must not exceed fail ({self.fail})")
        return self


class ReconciliationConfig(_Strict):
    checksum_columns: list[str] = Field(
        default_factory=list,
        min_length=1,
        description=(
            "Canonical columns included in the content checksum, in this order. "
            "Order is part of the hash and must stay stable across contract versions."
        ),
    )
    null_rate_columns: list[str] = Field(default_factory=list)
    thresholds: dict[str, Threshold] = Field(default_factory=dict)


class ScdConfig(_Strict):
    tracked_attributes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TRACKED_ATTRIBUTES),
        min_length=1,
        description="Attributes whose change opens a new version. Measures are not eligible.",
    )


class ContractMeta(_Strict):
    version: SemVer
    effective_from: date
    owner: str | None = None
    description: str | None = None


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class Contract(BaseModel):
    """A complete, validated data contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    contract: ContractMeta
    source: SourceConfig
    identity: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Currently one key: source_record_id, naming the source's own identifier column."
        ),
    )
    mapping: dict[str, FieldMapping] = Field(min_length=1)
    lookups: dict[str, dict[str, str]] = Field(default_factory=dict)
    validation: dict[str, FieldValidation] = Field(default_factory=dict)
    rules: list[BusinessRule] = Field(default_factory=list)
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)
    scd: ScdConfig = Field(default_factory=ScdConfig)

    # Populated by the loader; not part of the YAML.
    content_hash: str = Field(default="", exclude=True)
    path: str = Field(default="", exclude=True)

    @property
    def source_id(self) -> str:
        return self.source.id

    @property
    def version(self) -> str:
        return self.contract.version

    @property
    def source_record_id_field(self) -> str | None:
        """The source column carrying the record's own identifier, if declared."""
        return self.identity.get("source_record_id")

    def compiled_rules(self) -> list[tuple[BusinessRule, Expression]]:
        """Business rules paired with their parsed expressions."""
        return [(rule, Expression(rule.expr)) for rule in self.rules]

    def model_post_init(self, _context: Any) -> None:
        """Cross-field checks that field-level validation cannot express."""
        self._check_mapped_fields_are_canonical()
        self._check_business_key_is_mapped()
        self._check_transforms_exist()
        self._check_lookups_resolve()
        self._check_validation_targets()
        self._check_declared_types_agree()
        self._check_references_are_allowed()
        self._check_tracked_attributes()
        self._check_checksum_columns()
        self._check_rules_compile()
        self._check_thresholds_known()

    # -- individual checks ------------------------------------------------

    def _check_mapped_fields_are_canonical(self) -> None:
        unknown = sorted(set(self.mapping) - CANONICAL_FIELD_NAMES)
        if unknown:
            known = ", ".join(sorted(CANONICAL_FIELD_NAMES))
            raise ContractError(
                f"mapping targets fields that are not in the canonical model: {unknown}. "
                f"Canonical fields are: {known}"
            )

    def _check_business_key_is_mapped(self) -> None:
        if BUSINESS_KEY not in self.mapping:
            raise ContractError(
                f"mapping must produce the business key {BUSINESS_KEY!r}; "
                "without it records cannot be identified across sources"
            )

    def _check_transforms_exist(self) -> None:
        known = set(available_transforms())
        for field_name, mapping in self.mapping.items():
            unknown = [name for name in mapping.transforms if name not in known]
            if unknown:
                raise ContractError(
                    f"mapping.{field_name} uses unregistered transforms {unknown}. "
                    f"Available: {', '.join(sorted(known))}"
                )

    def _check_lookups_resolve(self) -> None:
        for field_name, mapping in self.mapping.items():
            if mapping.lookup is not None and mapping.lookup not in self.lookups:
                available = ", ".join(sorted(self.lookups)) or "none defined"
                raise ContractError(
                    f"mapping.{field_name} references lookup {mapping.lookup!r} "
                    f"which is not defined. Available lookups: {available}"
                )
        unused = sorted(self.lookups.keys() - {m.lookup for m in self.mapping.values() if m.lookup})
        if unused:
            raise ContractError(
                f"lookups {unused} are defined but never referenced by any mapping; "
                "an unused lookup is almost always a mapping that forgot to name it"
            )

    def _check_validation_targets(self) -> None:
        unmapped = sorted(set(self.validation) - set(self.mapping))
        if unmapped:
            raise ContractError(
                f"validation targets fields that the mapping never produces: {unmapped}"
            )

    def _check_declared_types_agree(self) -> None:
        for field_name, rules in self.validation.items():
            if rules.type is None:
                continue
            expected = CANONICAL_FIELDS_BY_NAME[field_name].type
            if rules.type is not expected:
                raise ContractError(
                    f"validation.{field_name}.type is {rules.type.value!r} but the canonical "
                    f"model declares {expected.value!r}. A contract cannot redefine a "
                    "canonical field's type — that is what makes the model canonical."
                )

    def _check_references_are_allowed(self) -> None:
        """Constrain foreign-key checks to an explicit allow-list.

        The table name reaches SQL, so "it matches a pattern" is not a strong
        enough guarantee. Only the targets registered in
        :mod:`supplylink.validation.references` are legal.
        """
        for field_name, rules in self.validation.items():
            reference = rules.references
            if reference is None:
                continue
            if not is_referenceable(reference.table, reference.column):
                raise ContractError(
                    f"validation.{field_name}.references points at "
                    f"{reference.table}({reference.column}), which is not a permitted "
                    f"referential-integrity target. Permitted: {describe_targets()}"
                )

    def _check_tracked_attributes(self) -> None:
        tracked = set(self.scd.tracked_attributes)
        unknown = sorted(tracked - CANONICAL_FIELD_NAMES)
        if unknown:
            raise ContractError(f"scd.tracked_attributes contains unknown fields: {unknown}")
        measures = sorted(tracked - VERSIONABLE_FIELDS)
        if measures:
            raise ContractError(
                f"scd.tracked_attributes contains measures {measures}. Tracking a measure "
                "would open a new dimension version on every run and bury the real changes; "
                "measures belong in core.inventory_snapshots."
            )
        unmapped = sorted(tracked - set(self.mapping))
        if unmapped:
            raise ContractError(
                f"scd.tracked_attributes names fields this contract never maps: {unmapped}. "
                "They would hash as null on every run and make every record look changed."
            )

    def _check_checksum_columns(self) -> None:
        columns = self.reconciliation.checksum_columns
        unknown = sorted(set(columns) - CANONICAL_FIELD_NAMES)
        if unknown:
            raise ContractError(f"reconciliation.checksum_columns has unknown fields: {unknown}")
        unmapped = sorted(set(columns) - set(self.mapping))
        if unmapped:
            raise ContractError(
                f"reconciliation.checksum_columns names unmapped fields: {unmapped}"
            )
        if len(columns) != len(set(columns)):
            raise ContractError("reconciliation.checksum_columns contains duplicates")
        unknown_nulls = sorted(set(self.reconciliation.null_rate_columns) - set(self.mapping))
        if unknown_nulls:
            raise ContractError(
                f"reconciliation.null_rate_columns names unmapped fields: {unknown_nulls}"
            )

    def _check_rules_compile(self) -> None:
        names = [rule.name for rule in self.rules]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ContractError(f"duplicate business rule names: {duplicates}")
        for rule in self.rules:
            expression = Expression(rule.expr)  # raises ContractError on bad syntax or constructs
            unknown = sorted(expression.identifiers - CANONICAL_FIELD_NAMES)
            if unknown:
                raise ContractError(
                    f"rule {rule.name!r} references fields that are not canonical: {unknown}"
                )

    def _check_thresholds_known(self) -> None:
        unknown = sorted(set(self.reconciliation.thresholds) - THRESHOLD_KEYS)
        if unknown:
            raise ContractError(
                f"reconciliation.thresholds has unknown keys: {unknown}. "
                f"Known thresholds: {', '.join(sorted(THRESHOLD_KEYS))}"
            )
