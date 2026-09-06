# Security

## Reporting

This is a portfolio project and is not deployed anywhere. If you find something
worth flagging, open an issue.

## Practices in the codebase

**No secrets in the repository.** `.env` is gitignored; `.env.example` documents
every variable with a working non-secret default. Contracts reference credentials
by environment-variable name — a contract names a token, never contains one.

```yaml
auth:
  type: bearer
  token_env: VENDOR_API_TOKEN     # the value comes from the environment
```

A missing credential fails with the variable named, and the value never appears
in a log line, an error message or a metric. A test asserts that a token is
absent from an error raised while using it.

**SQL parameters are always bound.** Identifiers cannot be bound, so the two
places where one is interpolated are constrained twice: referential-integrity
targets come from an allow-list in `validation/references.py`, and checksum
columns from the closed canonical field set. Both are re-checked immediately
before the statement is built, and both raise rather than interpolate anything
unrecognised.

**Contract expressions are sandboxed.** Business rules are parsed to an AST and
structurally validated against an allow-list — attribute access, calls to unknown
names, subscripts, comprehensions, imports, lambdas and the walrus operator are
all rejected at contract load. `eval` is never used. Contracts are plausibly
edited by people outside the core team, so this is a real boundary and not a
theoretical one.

**YAML is parsed with `safe_load`.** The full loader can instantiate arbitrary
Python objects from a document.

**API errors are sanitised.** A SQLAlchemy exception stringifies to the failing
statement, its bound parameters and often the connection URL. The handler logs
that in full and returns `503 {"error": "DatabaseError", "detail": "the warehouse
is unavailable"}`. A test plants a password in a simulated failure and asserts it
never reaches the client.

**Input is validated at every boundary.** Contracts through Pydantic with
`extra="forbid"`; API requests and path parameters through typed models with
patterns; source records through the contract's own rules.

**Minimal data leaves the process.** The quality report sends counts, rates, rule
names and field names — never product records, prices, supplier names or
quarantined payloads. A test asserts no product name from the fixtures appears in
what was sent.

**Stored text is bounded.** Error messages, failure values and quarantined
payloads are all capped, so a pathological input cannot exhaust storage through
the quarantine path.

**The container runs as a non-root user** with no login shell, built from a
multi-stage Dockerfile so no compilers or build headers ship in the runtime
image. CI asserts the running user is not root.

**Dependencies are pinned to compatible ranges** with upper bounds, and the
dependency list is deliberately short — eleven runtime packages, none of them
transitively enormous.

## Known limitations

This is a demonstration project, not a hardened deployment.

* **The API has no authentication.** It is designed to run inside a trusted
  network behind a gateway that terminates auth. Exposing it directly would need
  authentication, authorisation and rate limiting added first.
* **No row-level security or multi-tenancy.** Anyone with database access sees
  everything.
* **Default credentials in `.env.example`** are for local development only and
  are documented as such.
* **The vendor simulator has no authentication by default.** It is a test fixture
  and must not be deployed.
