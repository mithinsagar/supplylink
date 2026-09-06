"""Exception hierarchy.

SupplyLink draws a hard line between two classes of failure, because they demand
opposite responses:

* :class:`RecordError` — one input record is unusable. The record is quarantined
  with full forensic detail and the run continues. Rejecting a 15,000-row batch
  because 3 rows have a negative price destroys more value than it protects.
* :class:`PipelineError` — the run itself cannot be trusted (bad contract,
  database down, source unreachable). The run is marked FAILED and stops. Loading
  a partial batch and calling it a success is how silent data corruption starts.

See ``docs/pipeline-lifecycle.md`` for the full policy.
"""

from __future__ import annotations

from typing import Any


class SupplyLinkError(Exception):
    """Base class for every error raised by SupplyLink."""


# --------------------------------------------------------------------------
# Record-level: quarantine and keep going
# --------------------------------------------------------------------------


class RecordError(SupplyLinkError):
    """A single source record could not be processed. Quarantine it."""


class MappingError(RecordError):
    """A source field could not be mapped or coerced into its canonical form."""

    def __init__(self, field: str, message: str, value: Any = None) -> None:
        self.field = field
        self.value = value
        #: The reason on its own, without the field prefix. Callers that already
        #: know the field use this to avoid "unit_price: unit_price: ..." messages.
        self.detail = message
        super().__init__(f"{field}: {message}")


# --------------------------------------------------------------------------
# Pipeline-level: fail the run
# --------------------------------------------------------------------------


class PipelineError(SupplyLinkError):
    """The run cannot continue safely."""


class ConfigurationError(PipelineError):
    """Application configuration is missing or invalid."""


class ContractError(PipelineError):
    """A data contract is malformed, unreadable, or internally inconsistent."""

    def __init__(self, message: str, *, path: str | None = None) -> None:
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


class ContractImmutabilityError(ContractError):
    """A published contract version was edited without a version bump.

    Contract versions are immutable by design: a run records which contract
    version produced it, and that reference is worthless if the definition behind
    it can change silently.
    """


class IngestionError(PipelineError):
    """A source could not be read (missing file, unreachable API, bad encoding)."""


class SourceUnavailableError(IngestionError):
    """A remote source stayed unavailable after the configured retries."""


class WarehouseError(PipelineError):
    """A canonical-load or SCD operation failed."""


class ReconciliationError(PipelineError):
    """Reconciliation could not be computed (as distinct from reconciliation failing)."""


class DuplicateBatchError(PipelineError):
    """This exact payload was already loaded successfully by an earlier run."""

    def __init__(self, fingerprint: str, previous_run_id: str) -> None:
        self.fingerprint = fingerprint
        self.previous_run_id = previous_run_id
        super().__init__(
            f"batch fingerprint {fingerprint[:12]}… was already loaded by run "
            f"{previous_run_id}; re-run with --force to load it again"
        )
