"""Detects when observed behaviour disagrees with what this run previously
recorded.

Applications under active development change underneath the tester. A route
that worked this morning 404s this afternoon; a field that was optional
becomes required; a screen an actor could reach yesterday now refuses them.
GemmaQA must notice these as FINDINGS rather than silently overwriting its
earlier belief -- overwriting would erase exactly the signal a QA engineer
most wants.

Prior knowledge is therefore treated as a HYPOTHESIS to be re-verified, not
a fact to be trusted. Every contradiction records both the previous and the
current observation so a human can adjudicate.

The contradiction objects deliberately mirror the shape of
`GraphContradiction` (deterministic id, description, evidence, confidence,
status) rather than inventing a parallel mechanism.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.adaptive_understanding.schemas import UnderstandingContradiction


def _contradiction(
    contradiction_type: str,
    *,
    key: str,
    description: str,
    previous: str,
    current: str,
    evidence: list[str],
    confidence: float,
) -> UnderstandingContradiction:
    return UnderstandingContradiction(
        # Deterministic id: the same disagreement observed twice is the same
        # contradiction, never a duplicate record.
        contradiction_id=f"understanding-contradiction:{contradiction_type}:{key}",
        contradiction_type=contradiction_type,
        description=description,
        previous_observation=previous,
        current_observation=current,
        supporting_evidence=evidence,
        confidence=confidence,
    )


def detect_state_contradiction(
    *,
    fingerprint: str,
    url: str,
    previous_state: str,
    current_state: str,
    previous_confidence: float,
    current_confidence: float,
) -> UnderstandingContradiction | None:
    """The same screen (identical fingerprint) classified differently than
    before. Only raised when BOTH classifications were reasonably confident
    -- a low-confidence guess changing its mind is learning, not a
    contradiction, and reporting it as one would be noise."""
    if not fingerprint or previous_state == current_state:
        return None
    if previous_confidence < 0.5 or current_confidence < 0.5:
        return None
    return _contradiction(
        "state_contradiction",
        key=fingerprint,
        description=(
            f"A screen with an identical structural fingerprint was previously understood as "
            f"'{previous_state}' but is now understood as '{current_state}'."
        ),
        previous=f"{previous_state} (confidence {previous_confidence:.2f})",
        current=f"{current_state} (confidence {current_confidence:.2f})",
        evidence=[f"url={url}", f"fingerprint={fingerprint}"],
        confidence=min(previous_confidence, current_confidence),
    )


def detect_navigation_contradiction(
    *,
    url: str,
    previously_reachable: bool,
    currently_reachable: bool,
    failure_detail: str = "",
) -> UnderstandingContradiction | None:
    """A destination that was reachable earlier in this run no longer is."""
    if not url or not previously_reachable or currently_reachable:
        return None
    return _contradiction(
        "navigation_contradiction",
        key=url,
        description=f"A destination that was reachable earlier in this run can no longer be reached: {url}",
        previous="reachable",
        current=f"unreachable{(' -- ' + failure_detail) if failure_detail else ''}",
        evidence=[f"url={url}"] + ([failure_detail] if failure_detail else []),
        confidence=0.7,
    )


def detect_permission_contradiction(
    *,
    actor_term: str,
    surface_key: str,
    previously_permitted: bool,
    currently_permitted: bool,
    evidence_detail: str = "",
) -> UnderstandingContradiction | None:
    """The same actor's access to the same surface changed within one run."""
    if previously_permitted == currently_permitted:
        return None
    return _contradiction(
        "permission_contradiction",
        key=f"{actor_term or 'current session'}:{surface_key}",
        description=(
            f"Access for '{actor_term or 'the current session'}' to '{surface_key}' changed during this run: "
            f"{'granted then refused' if previously_permitted else 'refused then granted'}."
        ),
        previous="permitted" if previously_permitted else "denied",
        current="permitted" if currently_permitted else "denied",
        evidence=[f"actor={actor_term or 'current session'}", f"surface={surface_key}"]
        + ([evidence_detail] if evidence_detail else []),
        confidence=0.75,
    )


def detect_validation_contradiction(
    *,
    form_key: str,
    previously_validated: bool,
    currently_validated: bool,
    evidence_detail: str = "",
) -> UnderstandingContradiction | None:
    """Validation that previously fired for a form no longer does, or vice
    versa -- a common and important signal in software under development."""
    if previously_validated == currently_validated:
        return None
    return _contradiction(
        "validation_contradiction",
        key=form_key,
        description=(
            f"Validation behaviour changed for '{form_key}' during this run: "
            f"{'validation was observed and then absent' if previously_validated else 'validation was absent and then observed'}."
        ),
        previous="validation observed" if previously_validated else "no validation observed",
        current="validation observed" if currently_validated else "no validation observed",
        evidence=[f"form={form_key}"] + ([evidence_detail] if evidence_detail else []),
        confidence=0.65,
    )


def detect_behaviour_contradiction(
    *,
    action_signature: str,
    previous_outcome_state: str,
    current_outcome_state: str,
    evidence_detail: str = "",
) -> UnderstandingContradiction | None:
    """The same action, taken from the same starting point, produced a
    materially different destination state than it did before."""
    if not action_signature or previous_outcome_state == current_outcome_state:
        return None
    return _contradiction(
        "behaviour_contradiction",
        key=action_signature,
        description=(
            f"The same action previously led to '{previous_outcome_state}' but now leads to "
            f"'{current_outcome_state}'."
        ),
        previous=previous_outcome_state,
        current=current_outcome_state,
        evidence=[f"action={action_signature}"] + ([evidence_detail] if evidence_detail else []),
        confidence=0.7,
    )
