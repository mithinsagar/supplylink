# Data contracts

A contract is the complete, declarative description of one source feed.
Onboarding a supplier is writing one of these files. There is no
supplier-specific Python anywhere in the codebase — see [ADR-002](decisions/ADR-002-declarative-yaml-contracts.md).

```bash
supplylink contracts list          # every discovered contract
supplylink contracts validate      # parse and cross-check them all
supplylink contracts transforms    # what a mapping may reference
supplylink contracts sync          # register versions in the warehouse
```

## Shape

```yaml
contract:        # version, effective date, ownership
source:          # identity, precedence, and how to read it
identity:        # which source column carries the record's own id
mapping:         # source dialect  ->  canonical model
lookups:         # value translation tables
validation:      # per-field rules
rules:           # cross-field business rules
reconciliation:  # checksum columns and thresholds
scd:             # which attributes are worth versioning
```

## `contract` — identity and immutability

```yaml
contract:
  version: "1.3.0"           # semver; a published version is immutable
  effective_from: 2026-01-01
  owner: master-data-team
  description: >
    Nightly product master extract from the ERP.
```

The content hash is computed over the **parsed structure**, so reformatting and
comments are free but a semantic change is not. Re-registering a version with
different content is refused:

```text
ContractImmutabilityError: contract erp_core version 1.2.0 is already registered
with a different definition (registered 07e2a3ca181e…, on disk 9f1c2e40a8b1…).
Bump contract.version in contracts/erp.yaml instead of editing a published version.
```

That matters because every run records which contract version produced it, and
that reference means nothing if the definition behind it can change.

## `source` — access and authority

```yaml
source:
  id: erp_core
  display_name: ERP Product Master
  type: csv
  precedence: 10             # lower is more authoritative (ADR-007)
  csv:
    path_glob: samples/erp/*.csv
    encoding: utf-8
    delimiter: ","
    required_headers: [SKU, Name, Category, Price, Currency, Stock, Supplier, Status]
```

`required_headers` is the difference between a run failure and silent corruption:
a missing header means the export shape changed, and loading it would null out a
whole canonical column while every row still "succeeded".

For a REST source:

```yaml
  type: rest
  rest:
    base_url: "${VENDOR_API_URL:-http://localhost:8081}"
    path: /v1/catalog
    records_path: items            # dotted path to the record array
    auth:
      type: bearer
      token_env: VENDOR_API_TOKEN  # named, never inlined
    pagination:
      style: page_number
      page_size: 40
      total_pages_path: meta.totalPages
      max_pages: 50                # a cursor that never advances must not loop forever
    timeout_seconds: 10.0
    max_attempts: 4
    backoff_initial_seconds: 0.4
    retry_on_status: [408, 425, 429, 500, 502, 503, 504]
```

`${VAR}` and `${VAR:-default}` are resolved from the environment, so a contract
names a credential without containing one. A reference with no default and no
matching variable is a contract error rather than an empty string — an empty base
URL fails much later and much more confusingly.

**The contract model refuses to let a 4xx onto `retry_on_status`.** Retrying a
401 four times does not fix the credential; it turns an instant clear error into
a slow confusing one.

## `mapping` — the translation

Every field follows the same declared pipeline:

```text
source value  →  transforms (in order)  →  lookup  →  default  →  type coercion
```

```yaml
mapping:
  canonical_sku:
    from: SKU
    transforms: [trim, sku_normalise]      # 'WM-001' → 'WM001'

  product_name:
    from: Name
    transforms: [nfkc, trim, collapse_whitespace]

  category:
    from: Category
    transforms: [nullify_placeholders, trim, upper]
    lookup: category_map                   # 'ELEC' → 'ELECTRONICS'

  currency:
    from: Currency
    transforms: [nullify_placeholders, trim, currency_normalise]
    default: INR                           # applied after the lookup

  status:
    constant: ACTIVE                       # for feeds with no such column
```

**Coercion is the type check.** There is no separate `type` rule that runs later:
coercing to the canonical type either succeeds — and the value is now guaranteed
correct for hashing and for the database — or it fails and produces a `type`
failure. One mechanism, so the two can never disagree about what a valid decimal
is.

**The default applies after the lookup, not before.** An unrecognised category
resolves to null and is then filled by a default if one exists; without one it
stays null and the `required` rule catches it. That ordering is what stops an
unknown code entering the warehouse as itself. Set `lookup_passthrough: true` to
opt out.

### Transforms

Pure, null-safe, and registered in code — `supplylink contracts transforms`
lists them. A genuinely new normalisation is one registered function plus a unit
test, available to every contract from then on.

| | |
| --- | --- |
| **Hygiene** | `trim` `collapse_whitespace` `upper` `lower` `title_case` `nfkc` `nullify_placeholders` |
| **Domain** | `sku_normalise` `supplier_id_normalise` `currency_normalise` |
| **Coercion** | `to_decimal` `to_integer` `to_boolean` `to_date` |
| **Numeric** | `abs` |

Two worth noting. `collapse_whitespace` exists because fixed-width ERP exports
pad names with runs of spaces, and without it `"Wireless  Mouse"` looks like a
rename every single night. `nfkc` exists because a full-width digit or a
non-breaking space hashes differently from its ASCII equivalent, and
reconciliation would report a content mismatch that is not real.

