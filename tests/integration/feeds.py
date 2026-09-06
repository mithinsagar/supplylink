"""Purpose-written feed fixtures for integration tests.

Deliberately small and hand-written rather than reusing the generated demo
dataset. A test asserting "5 records were quarantined" against generated data
breaks whenever the generator changes, and then tells you nothing about whether
the pipeline is correct.
"""

from __future__ import annotations

from typing import Any

ERP_HEADERS = ["SKU", "Name", "Category", "Price", "Currency", "Stock", "Supplier", "Status"]

GOOD_ROWS: list[dict[str, str]] = [
    {
        "SKU": "WM-001",
        "Name": "Wireless Mouse",
        "Category": "Electronics",
        "Price": "799.00",
        "Currency": "INR",
        "Stock": "120",
        "Supplier": "SUP-042",
        "Status": "ACTIVE",
    },
    {
        "SKU": "KB-002",
        "Name": "Mechanical Keyboard",
        "Category": "Peripherals",
        "Price": "6499.00",
        "Currency": "INR",
        "Stock": "40",
        "Supplier": "SUP-007",
        "Status": "ACTIVE",
    },
    {
        "SKU": "MN-003",
        "Name": "27 Inch QHD Monitor",
        "Category": "Electronics",
        "Price": "24999.00",
        "Currency": "INR",
        "Stock": "12",
        "Supplier": "SUP-001",
        "Status": "ACTIVE",
    },
]


def rows_with(**overrides: Any) -> list[dict[str, str]]:
    """A copy of the good rows with the first row's fields overridden."""
    rows = [dict(row) for row in GOOD_ROWS]
    rows[0].update({key: str(value) for key, value in overrides.items()})
    return rows
