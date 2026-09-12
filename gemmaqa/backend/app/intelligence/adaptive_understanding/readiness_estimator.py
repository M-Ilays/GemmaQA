"""Turns readiness signals into a readiness score and an observation
decision.

The model is deliberately the same shape used everywhere else in GemmaQA's
reasoning layer (see `goal_priority.py`, `strategy_scoring.py`): a weighted
combination of independently-observed signals, never a single opaque
verdict, and always explainable by listing which signals fired.

The decision is EVIDENCE-driven, not clock-driven. GemmaQA does not decide
"wait 3 seconds and hope"; it decides "the evidence is too weak to plan
from, so look again". The only role time plays is the physically
unavoidable one: two samples cannot be taken at the same instant, and the
number of samples is bounded so a permanently-animating screen cannot hang
the run. Both bounds are stated explicitly rather than hidden.
"""

from __future__ import annotations

from app.intelligence.adaptive_understanding.schemas import (
    POSITIVE_READINESS_SIGNALS,
    ObservationConfidence,
    ReadinessAssessment,
    ReadinessSignal,
)

# Readiness at or above this is treated as "safe to reason from".
READY_THRESHOLD = 0.6
# Below this, the observation is too weak to plan from at all.
CRITICALLY_UNREADY_THRESHOLD = 0.35

# A screen that keeps changing forever (a clock, a live feed, an animation)
# must not stall the run. After this many passes the engine accepts what it
# has and says so honestly via `accept_degraded`.
MAX_OBSERVATION_PASSES = 6

# Spacing between re-samples, when -- and only when -- the evidence was
# judged too weak to plan from. This is NOT a readiness timeout: readiness is
# decided by evidence, and a screen that is already rendered exits on pass 1
# having waited zero milliseconds.
#
# The interval GROWS geometrically because application startup cost varies by
# orders of magnitude between deployments: a local development server may
# render in 50 ms while a cold, contended, or throttled environment takes
# tens of seconds. A flat interval must either be too short for the slow case
# (giving up just before content arrives -- observed live during development)
# or wastefully long for the fast case. Doubling accommodates both from a
# single policy, without encoding any assumption about a particular
# application or technology.
REOBSERVATION_BASE_INTERVAL_MS = 400
REOBSERVATION_MAX_INTERVAL_MS = 4000


def reobservation_interval_ms(
    observation_pass: int,
    *,
    base_ms: int = REOBSERVATION_BASE_INTERVAL_MS,
    max_ms: int = REOBSERVATION_MAX_INTERVAL_MS,
) -> int:
    """How long to wait before sampling again, given how many passes have
    already been spent. Geometric growth, capped.

    With the defaults, the cumulative observation window across
    `MAX_OBSERVATION_PASSES` is roughly 400 + 800 + 1600 + 3200 + 4000 ms
    (~10 s) -- long enough for a genuinely slow deployment, and paid only by
    applications whose own evidence says they are not ready yet.
    """
    exponent = max(0, observation_pass - 1)
    # Bounded shift: never let a large pass count overflow into a huge number.
    interval = base_ms * (2 ** min(exponent, 8))
    return int(min(interval, max_ms))


# Signals that mean "there is nothing here". While any of these hold, a
# quiescent screen is NOT a ready screen -- it is a screen that is
# persistently empty, which is evidence of a problem rather than of
# completion. See `_suppress_settled_emptiness`.
_EMPTINESS_SIGNALS = frozenset({"empty_document", "no_interactive_elements", "no_content"})


def _suppress_settled_emptiness(signals: list[ReadinessSignal]) -> list[ReadinessSignal]:
    """Stop `dom_settled` from being read as readiness on an empty screen.

    Two identical observations of a blank page demonstrate that the page is
    *consistently* blank -- which argues the opposite of readiness. Without
    this, an application that never renders would score progressively
    *higher* the longer it failed to render, which is precisely backwards.
    """
    if not any(s.kind in _EMPTINESS_SIGNALS for s in signals):
        return signals

    adjusted: list[ReadinessSignal] = []
    for signal in signals:
        if signal.kind == "dom_settled":
            adjusted.append(
                signal.model_copy(
                    update={
                        "supports_ready": False,
                        "description": (
                            "The screen has stopped changing, but it is still empty -- consistent emptiness "
                            "is evidence of a problem or of content that never arrived, not of readiness."
                        ),
                    }
                )
            )
        else:
            adjusted.append(signal)
    return adjusted


