"""Compares two consecutive observations of the same screen.

This is how GemmaQA detects "the application is still changing" without
injecting a MutationObserver, without polling a framework's internal
lifecycle, and without knowing what a framework is: take two samples of what
a tester would see, and check whether they agree.

Two consecutive observations that agree are evidence of quiescence. Two that
disagree are evidence of ongoing work -- and the DIRECTION matters: content
that grew between samples is a strong indication that rendering is still
in progress, whereas content that merely churned may be a live-updating
widget that will never settle. Both are handled below, and the second case
is deliberately not treated as permanently "unready" -- see
`readiness_estimator`, which bounds how long it will keep waiting.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.adaptive_understanding.readiness_signals import _content_count, _interactive_count
from app.intelligence.adaptive_understanding.schemas import ReadinessSignal, StabilityAssessment

_SIGNAL_WEIGHT_CHANGING = 0.7
_SIGNAL_WEIGHT_GROWING = 0.6
_SIGNAL_WEIGHT_SETTLED = 0.6


def _collection_row_total(model: Any) -> int:
    total = 0
    for coll in getattr(model, "collections", None) or []:
        total += int(getattr(coll, "row_count", 0) or 0)
    return total


def compare(previous: Any, current: Any) -> StabilityAssessment:
    """Structural diff of two `CanonicalPageModel` observations.

    Returns `compared=False` when there is no previous observation to
    compare against -- absence of a comparison is never reported as
    stability, because a single sample cannot demonstrate quiescence.
    """
    if previous is None or current is None:
        return StabilityAssessment(
            stable=False,
            compared=False,
            explanation="Only one observation available; quiescence cannot be demonstrated from a single sample.",
        )

    prev_fp = str(getattr(previous, "state_fingerprint", "") or "")
    curr_fp = str(getattr(current, "state_fingerprint", "") or "")
    fingerprint_changed = bool(prev_fp and curr_fp and prev_fp != curr_fp)

    interactive_delta = _interactive_count(current) - _interactive_count(previous)
    content_delta = _content_count(current) - _content_count(previous)
    row_delta = _collection_row_total(current) - _collection_row_total(previous)

    changed = fingerprint_changed or interactive_delta != 0 or content_delta != 0 or row_delta != 0

    if not changed:
        explanation = "Two consecutive observations are structurally identical; the screen appears to have settled."
    else:
        parts = []
        if fingerprint_changed:
            parts.append("state fingerprint changed")
        if interactive_delta:
            parts.append(f"interactive controls {interactive_delta:+d}")
        if content_delta:
            parts.append(f"content blocks {content_delta:+d}")
        if row_delta:
            parts.append(f"collection rows {row_delta:+d}")
        explanation = "The screen changed between consecutive observations: " + ", ".join(parts) + "."

    return StabilityAssessment(
        stable=not changed,
        compared=True,
        fingerprint_changed=fingerprint_changed,
        interactive_delta=interactive_delta,
        content_delta=content_delta,
        collection_row_delta=row_delta,
        explanation=explanation,
    )


def stability_signals(stability: StabilityAssessment) -> list[ReadinessSignal]:
    """Convert a comparison into readiness signals so the estimator can
    weigh cross-observation evidence alongside single-observation evidence
    in one uniform model."""
    if not stability.compared:
        return []

    if stability.stable:
        return [
            ReadinessSignal(
                signal_id="readiness-signal:dom_settled",
                kind="dom_settled",
                supports_ready=True,
                weight=_SIGNAL_WEIGHT_SETTLED,
                observed_value="no structural change",
                description=stability.explanation,
            )
        ]

    signals = [
        ReadinessSignal(
            signal_id="readiness-signal:dom_still_changing",
            kind="dom_still_changing",
            supports_ready=False,
            weight=_SIGNAL_WEIGHT_CHANGING,
            observed_value=stability.explanation,
            description="The application is still mutating the screen, so any plan made now may target a control that is about to move or disappear.",
        )
    ]
    # Growth is a stronger and more specific signal than mere churn: it says
    # content is still ARRIVING, not merely cycling.
    if stability.content_delta > 0 or stability.interactive_delta > 0 or stability.collection_row_delta > 0:
        signals.append(
            ReadinessSignal(
                signal_id="readiness-signal:content_growing",
                kind="content_growing",
                supports_ready=False,
                weight=_SIGNAL_WEIGHT_GROWING,
                observed_value=(
                    f"interactive {stability.interactive_delta:+d}, "
                    f"content {stability.content_delta:+d}, rows {stability.collection_row_delta:+d}"
                ),
                description="Content is still arriving; the screen is more complete than it was a moment ago.",
            )
        )
    return signals
