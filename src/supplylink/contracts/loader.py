"""Loading, hashing and registering data contracts.

Load order matters, and each step exists for a reason:

1. **Parse** the YAML with ``yaml.safe_load``. Never ``yaml.load`` — the full
   loader can instantiate arbitrary Python objects from a document, and contracts
   are exactly the kind of file that gets edited by someone outside the core team.
2. **Hash** the parsed structure, *before* environment substitution. Hashing the
   structure rather than the file text means reformatting and comments do not
   invent a new version; hashing before substitution means the same contract has
   the same identity in dev, CI and production, where the substituted values
   differ.
3. **Substitute** ``${VAR}`` and ``${VAR:-default}`` references. This is how a
   contract names a base URL or a credential without containing one.
4. **Validate** into a :class:`~supplylink.contracts.model.Contract`, which runs
   the cross-field checks.

Registration then enforces immutability: re-registering a version whose content
has changed is rejected. A pipeline run records which contract version produced
it, and that reference means nothing if the definition behind it can quietly
change.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

import yaml
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from supplylink.clock import utcnow
from supplylink.contracts.model import Contract
from supplylink.errors import ContractError, ContractImmutabilityError
from supplylink.hashing import sha256_json
from supplylink.observability import get_logger
from supplylink.warehouse import schema

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Connection

log = get_logger(__name__)

#: ``${VAR}`` or ``${VAR:-default}``. Anything else is left alone, so a literal
#: dollar sign in a description is not mangled.
_ENV_PATTERN = re.compile(r"\$\{(?P<name>[A-Z_][A-Z0-9_]*)(?::-(?P<default>[^}]*))?\}")

CONTRACT_SUFFIXES = (".yaml", ".yml")


def substitute_env(value: Any, *, source: str) -> Any:
    """Recursively resolve ``${VAR}`` references in strings.

    A reference with no default and no matching variable is a contract error
    rather than an empty string, because an empty base URL or a blank credential
    fails much later and much more confusingly than a message naming the variable.
    """
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda match: _resolve(match, source), value)
    if isinstance(value, dict):
        return {key: substitute_env(item, source=source) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_env(item, source=source) for item in value]
    return value


def _resolve(match: re.Match[str], source: str) -> str:
    name = match.group("name")
    default = match.group("default")
    resolved = os.environ.get(name, default)
    if resolved is None:
        raise ContractError(
            f"environment variable {name} is referenced but not set, and no default "
            f"was given (write ${{{name}:-fallback}} to provide one)",
            path=source,
        )
    return resolved


def parse_contract(text: str, *, path: str) -> Contract:
    """Parse contract YAML into a validated :class:`Contract`."""
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractError(f"not valid YAML: {exc}", path=path) from exc

    if not isinstance(document, dict):
        raise ContractError("contract must be a YAML mapping at the top level", path=path)

    content_hash = sha256_json(document)
    substituted = substitute_env(document, source=path)

    try:
        contract = Contract.model_validate(
            {**substituted, "content_hash": content_hash, "path": path}
        )
    except ContractError:
        raise
    except Exception as exc:  # pydantic ValidationError and anything it wraps
        raise ContractError(_format_validation_error(exc), path=path) from exc

    return contract


def _format_validation_error(exc: Exception) -> str:
    """Render a Pydantic error as ``field: message`` lines an operator can act on."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    lines = []
    for error in errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        lines.append(f"  {location}: {error.get('msg', 'invalid')}")
    return "contract failed validation:\n" + "\n".join(lines)


