#!/usr/bin/env python3
"""Generate the synthetic supply-chain dataset used by the demo and tests.

Deterministic by construction: a fixed seed and a fixed catalogue mean the demo
produces identical numbers on every machine, so the figures quoted in the README
are the figures a reviewer will actually see.

The dataset is built to make validation and reconciliation *do something*. Each
defect below is planted on purpose and is documented in
``docs/sample-data.md``:

======================  =========================================================
Defect                  Why it is here
======================  =========================================================
negative inventory      exercises the ``min`` rule, the largest failure category
unknown supplier        exercises referential integrity against core.suppliers
duplicate SKU           exercises batch-scoped uniqueness
invalid currency        exercises allowed_values
missing required field  exercises the ``required`` rule
non-numeric price       exercises type coercion failure
unknown category code   exercises lookup-miss becoming null, then required
zero price with stock   exercises a cross-field business rule
discontinued with stock exercises a *warning*, which does not quarantine
======================  =========================================================

The three feeds also disagree about how to write the same identifiers — the ERP
uses ``WM-001``, ACME uses ``wm001``, the vendor API uses ``wm_001`` — which is
what the mapping layer exists to reconcile.

Usage::

    python scripts/generate_sample_data.py [--out DATA_DIR]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from decimal import Decimal
from pathlib import Path
from typing import Any

SEED = 20260314

# Rates at which each realistic imperfection is planted.
DISCONTINUED_RATE = 0.06
PADDED_NAME_RATE = 0.12
BARE_INTEGER_PRICE_RATE = 0.5
REPO_ROOT = Path(__file__).resolve().parents[1]

SUPPLIERS: list[dict[str, str]] = [
    {"supplier_id": "SUP-001", "supplier_name": "Nordwind Elektronik GmbH", "country": "DE"},
    {"supplier_id": "SUP-007", "supplier_name": "Kestrel Peripherals Ltd", "country": "GB"},
    {"supplier_id": "SUP-013", "supplier_name": "Meridian Cable Works", "country": "IN"},
    {"supplier_id": "SUP-021", "supplier_name": "Tanaka Precision KK", "country": "JP"},
    {"supplier_id": "SUP-034", "supplier_name": "Alto Storage Systems", "country": "US"},
    {"supplier_id": "SUP-042", "supplier_name": "ACME Components Ltd", "country": "IN"},
    {"supplier_id": "SUP-055", "supplier_name": "Cerulean Networks BV", "country": "NL"},
    {"supplier_id": "SUP-068", "supplier_name": "Vantage Power Solutions", "country": "SG"},
    {"supplier_id": "SUP-076", "supplier_name": "GlobalParts Trading", "country": "AE"},
    {"supplier_id": "SUP-089", "supplier_name": "Hallstatt Industrie AG", "country": "AT"},
]

# (prefix, category, [(name, base price INR)])
CATALOGUE: list[tuple[str, str, list[tuple[str, int]]]] = [
    (
        "WM",
        "ELECTRONICS",
        [
            ("Wireless Mouse", 799),
            ("Ergonomic Vertical Mouse", 2499),
            ("Bluetooth Travel Mouse", 1299),
            ("Gaming Mouse 16000 DPI", 4999),
            ("Trackball Mouse", 3499),
            ("Silent Click Mouse", 1099),
            ("Presenter Mouse with Laser Pointer", 2799),
            ("Left Handed Mouse", 1899),
            ("Rechargeable Wireless Mouse", 2299),
            ("Compact Travel Mouse", 649),
        ],
    ),
    (
        "KB",
        "PERIPHERALS",
        [
            ("Mechanical Keyboard TKL", 6499),
            ("Wireless Compact Keyboard", 2899),
            ("Backlit Membrane Keyboard", 1499),
            ("Split Ergonomic Keyboard", 11999),
            ("Numeric Keypad", 899),
            ("Low Profile Mechanical Keyboard", 8499),
            ("Full Size Wired Keyboard", 1199),
            ("Keyboard Wrist Rest Gel", 749),
            ("Programmable Macro Pad", 3899),
            ("Bluetooth Multi-Device Keyboard", 4299),
            ("Rugged Spill Resistant Keyboard", 2699),
            ("Keyboard and Mouse Combo", 1999),
        ],
    ),
    (
        "MN",
        "ELECTRONICS",
        [
            ("27 Inch QHD Monitor", 24999),
            ("32 Inch 4K Monitor", 42999),
            ("24 Inch FHD Monitor", 12499),
            ("34 Inch Ultrawide Monitor", 58999),
            ("Portable USB-C Monitor", 18999),
            ("27 Inch 144Hz Gaming Monitor", 31999),
            ("22 Inch Business Monitor", 8999),
            ("49 Inch Super Ultrawide", 94999),
            ("24 Inch Touchscreen Monitor", 27499),
            ("Colour Calibrated 4K Monitor", 66999),
        ],
    ),
    (
        "HS",
        "ACCESSORIES",
        [
            ("Over-Ear Headset", 3999),
            ("USB Conference Headset", 5499),
            ("Noise Cancelling Headphones", 21999),
            ("In-Ear Monitor", 1799),
            ("Wireless Earbuds", 6499),
            ("Mono Call Centre Headset", 2899),
            ("Bone Conduction Headset", 9499),
            ("Gaming Headset 7.1", 7299),
        ],
    ),
    (
        "DK",
        "ACCESSORIES",
        [
            ("USB-C Docking Station", 8999),
            ("Thunderbolt 4 Dock", 24999),
            ("Laptop Stand Aluminium", 2199),
            ("Monitor Arm Dual", 6799),
            ("Under Desk Cable Tray", 1299),
            ("Laptop Riser Adjustable", 3299),
            ("Sit Stand Desk Converter", 15999),
            ("Docking Station 11-in-1", 6299),
            ("Monitor Arm Single Gas Spring", 4199),
            ("Footrest Ergonomic", 2499),
        ],
    ),
    (
        "SD",
        "STORAGE",
        [
            ("1TB NVMe SSD", 7499),
            ("2TB NVMe SSD", 13999),
            ("512GB Portable SSD", 4999),
            ("4TB External HDD", 8999),
            ("64GB USB 3.2 Flash Drive", 699),
            ("8TB NAS Drive", 21499),
            ("256GB microSD Card", 1899),
            ("1TB Portable SSD Rugged", 11499),
            ("16TB Enterprise HDD", 42999),
            ("M.2 NVMe Enclosure", 2299),
            ("4TB NVMe SSD", 27999),
            ("2TB Network Attached Storage", 34999),
        ],
    ),
    (
        "NW",
        "NETWORKING",
        [
            ("Wi-Fi 6 Access Point", 9999),
            ("8 Port Gigabit Switch", 2499),
            ("24 Port Managed Switch", 18999),
            ("USB-C Ethernet Adapter", 1199),
            ("Mesh Router 3 Pack", 16999),
            ("PoE Injector", 1899),
            ("Wi-Fi 6E Router", 21999),
            ("5 Port Unmanaged Switch", 1299),
            ("10GbE Network Card", 12499),
            ("Fibre Media Converter", 3899),
            ("48 Port PoE Switch", 64999),
            ("Powerline Adapter Kit", 4599),
        ],
    ),
    (
        "CB",
        "CABLES",
        [
            ("USB-C to USB-C 2m", 899),
            ("HDMI 2.1 Cable 3m", 1299),
            ("DisplayPort 1.4 Cable 2m", 1099),
            ("Cat6A Patch Cable 5m", 499),
            ("USB-C to Lightning 1m", 1599),
            ("Thunderbolt 4 Cable 0.8m", 3499),
            ("USB-A to USB-C 1m", 449),
            ("Cat6 Patch Cable 0.5m", 199),
            ("HDMI to DisplayPort Adapter", 1799),
            ("Fibre Optic HDMI 10m", 6499),
            ("USB Extension Cable 3m", 599),
            ("Audio Jack 3.5mm 2m", 349),
        ],
    ),
    (
        "PW",
        "POWER",
        [
            ("65W USB-C Charger", 2499),
            ("100W GaN Charger", 4499),
            ("Surge Protector 6 Way", 1699),
            ("20000mAh Power Bank", 3299),
            ("UPS 1000VA", 12999),
            ("Desktop Power Strip USB", 1999),
            ("140W GaN Charger", 6999),
            ("Wireless Charging Pad 15W", 2199),
            ("UPS 3000VA Rack Mount", 48999),
            ("Car Charger Dual USB-C", 1499),
            ("10000mAh Slim Power Bank", 2299),
            ("PDU 8 Outlet Metered", 22999),
        ],
    ),
]

CATEGORY_ERP_LABEL = {
    "ELECTRONICS": "Electronics",
    "PERIPHERALS": "Peripherals",
    "ACCESSORIES": "Accessories",
    "STORAGE": "Storage",
    "NETWORKING": "Networking",
    "CABLES": "Cables",
    "POWER": "Power",
}
CATEGORY_VENDOR_CODE = {
    "ELECTRONICS": "ELEC",
    "PERIPHERALS": "PERIPH",
    "ACCESSORIES": "ACC",
    "STORAGE": "STOR",
    "NETWORKING": "NET",
    "CABLES": "CBL",
    "POWER": "PWR",
}


def build_catalogue(rng: random.Random) -> list[dict[str, Any]]:
    """The shared truth all three feeds are imperfect views of."""
    products: list[dict[str, Any]] = []
    for prefix, category, items in CATALOGUE:
        for index, (name, base_price) in enumerate(items, start=1):
            supplier = rng.choice(SUPPLIERS)["supplier_id"]
            products.append(
                {
                    "sku": f"{prefix}-{index:03d}",
                    "name": name,
                    "category": category,
                    "supplier_id": supplier,
                    "price": Decimal(base_price),
                    "currency": "INR",
                    "stock": rng.choice([0, 0, *range(4, 900)]),
                    "status": "DISCONTINUED" if rng.random() < DISCONTINUED_RATE else "ACTIVE",
                }
            )
    return products


# ---------------------------------------------------------------------------
# ERP feed
# ---------------------------------------------------------------------------

ERP_HEADERS = ["SKU", "Name", "Category", "Price", "Currency", "Stock", "Supplier", "Status"]


def erp_rows(products: list[dict[str, Any]], rng: random.Random) -> list[dict[str, str]]:
    """The system of record: mostly clean, with the padding a fixed-width export leaves."""
    rows = []
    for product in products:
        name = product["name"]
        if rng.random() < PADDED_NAME_RATE:
            # Fixed-width origin leaves runs of spaces. collapse_whitespace must
            # absorb this, or it looks like a product rename every single night.
            name = name.replace(" ", "   ", 1)
        rows.append(
            {
                "SKU": product["sku"],
                "Name": name,
                "Category": CATEGORY_ERP_LABEL[product["category"]],
                "Price": f"{product['price']:.2f}",
                "Currency": product["currency"],
                "Stock": str(product["stock"]),
                "Supplier": product["supplier_id"],
                "Status": product["status"],
            }
        )

    # A handful of planted defects, so even the system of record is not assumed clean.
    rows[3]["Stock"] = "-12"  # negative inventory
    rows[11]["Supplier"] = "SUP-999"  # supplier that does not exist
    rows[19]["Currency"] = "RUPEE"  # not an ISO 4217 code
    rows[27]["Price"] = ""  # required field missing
    rows.append({**rows[5], "Stock": "77"})  # duplicate SKU, differing stock
    return rows


def erp_second_run(rows: list[dict[str, str]], rng: random.Random) -> list[dict[str, str]]:
    """A later extract with genuine attribute changes — the SCD-2 evidence.

    Most rows are byte-identical so the loader must report them as unchanged and
    open no new version. A small, named set changes so history has something real
    to record.
    """
    changed = [dict(row) for row in rows]

    # Price rise on a monitor — the classic "what did it cost in March?" question.
    for row in changed:
        if row["SKU"] == "MN-001":
            row["Price"] = "27499.00"
        elif row["SKU"] == "KB-001":
            row["Name"] = "Mechanical Keyboard TKL Hot-Swap"  # rename
        elif row["SKU"] == "SD-002":
            row["Supplier"] = "SUP-034"  # re-sourced
        elif row["SKU"] == "NW-002":
            row["Status"] = "DISCONTINUED"  # end of life
        elif row["SKU"] == "CB-004":
            row["Price"] = "549.00"  # price rise

    # Stock moves everywhere. Because quantity is a measure, not a tracked
    # attribute, none of this may open a new dimension version.
    for row in changed:
        if row["Stock"].lstrip("-").isdigit():
            current = int(row["Stock"])
            if current > 0:
                row["Stock"] = str(max(0, current + rng.randint(-40, 40)))
    return changed


# ---------------------------------------------------------------------------
# ACME supplier feed
# ---------------------------------------------------------------------------

ACME_HEADERS = [
    "product_code",
    "product_name",
    "category",
    "unit_price",
    "currency",
    "qty",
    "supplier_code",
]


def acme_rows(products: list[dict[str, Any]], rng: random.Random) -> list[dict[str, str]]:
    """A supplier drop: a partial catalogue in a different dialect, with real problems."""
    subset = rng.sample(products, k=int(len(products) * 0.62))
    subset.sort(key=lambda product: product["sku"])

    rows = []
    for product in subset:
        rows.append(
            {
                # No punctuation, lower case: 'WM-001' becomes 'wm001'.
                "product_code": product["sku"].replace("-", "").lower(),
                "product_name": product["name"].lower(),
                "category": product["category"].lower(),
                # Sometimes a bare integer, sometimes two decimal places.
                "unit_price": (
                    f"{product['price']}"
                    if rng.random() < BARE_INTEGER_PRICE_RATE
                    else f"{product['price']:.2f}"
                ),
                "currency": product["currency"],
                "qty": str(product["stock"]),
                # 'SUP-042' becomes 'SUP42'.
                "supplier_code": product["supplier_id"].replace("-", "").replace("SUP0", "SUP"),
            }
        )

    # ACME's characteristic defects, planted at a realistic rate.
    negative_stock_at = rng.sample(range(len(rows)), k=max(6, len(rows) // 9))
    for index in negative_stock_at:
        rows[index]["qty"] = str(-rng.randint(1, 60))

    for index in rng.sample([i for i in range(len(rows)) if i not in negative_stock_at], k=5):
        rows[index]["supplier_code"] = rng.choice(["SUP777", "SUP888", "SUP404"])

    rows[2]["currency"] = "RS"  # not ISO 4217
    rows[8]["unit_price"] = "n/a"  # non-numeric price
    rows[14]["product_name"] = ""  # required field blank
    rows[21]["category"] = "widgets"  # category the lookup does not know
    rows[30]["unit_price"] = "0"  # zero price with stock: business rule
    rows[30]["qty"] = "45"
    rows.append(dict(rows[4]))  # exact duplicate row
    rows.append({**rows[9], "qty": "13"})  # same SKU, different quantity

    # Thousands separators and a stray currency symbol, as exported from a spreadsheet.
    rows[6]["unit_price"] = f"{int(products[6]['price']):,}"
    rows[17]["unit_price"] = f"₹{products[17]['price']}"
    return rows


# ---------------------------------------------------------------------------
# GlobalParts vendor API feed
# ---------------------------------------------------------------------------


def vendor_records(products: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    """JSON records for the vendor API simulator to serve."""
    subset = rng.sample(products, k=int(len(products) * 0.45))
    subset.sort(key=lambda product: product["sku"])

    records = []
    for product in subset:
        prefix, number = product["sku"].split("-")
        records.append(
            {
                # A third spelling of the same identifier: 'wm_001'.
                "itemId": f"{prefix.lower()}_{number}",
                "description": product["name"],
                "dept": CATEGORY_VENDOR_CODE[product["category"]],
                "price": float(product["price"]),
                "currencyCode": product["currency"],
                "availableUnits": product["stock"],
                "vendor": product["supplier_id"].replace("-", ""),
                "lifecycle": "EOL" if product["status"] == "DISCONTINUED" else "LIVE",
            }
        )

    for index in rng.sample(range(len(records)), k=4):
        records[index]["availableUnits"] = -rng.randint(1, 30)
    for index in rng.sample(range(len(records)), k=3):
        records[index]["vendor"] = "SUP901"
    records[5]["dept"] = "MISC"  # department the lookup does not know
    records[12]["price"] = None  # null price
    records.append(dict(records[7]))  # duplicate item
    return records


# ---------------------------------------------------------------------------
# Malformed files, for pipeline-level failure tests
# ---------------------------------------------------------------------------


def write_malformed(directory: Path) -> None:
    """Structurally broken files. These fail the *run*, not individual records."""
    directory.mkdir(parents=True, exist_ok=True)

    (directory / "missing_headers.csv").write_text(
        "product_code,product_name,price\nwm001,Wireless Mouse,799\n",
        encoding="utf-8",
    )
    (directory / "ragged_rows.csv").write_text(
        "product_code,product_name,category,unit_price,currency,qty,supplier_code\n"
        "wm001,Wireless Mouse,electronics,799,INR,120,SUP42\n"
        "kb002,Mechanical Keyboard,peripherals,6499,INR\n"
        "mn003,Monitor,electronics,24999,INR,45,SUP07,EXTRA,FIELDS\n",
        encoding="utf-8",
    )
    (directory / "empty.csv").write_text("", encoding="utf-8")
    (directory / "headers_only.csv").write_text(
        "product_code,product_name,category,unit_price,currency,qty,supplier_code\n",
        encoding="utf-8",
    )
    # Latin-1 bytes in a file the contract declares as UTF-8.
    (directory / "bad_encoding.csv").write_bytes(
        b"product_code,product_name,category,unit_price,currency,qty,supplier_code\n"
        + b"wm001,Wireless Mo\xfcse,electronics,799,INR,120,SUP42\n"
    )


# ---------------------------------------------------------------------------


def write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "data")
    arguments = parser.parse_args()
    out: Path = arguments.out

    rng = random.Random(SEED)  # noqa: S311 - synthetic fixtures, not cryptography
    products = build_catalogue(rng)

    erp_first = erp_rows(products, rng)
    erp_later = erp_second_run(erp_first, rng)
    acme = acme_rows(products, rng)
    vendor = vendor_records(products, rng)

    # Two directories with distinct roles:
    #
    #   samples/erp/           the landing zone — holds *the current extract*.
    #                          A real ERP drops a new file here each night and it
    #                          supersedes the last; the pipeline reads whatever
    #                          is present.
    #   samples/erp_extracts/  the archive of every extract, so the demo can stage
    #                          them in sequence and show SCD-2 doing its job.
    archive = out / "samples" / "erp_extracts"
    write_csv(archive / "erp_master_2026-03-01.csv", ERP_HEADERS, erp_first)
    write_csv(archive / "erp_master_2026-03-14.csv", ERP_HEADERS, erp_later)

    # Stage the first extract so `supplylink run --source erp_core` works
    # immediately after generating data.
    landing = out / "samples" / "erp"
    landing.mkdir(parents=True, exist_ok=True)
    for stale in landing.glob("*.csv"):
        stale.unlink()
    write_csv(landing / "erp_master_2026-03-01.csv", ERP_HEADERS, erp_first)

    write_csv(out / "samples" / "acme" / "acme_catalogue_2026-03-01.csv", ACME_HEADERS, acme)

    vendor_path = out / "samples" / "globalparts" / "catalog.json"
    vendor_path.parent.mkdir(parents=True, exist_ok=True)
    vendor_path.write_text(json.dumps(vendor, indent=2) + "\n", encoding="utf-8")

    reference = out / "reference" / "suppliers.csv"
    write_csv(reference, ["supplier_id", "supplier_name", "country"], SUPPLIERS)

    write_malformed(out / "malformed")

    print(f"catalogue           {len(products):>5} products across {len(CATALOGUE)} categories")
    print(f"suppliers           {len(SUPPLIERS):>5}")
    print(f"erp  extract 1      {len(erp_first):>5} rows  (staged in samples/erp/)")
    print(f"erp  extract 2      {len(erp_later):>5} rows  (samples/erp_extracts/)")
    print(f"acme                {len(acme):>5} rows")
    print(f"globalparts (api)   {len(vendor):>5} records")
    print(f"written to {out}")


if __name__ == "__main__":
    main()
