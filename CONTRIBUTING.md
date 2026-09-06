# Contributing

## Getting set up

```bash
make install                  # virtualenv and dependencies
make db-up                    # PostgreSQL via Compose, or use your own
make test-db                  # creates supplylink_test and its btree_gist extension
make migrate seed sample-data
make check                    # lint, typecheck, tests — what CI runs
```

Integration tests need a real PostgreSQL 16. They are skipped with a clear
message if one is not reachable, but a change to the schema, the SCD loader or
reconciliation is not meaningfully tested without it.

## Adding a data source

No Python is needed. Write a contract, and:

```bash
supplylink contracts validate
supplylink run --source <your-source-id>
```

See [`docs/data-contracts.md`](docs/data-contracts.md).

## Adding a transform

Transforms are pure, null-safe functions registered by name.

```python
@register("your_transform")
@_null_safe
def your_transform(value: Any) -> str:
    """One line saying what it normalises, and why that matters."""
    ...
```

Rules: no I/O, no clock, no global state; `None` in, `None` out; raise
`MappingError` on input it cannot honestly convert. Add a unit test — the
registry test already asserts every transform is null-safe.

## Changing the schema

1. Write the migration in `migrations/versions/` as readable SQL via
   `op.execute`, with a `downgrade` that actually works.
2. Mirror the change in `src/supplylink/warehouse/schema.py`.
3. Run `pytest tests/integration/test_schema.py` — the drift test will tell you
   if the two disagree.
4. If you add a constraint that carries design intent, add a test that attempts
   to violate it. A constraint nothing tries to break is a constraint that can be
   silently dropped.

## Conventions

**Comments explain why, not what.** `# increment counter` above `counter += 1`
is noise. A comment earning its place says what would go wrong without the line.

**Errors are actionable.** Name the file, the field, the value, and what to do.
`"invalid contract"` helps nobody; the messages in
`docs/data-contracts.md#what-contract-validation-catches` are the standard.

**No supplier-specific code.** If something cannot be expressed in a contract,
the fix is a new transform or a new contract capability, not a special case.

**Record-level versus run-level failures.** A bad record is quarantined and the
run continues; a bad run stops and loads nothing. See
[`docs/pipeline-lifecycle.md`](docs/pipeline-lifecycle.md#failure-policy). Getting
this wrong is the most consequential mistake available in this codebase.

**Tests state the property, not the mechanics.** `test_an_unchanged_record_opens_no_version`
over `test_scd_2`.

## Making a decision

Anything that would be hard to reverse, or that a future reader would question,
gets an ADR in `docs/decisions/`: context, decision, alternatives considered,
consequences. The alternatives section is the valuable half — it records what was
rejected and why, which is the part nobody can reconstruct later.

## Before opening a pull request

```bash
make format check
```

CI additionally runs the demo end to end and builds the Docker stack, so a change
that breaks either will be caught there.
