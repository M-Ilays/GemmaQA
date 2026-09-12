"""Structured, importance-aware context reduction.

Replaces naive tail truncation for JSON-shaped prompts. The audit
(docs/MODEL_CONTEXT_AUDIT.md, J-1) found that `clamp_prompt` cuts the END of the
serialized string, and that the end of the action payload is where the page
observation and the anti-prompt-injection reminder live. On any large page the
model therefore lost the controls it was required to choose from, lost the
security reminder, and received JSON that stopped mid-structure.

The fix is structural rather than textual: **reduce the object graph, then
serialize once.** A payload built this way is well-formed by construction — there
is no code path that can emit a half-written object, because the serializer only
ever sees a complete Python object.

Reduction order is by declared importance, never by position in the document.
Sections are shrunk (lists trimmed, long strings summarized) before they are
dropped, and protected sections are never dropped at all. Everything that was
reduced or omitted is recorded in a `ContextReductionReport` so the run can
report honestly on what the model did and did not see, rather than silently
presenting a truncated view as complete.

No tokenizer ships with this project, so `estimate_tokens` uses a documented
conservative approximation (see CHARS_PER_TOKEN). It is used for reporting and
for budget headroom, never as the authoritative limit — the authoritative limit
remains `Settings.max_prompt_chars`, which every existing caller already honours.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

# Conservative characters-per-token approximation. Real tokenizers average
# roughly 4 chars/token on prose and rather less on punctuation-dense JSON, so
# 3.2 deliberately OVER-estimates token count: an over-estimate spends budget we
# did not need, while an under-estimate silently overruns the model's context
# window. Only replace this with a real tokenizer, never with a larger constant.
CHARS_PER_TOKEN = 3.2


def estimate_tokens(text: str | int) -> int:
    """Conservative token estimate for reporting and headroom decisions."""
    length = text if isinstance(text, int) else len(text or "")
    return int(length / CHARS_PER_TOKEN) + 1


# ---------------------------------------------------------------------------
# Section priorities
# ---------------------------------------------------------------------------
#
# Lower number == more important == reduced last. The two tiers below are the
# reduction policy in one place; `PRESERVE_TIER_MAX` is the boundary between
# "shrink only as a last resort" and "shrink freely".

PRIORITY_SECURITY_REMINDER = 1
PRIORITY_OPERATOR_OBJECTIVE = 2
PRIORITY_TASK_CONTEXT = 3
PRIORITY_CONSTRAINTS = 4
PRIORITY_CURRENT_STATE = 5
PRIORITY_ACTIONABLE_CONTROLS = 6
PRIORITY_EVIDENCE_REGISTRY = 7
PRIORITY_FAILURES_AND_CONTRADICTIONS = 8

PRIORITY_ACTION_HISTORY = 9
PRIORITY_KNOWN_PAGES = 10
PRIORITY_LOW_CONFIDENCE = 11
PRIORITY_ENGINE_DIAGNOSTICS = 12
PRIORITY_PAGE_TEXT = 13
PRIORITY_GRAPH_CONTEXT = 14
PRIORITY_EVIDENCE_SUMMARIES = 15

PRESERVE_TIER_MAX = PRIORITY_FAILURES_AND_CONTRADICTIONS

# A list is never trimmed below this while its section is still present — a
# section reduced to nothing is indistinguishable from a section that was empty
# all along, which is exactly the ambiguity this module exists to prevent.
MIN_LIST_ITEMS = 1
# Long free text is summarized to at least this many characters before the whole
# section becomes a drop candidate.
MIN_TEXT_CHARS = 120
REDUCED_TEXT_MARKER = " …[reduced]"


@dataclass
class PromptSection:
    """One named, independently-reducible piece of a prompt payload."""

    key: str
    value: Any
    priority: int
    # Protected sections survive every reduction pass. Only the security
    # reminder and the response-schema contract qualify: dropping either changes
    # what the model is permitted to do, which is never a size trade-off.
    protected: bool = False

    @property
    def preserve_tier(self) -> bool:
        return self.priority <= PRESERVE_TIER_MAX


@dataclass
class ContextReductionReport:
    """What the model actually received, and what it did not."""

    included_sections: list[str] = field(default_factory=list)
    reduced_sections: list[str] = field(default_factory=list)
    omitted_sections: list[str] = field(default_factory=list)
    estimated_tokens: int = 0
    final_chars: int = 0
    budget_chars: int = 0
    # True when reduction ran at all. A run that never exceeded budget reports
    # False and empty reduced/omitted lists.
    reduction_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "included_sections": list(self.included_sections),
            "reduced_sections": list(self.reduced_sections),
            "omitted_sections": list(self.omitted_sections),
            "estimated_tokens": self.estimated_tokens,
            "final_chars": self.final_chars,
            "budget_chars": self.budget_chars,
            "reduction_applied": self.reduction_applied,
        }


Serializer = Callable[[dict[str, Any]], str]


def _default_serializer(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str, indent=2)


def _shrink(value: Any) -> tuple[Any, bool]:
    """Return `(smaller_value, changed)` for one reduction step.

    Halving rather than removing keeps the *shape* of the evidence visible to the
    model — five of forty recent actions still communicates "there is history
    here", where an absent key communicates "there is none".
    """
    if isinstance(value, list):
        if len(value) > MIN_LIST_ITEMS:
            return value[: max(MIN_LIST_ITEMS, len(value) // 2)], True
        return value, False

    if isinstance(value, str):
        stripped = value[: -len(REDUCED_TEXT_MARKER)] if value.endswith(REDUCED_TEXT_MARKER) else value
        if len(stripped) > MIN_TEXT_CHARS:
            keep = max(MIN_TEXT_CHARS, len(stripped) // 2)
            return stripped[:keep] + REDUCED_TEXT_MARKER, True
        return value, False

    if isinstance(value, dict):
        # Shrink the single largest child, so one oversized sub-object cannot
        # hide behind a dozen small siblings.
        if not value:
            return value, False
        largest_key = max(value, key=lambda k: len(_default_serializer({str(k): value[k]})))
        shrunk, changed = _shrink(value[largest_key])
        if not changed:
            return value, False
        copy = dict(value)
        copy[largest_key] = shrunk
        return copy, True

    return value, False


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, (list, dict, str)) and len(value) == 0)


def reduce_sections(
    sections: list[PromptSection],
    *,
    budget_chars: int,
    overhead_chars: int = 0,
    serializer: Serializer | None = None,
) -> tuple[dict[str, Any], ContextReductionReport]:
    """Reduce `sections` until the serialized payload fits the budget.

    `overhead_chars` accounts for prompt text wrapped around the JSON payload
    (schema hint, headings, trailing instruction) so the caller's final string —
    not just the payload — respects the budget.

    Guarantees:
      * the returned dict is always serializable to well-formed JSON;
      * protected sections are always present;
      * lower-priority sections are always reduced before higher-priority ones;
      * the report names every section that was reduced or omitted.
    """
    dumps = serializer or _default_serializer
    effective_budget = max(0, budget_chars - overhead_chars)

    payload: dict[str, Any] = {s.key: s.value for s in sections}
    report = ContextReductionReport(
        included_sections=[s.key for s in sections],
        budget_chars=budget_chars,
    )

    def _finish(final_payload: dict[str, Any]) -> tuple[dict[str, Any], ContextReductionReport]:
        text = dumps(final_payload)
        report.final_chars = len(text)
        report.estimated_tokens = estimate_tokens(len(text) + overhead_chars)
        return final_payload, report

    if budget_chars <= 0 or len(dumps(payload)) <= effective_budget:
        return _finish(payload)

    report.reduction_applied = True

    # Reduce lowest-priority first. Within a priority, later-declared sections
    # go first, so a caller's declaration order is a meaningful tie-break.
    order = sorted(range(len(sections)), key=lambda i: (-sections[i].priority, -i))
    reduced: set[str] = set()
    omitted: set[str] = set()

    # Pass 1 — shrink and, where allowed, drop the reducible tier.
    for index in order:
        section = sections[index]
        if section.protected:
            continue
        while len(dumps(payload)) > effective_budget:
            current = payload.get(section.key)
            if _is_empty(current):
                break
            shrunk, changed = _shrink(current)
            if not changed:
                # Cannot get smaller. Droppable only outside the preserve tier.
                if section.preserve_tier:
                    break
                payload.pop(section.key, None)
                omitted.add(section.key)
                reduced.discard(section.key)
                break
            payload[section.key] = shrunk
            reduced.add(section.key)
        if len(dumps(payload)) <= effective_budget:
            break

    # Pass 2 — last resort. Still over budget, so shrink the preserve tier too
    # (never dropping it, and never touching protected sections).
    if len(dumps(payload)) > effective_budget:
        guard = 0
        while len(dumps(payload)) > effective_budget and guard < 500:
            guard += 1
            progressed = False
            for index in order:
                section = sections[index]
                if section.protected or section.key in omitted:
                    continue
                current = payload.get(section.key)
                if _is_empty(current):
                    continue
                shrunk, changed = _shrink(current)
                if changed:
                    payload[section.key] = shrunk
                    reduced.add(section.key)
                    progressed = True
                    if len(dumps(payload)) <= effective_budget:
                        break
            if not progressed:
                break

    # Pass 3 — the floor. Every section has now been shrunk as far as shrinking
    # goes (each list down to one item, each string to its minimum), and the
    # payload still does not fit. Rather than silently emit an over-budget
    # prompt, drop whole sections in strict reverse-priority order, so the LAST
    # things standing are the protected sections plus the highest-priority
    # context. Found by a live probe at a deliberately tiny budget: without
    # this the reducer stops at its shrink floor and quietly overruns.
    if len(dumps(payload)) > effective_budget:
        for index in order:
            if len(dumps(payload)) <= effective_budget:
                break
            section = sections[index]
            if section.protected or section.key not in payload:
                continue
            payload.pop(section.key, None)
            omitted.add(section.key)
            reduced.discard(section.key)

    report.reduced_sections = sorted(reduced - omitted)
    report.omitted_sections = sorted(omitted)
    report.included_sections = [s.key for s in sections if s.key in payload]
    return _finish(payload)
