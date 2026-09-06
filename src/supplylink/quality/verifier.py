"""The numeric guard.

Instructing a model not to invent numbers is a request, not a guarantee. This
module makes it checkable: every numeric literal in the generated text is matched
against the set of figures the pipeline actually computed and put in the payload.
Anything unaccounted for means the report is not trustworthy, and an untrustworthy
number in a confident sentence is worse than no report at all — so the report is
discarded in favour of the deterministic fallback, with the rejected draft kept
for inspection.

This is the mechanism behind the claim in ADR-005 that generated narrative cannot
misreport metrics. The prompt asks; this checks.

Two details make it work in practice rather than in theory:

**Every figure is pre-computed.** Rates, shares and deltas are all in the payload
(see :mod:`supplylink.quality.metrics`), so the model never has a legitimate
reason to derive one. Without that, sound derivations would trip the guard and
the guard would have to be loosened until it meant nothing.

**Structural numbers are stripped before matching.** Markdown list markers and
heading levels are formatting, not claims. Removing them first avoids the
alternative — an allow-list of "small numbers that are probably fine" — which
would punch a hole exactly where a wrong small count could hide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

#: Numeric literals: optional sign, digits with optional thousands separators,
#: optional fractional part. Trailing % and surrounding text are handled by the
#: surrounding context, not captured here.
_NUMBER = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?")

#: Markdown ordered-list markers at the start of a line ("1. ", "2) ").
_LIST_MARKER = re.compile(r"^[ \t]*\d+[.)]\s+", re.MULTILINE)

#: Fenced code blocks. Their contents are quoted data, not prose claims.
_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)

#: Inline code spans, which the reports use for identifiers — run ids, SKUs,
#: rule names, abbreviated checksums.
_CODE_SPAN = re.compile(r"`[^`]*`")

#: A token mixing letters and digits: ``WM001``, ``abc123``, ``fb53d5171e67``,
#: ``grok-4-fast``. These are identifiers, not claims, and their digits must not
#: be checked as figures — nor, symmetrically, admitted as metrics. A genuine
#: figure never contains a letter.
_MIXED_TOKEN = re.compile(r"\b(?=[\w-]*[A-Za-z])[\w-]*\d[\w-]*\b")

#: Tolerance for a figure the model rounded. 12.34 may legitimately appear as
#: 12.3 or 12. Anything further from a real metric is treated as invented.
_ROUNDING_PLACES = (0, 1, 2, 3, 4)

#: A string that is *entirely* a number, optionally signed and optionally a
#: percentage. These are metrics rendered as text ("5.59%", "0.0000%").
_NUMERIC_STRING = re.compile(r"^\s*[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*%?\s*$")

#: An ISO-8601 date or timestamp. Its components are legitimate figures for the
#: model to quote when it dates a run.
_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}\S*)?$")

#: A dotted version such as "1.3.0". A contract version is a real fact from the
#: payload and the model should be free to quote it. Unlike a UUID or a hex
#: digest it is short and structured, so admitting it does not meaningfully widen
#: what a hallucination could match.
_VERSION_STRING = re.compile(r"^\d+(?:\.\d+){1,3}$")


@dataclass(frozen=True, slots=True)
class GuardResult:
    """The outcome of verifying one generated report."""

    verified: bool
    numbers_checked: int
    unverified: tuple[str, ...] = ()
    allowed_sample: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "numbers_checked": self.numbers_checked,
            "unverified": list(self.unverified),
            "policy": (
                "every numeric literal in the generated text must match a metric "
                "the pipeline computed; unverifiable figures reject the report"
            ),
        }


def collect_allowed_numbers(payload: Any) -> set[Decimal]:
    """Every *metric* appearing anywhere in the metrics payload.

    Walks the structure recursively, so a figure nested inside a check's details
    counts as available.

    Strings are treated far more narrowly than they first appear they should be.
    Only a string that is entirely a number ("5.59%") or an ISO timestamp
    contributes. Pulling digits out of arbitrary strings looks harmless and
    quietly guts the guard: a run id is a UUID and a checksum is 64 hex
    characters, so between them almost any two- or three-digit number the model
    could invent would find a match somewhere and be waved through. Identifiers
    are not metrics.
    """
    allowed: set[Decimal] = set()
    _walk(payload, allowed)

    # Admit rounded forms of every allowed figure, so a model that writes 12.3
    # for a computed 12.34 is not treated as having invented it.
    for value in list(allowed):
        for places in _ROUNDING_PLACES:
            allowed.add(_round(value, places))
    return allowed


def _walk(node: Any, allowed: set[Decimal]) -> None:
    if isinstance(node, bool):
        return
    if isinstance(node, int | float | Decimal):
        allowed.add(_to_decimal(node))
        return
    if isinstance(node, str):
        if _NUMERIC_STRING.match(node) or _ISO_TIMESTAMP.match(node) or _VERSION_STRING.match(node):
            for match in _NUMBER.finditer(node):
                parsed = _parse(match.group())
                if parsed is not None:
                    allowed.add(parsed)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            # Keys can be dates when a payload is bucketed by day; other keys are
            # field names and contribute nothing under the rule above.
            _walk(key, allowed)
            _walk(value, allowed)
        return
    if isinstance(node, list | tuple | set):
        for item in node:
            _walk(item, allowed)


def verify(text: str, payload: Any) -> GuardResult:
    """Check every number in ``text`` against the metrics in ``payload``."""
    allowed = collect_allowed_numbers(payload)
    stripped = _strip_formatting(text)

    checked = 0
    unverified: list[str] = []

    for match in _NUMBER.finditer(stripped):
        literal = match.group()
        value = _parse(literal)
        if value is None:
            continue
        checked += 1
        if value in allowed:
            continue
        if any(_round(value, places) in allowed for places in _ROUNDING_PLACES):
            continue
        unverified.append(literal)

    return GuardResult(
        verified=not unverified,
        numbers_checked=checked,
        unverified=tuple(dict.fromkeys(unverified)),
    )


def _strip_formatting(text: str) -> str:
    """Remove digits that are structure or identifiers rather than claims.

    Three passes, in order: fenced blocks and inline code (quoted data), ordered
    list markers (formatting), then mixed letter/digit tokens (identifiers). What
    remains is prose, and every number in prose is a claim about the data.
    """
    without_code = _CODE_SPAN.sub(" ", _CODE_FENCE.sub(" ", text))
    without_markers = _LIST_MARKER.sub("", without_code)
    return _MIXED_TOKEN.sub(" ", without_markers)


def _parse(literal: str) -> Decimal | None:
    try:
        return Decimal(literal.replace(",", ""))
    except InvalidOperation:
        return None


def _to_decimal(value: int | float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _round(value: Decimal, places: int) -> Decimal:
    quantum = Decimal(1).scaleb(-places)
    try:
        return value.quantize(quantum)
    except InvalidOperation:
        return value
