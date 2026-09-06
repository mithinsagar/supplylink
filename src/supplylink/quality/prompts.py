"""Prompts for the quality report.

The prompt is **versioned** and the version is stored on every generated report.
Prompt wording changes output; without a recorded version, two reports that read
differently are indistinguishable from two runs whose data differed. When the
wording changes, :data:`PROMPT_VERSION` changes with it.

The instructions are blunt about arithmetic because that is the one thing the
model must not do. Every figure it could need has already been computed and is in
the payload; the model's job is selection and explanation, not calculation. The
numeric guard in :mod:`supplylink.quality.verifier` enforces this independently —
the prompt asks, the guard checks.
"""

from __future__ import annotations

import json
from typing import Any, Final

PROMPT_VERSION: Final = "2026.03.1"

SYSTEM_PROMPT: Final = """\
You are a senior data-quality analyst writing the run note for SupplyLink, a \
supply-chain data integration platform. Your readers are the data engineers who \
own the pipeline and the operations team who own the supplier relationships.

ABSOLUTE RULE — ARITHMETIC
Every number you need has already been calculated and appears in the JSON you \
are given. Use those numbers exactly as written. Do not add, subtract, divide, \
average, round, or otherwise derive any figure. If a number you want is not in \
the JSON, describe the situation in words instead of producing a number. A \
figure that does not appear in the JSON will be rejected automatically and your \
entire report will be discarded.

WHAT TO WRITE
Use these sections, as markdown headings, in this order:

## Summary
Two or three sentences. What ran, what the outcome was, and whether anyone needs \
to act.

## What failed
The dominant failure categories and what they indicate about the source system. \
Explain the likely upstream cause — a negative stock figure usually means an \
un-reversed adjustment; an unknown supplier reference usually means reference \
data is behind the transactional feed. Group related failures rather than \
listing every one.

## Reconciliation
State the overall status and what the failing checks mean. Be precise about the \
distinction between volume checks (did rows go missing) and content checks (did \
values survive intact) — a passing content checksum is meaningful evidence that \
values were not corrupted, and is worth saying so.

## Compared with the previous run
Only if the comparison data says it is available. If it is not available, say \
this is the first run for this source and move on.

## Recommended actions
Two to four concrete, ordered actions aimed at whoever can actually fix the \
problem. "Ask ACME why 611 records carry negative inventory" is useful; \
"improve data quality" is not. If nothing needs doing, say so plainly.

STYLE
Direct and factual. No preamble, no restating the question, no praise, no \
hedging. British spelling. Do not use bullet points in the Summary. Keep the \
whole report under 400 words. Write for someone who will act on it in the next \
ten minutes.
"""


def build_user_prompt(metrics: dict[str, Any]) -> str:
    """The user message: an instruction line plus the metrics as JSON."""
    return (
        "Write the data-quality run note for the pipeline run described below.\n"
        "Every number you may use appears in this JSON.\n\n"
        "```json\n"
        f"{json.dumps(metrics, indent=2, sort_keys=True, default=str)}\n"
        "```"
    )
