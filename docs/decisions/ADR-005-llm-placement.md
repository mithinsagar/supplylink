# ADR-005 — The LLM interprets metrics and never computes them

**Status:** accepted · **Date:** 2026-02

## Context

A pipeline run produces a lot of true, useful, unreadable output: counters,
failure categories, ten reconciliation verdicts, run-over-run deltas. Someone
still has to look at it and decide whether anything needs doing.

That is a language problem, and a reasonable place for a language model. It is
also a place where a language model can do real damage: a confident sentence
containing a wrong number is worse than no sentence at all, and a pipeline that
cannot load data because a vendor API is down is an unacceptable failure mode.

## Decision

The model sits at the very end of the pipeline, after everything is computed and
committed, and does exactly one job: turn metrics into prose.

Four constraints make that safe.

**Every figure is pre-computed.** Counts, rates, shares and deltas are all
calculated by the pipeline and included in the payload. The model never needs to
divide two numbers, so it is never asked to. This also makes the guard below
sound: any figure not in the payload was invented.

**Generated output is verified.** Every numeric literal in the response is
matched against the metrics the pipeline computed. An unverifiable figure
rejects the whole report in favour of the deterministic one, with the rejected
draft kept for inspection. Instructing a model not to invent numbers is a
request; this is a check.

The guard is narrower than it first appears it should be, deliberately. Numbers
are harvested only from numeric values and from strings that are *entirely* a
number, a timestamp, or a version. Pulling digits out of arbitrary strings looks
harmless and quietly guts the guard — a run id is a UUID and a checksum is 64 hex
characters, so between them almost any short number a model could invent would
match something. Symmetrically, tokens in the *text* that mix letters and digits
(`WM001`, an abbreviated checksum) are identifiers, not claims, and are stripped
before checking.

**Failure degrades, never propagates.** No key, provider down, malformed
response, guard rejection — every path produces a deterministic report rendered
from the same metrics. It is never less *accurate* than a generated report, only
less readable. `LLMUnavailableError` deliberately does not inherit from
`SupplyLinkError`, so an LLM failure cannot be mistaken for a pipeline failure.

**Nothing sensitive is sent.** The payload carries counts, rates, rule names and
field names — never product records, prices, supplier names or quarantined
payloads. A test asserts that no product name from the fixtures appears in what
was sent. Temperature is pinned to 0, and the exact payload, model, prompt
version and guard result are stored with every report.

## Alternatives considered

**No LLM.** Entirely defensible; the deterministic report is genuinely good. The
generated version adds the interpretation a table cannot — "unknown supplier
references usually mean reference data is lagging the transactional feed" — and
because it is strictly optional, adding it costs nothing when it is unavailable.

**LLM computes the metrics from raw data.** The obvious thing to try and the
wrong thing to build. It would be slower, more expensive, unable to explain its
arithmetic, and wrong occasionally in ways nobody could predict.

**LLM decides pass or fail.** Now the model is in the control path, and a run's
outcome depends on a non-deterministic remote service. Thresholds are declared in
contracts, evaluated in code, and reviewed in Git.

**Structured output / function calling.** Would constrain the shape of the
response but not the truth of the numbers in it. The guard is the constraint that
actually matters, and it works regardless of response format.

**Annotate suspect figures instead of rejecting.** Softer, and it ships a report
a reader must audit. For a data-quality tool, an unverifiable number is exactly
the thing that must not ship.

## Consequences

**Good.** A run note anyone can read, with a mechanical guarantee against
misreported metrics. The fallback path is the default in CI and in any
environment without a key, so it is continuously exercised rather than being
emergency code that has never run. Provider, model, prompt version, temperature,
token counts and the full input payload are stored for audit.

**Costs.** The guard can reject a legitimate report if a model rounds unusually
or phrases a figure in an unanticipated way — a false rejection is safe, but it
does mean generation is not guaranteed. The prompt must be versioned, since
wording changes output. A provider costs money per run, which the master switch
and `--no-llm` exist to control.
