"""Reference-data loading.

Suppliers are managed as a Type 1 dimension — new rows are inserted, existing
rows are overwritten. Supplier attribute history is not a question this platform
is asked, so versioning it would be cost with no consumer. That is a decision
worth stating rather than a default worth inheriting.

Reference data must exist before any feed loads, because referential-integrity
validation checks against it: with an empty supplier table every record in every
feed would be quarantined for an unknown supplier.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from supplylink.clock import utcnow
from supplylink.config import get_settings
from supplylink.errors import PipelineError
from supplylink.observability import get_logger
from supplylink.warehouse import schema

if TYPE_CHECKING:
    from sqlalchemy import Connection

log = get_logger(__name__)

REQUIRED_COLUMNS = ("supplier_id", "supplier_name")


def seed_suppliers(connection: Connection, path: Path | None = None) -> int:
    """Load or refresh the supplier dimension from CSV. Returns rows written."""
    source = path or Path(get_settings().data_dir) / "reference" / "suppliers.csv"
    if not source.is_file():
        raise PipelineError(
            f"supplier reference data not found at {source}. "
            f"Run: python scripts/generate_sample_data.py"
        )

    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    if not rows:
        raise PipelineError(f"{source} contains no supplier rows")

    missing = [column for column in REQUIRED_COLUMNS if column not in rows[0]]
    if missing:
        raise PipelineError(f"{source} is missing required columns {missing}")

    now = utcnow()
    values = [
        {
            "supplier_id": row["supplier_id"].strip().upper(),
            "supplier_name": row["supplier_name"].strip(),
            "country": (row.get("country") or "").strip().upper() or None,
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        for row in rows
    ]

    statement = insert(schema.suppliers).values(values)
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[schema.suppliers.c.supplier_id],
            set_={
                "supplier_name": statement.excluded.supplier_name,
                "country": statement.excluded.country,
                "is_active": statement.excluded.is_active,
                "updated_at": now,
            },
        )
    )

    log.info("suppliers seeded", extra={"rows": len(values), "source": str(source)})
    return len(values)


def supplier_count(connection: Connection) -> int:
    return int(connection.execute(select(func.count()).select_from(schema.suppliers)).scalar_one())