def estimate_readiness(signals: list[ReadinessSignal]) -> ReadinessAssessment:
    """Combine signals into a score in [0,1].

    With no signals at all the score is deliberately 0.5 -- genuine
    uncertainty, not confident readiness. An observation that produced no
    signals whatsoever is unusual and should not be mistaken for a clean
    bill of health.
    """
    if not signals:
        return ReadinessAssessment(
            assessment_id="readiness:no-signals",
            readiness_score=0.5,
            signals=[],
            explanation="No readiness signals could be extracted; readiness is genuinely unknown rather than confirmed.",
        )

    signals = _suppress_settled_emptiness(signals)

    positive_weight = sum(s.weight for s in signals if s.supports_ready)
    negative_weight = sum(s.weight for s in signals if not s.supports_ready)
    total = positive_weight + negative_weight
    score = (positive_weight / total) if total > 0 else 0.5

    positive_kinds = sorted({s.kind for s in signals if s.supports_ready})
    blocking_kinds = sorted({s.kind for s in signals if not s.supports_ready})

    if score >= READY_THRESHOLD:
        explanation = (
            f"Evidence favours readiness ({score:.2f}): {', '.join(positive_kinds) or 'no positive signals'}"
            + (f"; outstanding concerns: {', '.join(blocking_kinds)}" if blocking_kinds else "")
        )
    else:
        explanation = (
            f"Evidence does not yet support readiness ({score:.2f}); blocking signals: "
            + (", ".join(blocking_kinds) or "none identified")
        )

    return ReadinessAssessment(
        assessment_id="readiness:" + ("-".join(positive_kinds + blocking_kinds) or "empty"),
        readiness_score=score,
        signals=list(signals),
        positive_signal_kinds=positive_kinds,
        blocking_signal_kinds=blocking_kinds,
        explanation=explanation,
    )


def estimate_observation_confidence(
    readiness: ReadinessAssessment,
    *,
    state_confidence: float,
    compared: bool,
) -> ObservationConfidence:
    """How much this observation can be trusted as a basis for planning.

    Readiness dominates: a confidently-classified state observed on a
    half-rendered screen is still a poor basis for planning, because the
    controls a plan would target may not exist yet.
    """
    value = (readiness.readiness_score * 0.7) + (state_confidence * 0.3)

    supporting = [f"readiness={readiness.readiness_score:.2f}"]
    if readiness.positive_signal_kinds:
        supporting.append("positive signals: " + ", ".join(readiness.positive_signal_kinds))
    if state_confidence > 0:
        supporting.append(f"state classification confidence={state_confidence:.2f}")

    missing: list[str] = []
    if not compared:
        missing.append("no second observation, so quiescence is undemonstrated")
        # A single sample can never fully establish trust, however good it looks.
        value = min(value, 0.85)
    if not readiness.positive_signal_kinds:
        missing.append("no positive readiness signal was observed")
    if state_confidence <= 0.0:
        missing.append("the application state could not be identified")

    contradicting = list(readiness.blocking_signal_kinds)

    if value >= READY_THRESHOLD and not contradicting:
        basis = "direct_observation"
    elif value >= CRITICALLY_UNREADY_THRESHOLD:
        basis = "heuristic"
    else:
        basis = "insufficient_evidence"

    if value < READY_THRESHOLD:
        recommendation = "Observe again before planning; current evidence is too weak to target a control safely."
    elif not compared:
        recommendation = "A confirming second observation would raise confidence, but planning from this one is reasonable."
    else:
        recommendation = ""

    return ObservationConfidence(
        value=value,
        basis=basis,
        supporting_evidence=supporting,
        missing_evidence=missing,
        contradicting_evidence=contradicting,
        recommended_next_observation=recommendation,
    )


def decide(
    readiness: ReadinessAssessment,
    confidence: ObservationConfidence,
    *,
    observation_pass: int,
    max_passes: int = MAX_OBSERVATION_PASSES,
) -> tuple[str, str]:
    """Returns `(decision, reason)`.

    `accept`           -- evidence is sufficient; reason from this observation.
    `reobserve`        -- evidence is too weak; sample again.
    `accept_degraded`  -- budget spent; proceed, but the low confidence is
                          recorded rather than silently ignored.
    """
    if confidence.value >= READY_THRESHOLD:
        return "accept", (
            f"Observation confidence {confidence.value:.2f} meets the threshold "
            f"({READY_THRESHOLD}); {readiness.explanation}"
        )

    if observation_pass >= max_passes:
        return "accept_degraded", (
            f"Observation confidence {confidence.value:.2f} remains below {READY_THRESHOLD} after "
            f"{observation_pass} passes. Accepting the best available observation and recording the "
            f"low confidence rather than stalling the run. Blocking signals: "
            f"{', '.join(readiness.blocking_signal_kinds) or 'none identified'}."
        )

    return "reobserve", (
        f"Observation confidence {confidence.value:.2f} is below {READY_THRESHOLD} on pass "
        f"{observation_pass}; blocking signals: {', '.join(readiness.blocking_signal_kinds) or 'none identified'}. "
        f"Sampling again rather than planning from weak evidence."
    )