def load_contract(path: Path) -> Contract:
    """Load one contract file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"cannot read contract: {exc}", path=str(path)) from exc
    return parse_contract(text, path=str(path))


def iter_contract_paths(root: Path) -> Iterator[Path]:
    """Every contract file under ``root``, in a stable order."""
    for suffix in CONTRACT_SUFFIXES:
        yield from sorted(root.rglob(f"*{suffix}"))


class ContractRegistry:
    """All contracts discovered on disk, indexed by source id."""

    def __init__(self, contracts: dict[str, Contract]) -> None:
        self._contracts = contracts

    @classmethod
    def discover(cls, root: Path) -> ContractRegistry:
        """Load every contract under ``root``.

        A duplicate source id fails the load rather than letting the last file
        win, because which file "wins" would then depend on directory ordering.
        """
        if not root.is_dir():
            raise ContractError(f"contracts directory does not exist: {root}")

        contracts: dict[str, Contract] = {}
        for path in iter_contract_paths(root):
            contract = load_contract(path)
            existing = contracts.get(contract.source_id)
            if existing is not None:
                raise ContractError(
                    f"source id {contract.source_id!r} is declared by two contracts: "
                    f"{existing.path} and {path}"
                )
            contracts[contract.source_id] = contract

        if not contracts:
            raise ContractError(f"no contract files found under {root}")

        log.debug("contracts discovered", extra={"count": len(contracts), "root": str(root)})
        return cls(contracts)

    def get(self, source_id: str) -> Contract:
        try:
            return self._contracts[source_id]
        except KeyError:
            known = ", ".join(sorted(self._contracts)) or "none"
            raise ContractError(
                f"no contract for source {source_id!r}. Known sources: {known}"
            ) from None

    def all(self) -> tuple[Contract, ...]:
        return tuple(self._contracts[key] for key in sorted(self._contracts))

    def source_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._contracts))

    def __len__(self) -> int:
        return len(self._contracts)

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._contracts


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_contract(connection: Connection, contract: Contract) -> int:
    """Upsert the source and contract version, returning the contract version id.

    Published versions are immutable: if this version is already registered with
    a different content hash, the run stops. Bumping the version is a one-line
    edit; silently reinterpreting the history of every run that referenced the
    old definition is not recoverable.
    """
    connection.execute(
        _upsert_source(contract),
    )

    existing = connection.execute(
        select(
            schema.contract_versions.c.contract_version_id,
            schema.contract_versions.c.content_hash,
        ).where(
            schema.contract_versions.c.source_id == contract.source_id,
            schema.contract_versions.c.version == contract.version,
        )
    ).one_or_none()

    if existing is not None:
        if existing.content_hash != contract.content_hash:
            raise ContractImmutabilityError(
                f"contract {contract.source_id} version {contract.version} is already "
                f"registered with a different definition "
                f"(registered {existing.content_hash[:12]}…, "
                f"on disk {contract.content_hash[:12]}…). Bump contract.version in "
                f"{contract.path} instead of editing a published version.",
                path=contract.path,
            )
        return int(existing.contract_version_id)

    contract_version_id = connection.execute(
        schema.contract_versions.insert()
        .values(
            source_id=contract.source_id,
            version=contract.version,
            content_hash=contract.content_hash,
            definition=contract.model_dump(mode="json"),
            effective_from=contract.contract.effective_from,
        )
        .returning(schema.contract_versions.c.contract_version_id)
    ).scalar_one()

    log.info(
        "contract version registered",
        extra={
            "source_id": contract.source_id,
            "version": contract.version,
            "content_hash": contract.content_hash[:12],
        },
    )
    return int(contract_version_id)


def _upsert_source(contract: Contract) -> Any:
    """Keep meta.sources in step with the contract, which is the source of truth."""
    now = utcnow()
    statement = insert(schema.sources).values(
        source_id=contract.source_id,
        display_name=contract.source.display_name,
        source_type=contract.source.type,
        precedence=contract.source.precedence,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    return statement.on_conflict_do_update(
        index_elements=[schema.sources.c.source_id],
        set_={
            "display_name": statement.excluded.display_name,
            "source_type": statement.excluded.source_type,
            "precedence": statement.excluded.precedence,
            "updated_at": now,
        },
    )
