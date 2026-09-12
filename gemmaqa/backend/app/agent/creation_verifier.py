"""Creation/update/deletion verification — the explicit answer to "do not
treat a successful click as successful creation."

A `BrowserAction` succeeding only means the browser didn't error executing
it (`ActionResult.success`); it says nothing about whether a record was
actually created, updated, or removed. This module checks for INDEPENDENT,
observable evidence — a success toast, a URL redirect, the expected value
appearing in a record detail/list row/search result, or a stable identifier
— before anything is allowed to call a creation "verified".

Reuses this session's existing perception surfaces rather than inventing a
second detection mechanism: `PageState.toasts`/`.alerts` (legacy observer),
and, where available, the richer `CanonicalPageModel.collections` (Turn 2's
`RecordCollection`) for a real list-row/search-result membership check.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

_SUCCESS_HINTS = re.compile(r"success|created|saved|added|updated|complete", re.IGNORECASE)
_FAILURE_HINTS = re.compile(r"error|failed|invalid|required|denied|unable", re.IGNORECASE)

VERIFICATION_SIGNAL_KINDS = frozenset(
    {
        "success_toast",
        "redirect",
        "record_detail",
        "list_row",
        "search_result",
        "api_response",
        "stable_identifier",
    }
)


class CreationVerificationResult(BaseModel):
    verified: bool = False
    signals: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    stable_identifier: str | None = None
    contradicted: bool = False
    contradiction_reason: str | None = None


def _toast_signal(after_toasts: list[str], expected_value: str | None) -> tuple[list[str], list[str], bool]:
    signals: list[str] = []
    evidence: list[str] = []
    contradicted = False
    for t in after_toasts or []:
        if expected_value and expected_value in t:
            signals.append("success_toast")
            evidence.append(f"toast contains expected value: {t!r}")
        elif _SUCCESS_HINTS.search(t) and not _FAILURE_HINTS.search(t):
            signals.append("success_toast")
            evidence.append(f"success-shaped toast observed: {t!r}")
        elif _FAILURE_HINTS.search(t):
            contradicted = True
            evidence.append(f"failure-shaped toast observed: {t!r}")
    return signals, evidence, contradicted


def _collection_membership(collections: list[Any], expected_value: str) -> tuple[list[str], list[str]]:
    signals: list[str] = []
    evidence: list[str] = []
    for collection in collections or []:
        for row in getattr(collection, "visible_rows", None) or []:
            cells = getattr(row, "cell_values", None) or []
            if any(expected_value in (cell or "") for cell in cells):
                signals.append("list_row")
                evidence.append(f"expected value found in collection {getattr(collection, 'element_id', '?')} row")
                break
    return signals, evidence


def verify_creation(
    *,
    before_url: str,
    after_url: str,
    after_visible_text: str,
    after_toasts: list[str] | None = None,
    expected_value: str | None = None,
    after_collections: list[Any] | None = None,
    is_search_context: bool = False,
) -> CreationVerificationResult:
    """`is_search_context=True` reclassifies a `list_row` hit as
    `search_result` instead — the SAME underlying signal (the value is
    visible in a record collection), but the caller knows whether this
    followed a create action (list membership) or a search/filter action
    (search result), and the report should say which."""
    signals: list[str] = []
    evidence: list[str] = []

    if before_url != after_url:
        signals.append("redirect")
        evidence.append(f"URL changed: {before_url} -> {after_url}")

    toast_signals, toast_evidence, contradicted = _toast_signal(after_toasts or [], expected_value)
    signals.extend(toast_signals)
    evidence.extend(toast_evidence)

    if expected_value and expected_value in (after_visible_text or ""):
        signals.append("record_detail")
        evidence.append("expected value found in page text")

    collection_signals, collection_evidence = _collection_membership(after_collections or [], expected_value or "")
    if is_search_context:
        collection_signals = ["search_result" if s == "list_row" else s for s in collection_signals]
    signals.extend(collection_signals)
    evidence.extend(collection_evidence)

    stable_identifier = None
    if expected_value:
        match = re.search(re.escape(expected_value) + r"[/#]?(\d+)?", after_url)
        if match and match.group(1):
            stable_identifier = match.group(1)

    return CreationVerificationResult(
        verified=bool(signals) and not contradicted,
        signals=sorted(set(signals)),
        evidence=evidence,
        stable_identifier=stable_identifier,
        contradicted=contradicted,
        contradiction_reason=("failure-shaped toast observed after the action" if contradicted else None),
    )


def verify_update(before_value: str | None, after_value: str | None, *, expected_value: str) -> bool:
    """Before/after comparison — the update is verified only if the field's
    value actually CHANGED to the expected one, never merely "the submit
    click succeeded"."""
    if before_value == after_value:
        return False
    return after_value == expected_value


def verify_deletion(
    *,
    before_row_count: int | None,
    after_row_count: int | None,
    identity: str,
    after_visible_text: str = "",
) -> CreationVerificationResult:
    """Absence-based: either the collection's row count dropped, or the
    identity string is no longer present in the observed page text."""
    signals: list[str] = []
    evidence: list[str] = []
    if before_row_count is not None and after_row_count is not None and after_row_count < before_row_count:
        signals.append("list_row")
        evidence.append(f"row count decreased: {before_row_count} -> {after_row_count}")
    if identity and identity not in (after_visible_text or ""):
        signals.append("record_detail")
        evidence.append("identity no longer present in page text")
    return CreationVerificationResult(verified=bool(signals), signals=sorted(set(signals)), evidence=evidence)