`abs` is registered but used by no shipped contract, deliberately: turning a
negative stock figure positive manufactures data. The negative is a real signal
and belongs in quarantine.

## `validation` — per-field rules

```yaml
validation:
  canonical_sku:
    required: true
    type: string          # cross-checked against the canonical model
    min_length: 3
    max_length: 32
    regex: "^[A-Z0-9]{3,32}$"
    unique: true          # scoped to the batch

  unit_price:
    required: true
    type: decimal
    min: 0
    max: 10000000

  currency:
    required: true
    allowed_values: [INR, USD, EUR, GBP]

  supplier_id:
    required: true
    references:                     # resolved in one query per column
      table: core.suppliers
      column: supplier_id
```

**Null handling follows SQL.** If a value is null, only `required` has an
opinion — a null price is not "below the minimum", it is absent, and saying so
twice would produce two failures for one problem and make the top-failure
ranking lie about the feed.

**All applicable rules run.** A value both too long and badly formatted reports
both in one pass, rather than over two runs.

**`severity: warning`** records the finding without quarantining the record.

**Referential targets are allow-listed** in `validation/references.py`, not just
pattern-matched — the table name reaches SQL, so "it matches a regex" is not a
strong enough guarantee.

## `rules` — cross-field logic

```yaml
rules:
  - name: stocked_items_must_be_priced
    expr: "quantity_available == 0 or unit_price > 0"
    message: "An item with stock on hand must carry a non-zero price"
    severity: error

  - name: discontinued_items_should_be_empty
    expr: "status == 'ACTIVE' or quantity_available == 0"
    message: "A discontinued item still shows stock on hand"
    severity: warning
```

Expressions run in a **restricted evaluator**, never `eval`. They are parsed to
an AST once at contract load and structurally validated against an allow-list;
attribute access, calls to unknown names, subscripts, comprehensions, imports,
lambdas and the walrus operator are all rejected *before a single record is
touched*. Contracts are files plausibly edited by someone outside the core team,
and a rule that could execute arbitrary code would make every contract a
remote-code-execution vector.

Available: comparison, boolean and arithmetic operators, `in`, conditional
expressions, and `abs len min max round lower upper startswith is_null not_null`.

Comparison with null is false rather than an error, so a rule need not restate
nullability. A failure records the values the rule actually saw —
`quantity_available=282, unit_price=None` — because "stocked items must be
priced" alone tells an investigator nothing.

## `reconciliation`

```yaml
reconciliation:
  checksum_columns:                # order is part of the digest
    - canonical_sku
    - product_name
    - category
    - supplier_id
    - unit_price
    - currency

  null_rate_columns: [product_name, category, supplier_id, unit_price]

  thresholds:                      # unknown keys are a contract error
    row_count_delta_pct: { warn: 0,   fail: 0.5 }
    quarantine_rate_pct: { warn: 1.0, fail: 8.0 }
    duplicate_rate_pct:  { warn: 0,   fail: 3.0 }
    null_rate_delta_pct: { warn: 1.0, fail: 5.0 }
    orphan_rate_pct:     { warn: 0,   fail: 3.0 }
```

Thresholds are per source on purpose. The ERP is the system of record and is
held tightly; a supplier drop is expected to be imperfect, and holding it to the
same standard means a permanently red dashboard — which is the fastest way to
make people stop reading it.

Reordering `checksum_columns` changes every digest, so it requires a version bump.

## `scd`

```yaml
scd:
  tracked_attributes:
    - product_name
    - category
    - supplier_id
    - unit_price
    - currency
    - status
```

`quantity_available` is absent and **cannot** be added: the contract model
refuses to track a measure. It changes on every extract, so versioning it would
open a new dimension row nightly for every SKU and bury the changes that matter.
It is written to `core.inventory_snapshots` as a fact instead.

## What contract validation catches

All of these fail at load time, before any data is read:

| Mistake | Message |
| --- | --- |
| `requred: true` | `validation.canonical_sku.requred: Extra inputs are not permitted` |
| Maps a non-canonical field | `mapping targets fields that are not in the canonical model: ['prodcut_name']` |
| Unknown transform | `mapping.canonical_sku uses unregistered transforms ['sku_normalize']. Available: …` |
| Declared type disagrees | `validation.unit_price.type is 'integer' but the canonical model declares 'decimal'` |
| Tracks a measure | `scd.tracked_attributes contains measures ['quantity_available']` |
| Tracks an unmapped field | `scd.tracked_attributes names fields this contract never maps` |
| Unknown threshold key | `reconciliation.thresholds has unknown keys: ['orphaned_rate_pct']` |
| `warn` above `fail` | `warn (9.0) must not exceed fail (5.0)` |
| Lookup defined but unused | `lookups ['orphan_map'] are defined but never referenced` |
| Disallowed FK target | `not a permitted referential-integrity target. Permitted: …` |
| Retries a 401 | `refusing to retry permanently-failing statuses [401]` |
| Unsafe rule expression | `rule expression uses unsupported construct 'Attribute'` |
| Two contracts share a source id | `source id 'erp_core' is declared by two contracts: … and …` |

## Adding a source

1. Write `contracts/suppliers/<name>.yaml`.
2. `supplylink contracts validate`
3. Drop the data, or point the contract at the API.
4. `supplylink run --source <id>`

No code changes.
