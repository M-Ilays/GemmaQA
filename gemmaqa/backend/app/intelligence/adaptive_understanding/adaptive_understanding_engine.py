"""AdaptiveApplicationUnderstandingEngine -- the orchestrator.

Answers, for one observation: *is this application ready to reason about,
what am I looking at, how much do I trust that, and should I look again?*

This is the only class other GemmaQA code talks to. It is deliberately
SYNCHRONOUS and side-effect-free with respect to the browser: it never
navigates, never clicks, never waits. It examines observations the caller
already made and returns a judgement. The caller (the controller) owns the
decision to actually re-observe -- see `assess()`'s `decision` field.

That split matters. It keeps every I/O concern in the controller/adapter
layer where it already lives, and keeps this engine a pure, fully testable
function of its inputs.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.adaptive_understanding.observation_delta import compare, stability_signals
from app.intelligence.adaptive_understanding.readiness_estimator import (
    MAX_OBSERVATION_PASSES,
    decide,
    estimate_observation_confidence,
    estimate_readiness,
)
from app.intelligence.adaptive_understanding.readiness_signals import extract_signals
from app.intelligence.adaptive_understanding.schemas import (
    ObservedStateTransition,
    UnderstandingAssessment,
)
from app.intelligence.adaptive_understanding.state_classifier import classify
from app.intelligence.adaptive_understanding.understanding_contradictions import (
    detect_behaviour_contradiction,
    detect_navigation_contradiction,
    detect_state_contradiction,
)
from app.intelligence.adaptive_understanding.understanding_memory import UnderstandingMemory
from app.intelligence.adaptive_understanding.understanding_query_engine import UnderstandingQueryEngine
from app.utils.exploration_trace import record as trace_record
from app.utils.logging import get_logger

logger = get_logger("intelligence.adaptive_understanding")


class AdaptiveApplicationUnderstandingEngine:
    def __init__(self, memory: UnderstandingMemory | None = None) -> None:
        self.memory = memory or UnderstandingMemory()
        self.query_engine = UnderstandingQueryEngine(self.memory)

    # -- the core judgement --------------------------------------------------

    def assess(
        self,
        model: Any,
        *,
        page_state: Any = None,
        previous_model: Any = None,
        observation_pass: int = 1,
        max_passes: int = MAX_OBSERVATION_PASSES,
        record: bool = True,
    ) -> UnderstandingAssessment:
        """Judge ONE observation.

        `previous_model` is the immediately-preceding observation of the SAME
        screen (not the previous page) -- supplying it is what allows
        quiescence to be demonstrated rather than assumed.
        """
        signals = extract_signals(model, page_state)
        stability = compare(previous_model, model)
        signals.extend(stability_signals(stability))

        readiness = estimate_readiness(signals)
        state = classify(model, readiness_score=readiness.readiness_score, page_state=page_state)
        confidence = estimate_observation_confidence(
            readiness, state_confidence=state.confidence, compared=stability.compared
        )
        decision, reason = decide(
            readiness, confidence, observation_pass=observation_pass, max_passes=max_passes
        )

        unknowns: list[str] = []
        if state.primary_state == "unknown":
            unknowns.append(state.unknown_reason or "application state could not be identified")
        for kind in readiness.blocking_signal_kinds:
            unknowns.append(f"unresolved readiness concern: {kind}")

        assessment = UnderstandingAssessment(
            assessment_id=f"understanding:{getattr(model, 'state_fingerprint', '') or 'nofp'}:{observation_pass}",
            url=str(getattr(model, "url", "") or ""),
            fingerprint=str(getattr(model, "state_fingerprint", "") or ""),
            observation_pass=observation_pass,
            readiness=readiness,
            stability=stability,
            state=state,
            observation_confidence=confidence,
            decision=decision,
            decision_reason=reason,
            unknowns=unknowns,
        )

        if record:
            self._record(assessment)

        return assessment

    def _record(self, assessment: UnderstandingAssessment) -> None:
        self.memory.begin_pass()

        # Re-verify prior understanding of this exact screen before replacing it.
        previous = self.memory.previous_for_fingerprint(assessment.fingerprint)
        if previous is not None:
            contradiction = detect_state_contradiction(
                fingerprint=assessment.fingerprint,
                url=assessment.url,
                previous_state=previous.state.primary_state,
                current_state=assessment.state.primary_state,
                previous_confidence=previous.state.confidence,
                current_confidence=assessment.state.confidence,
            )
            if contradiction is not None and self.memory.record_contradiction(contradiction):
                trace_record(
                    "adaptive_understanding.contradiction",
                    contradiction_type=contradiction.contradiction_type,
                    contradiction_id=contradiction.contradiction_id,
                    previous=contradiction.previous_observation,
                    current=contradiction.current_observation,
                )

        self.memory.record_assessment(assessment)
        self.memory.end_pass()

        trace_record(
            "adaptive_understanding.assessment",
            url=assessment.url,
            observation_pass=assessment.observation_pass,
            readiness=round(assessment.readiness.readiness_score, 3),
            confidence=round(assessment.observation_confidence.value, 3),
            state=assessment.state.primary_state,
            state_confidence=round(assessment.state.confidence, 3),
            decision=assessment.decision,
            blocking_signals=assessment.readiness.blocking_signal_kinds,
            stable=assessment.stability.stable,
        )

    # -- post-action learning ------------------------------------------------

    def note_transition(
        self,
        *,
        before: UnderstandingAssessment | None,
        after: UnderstandingAssessment | None,
        action_type: str = "",
        action_label: str = "",
    ) -> ObservedStateTransition | None:
        """Record `state A --action--> state B` in APPLICATION-STATE terms.

        Complements `workflow_discovery`'s entity-state transitions: this axis
        answers "what kind of screen did that action take me to", which is a
        different and equally real question.
        """
        if before is None or after is None:
            return None

        from_state = before.state.primary_state
        to_state = after.state.primary_state
        transition_id = f"app-transition:{from_state}->{to_state}:{action_type or 'unknown'}"

        transition = ObservedStateTransition(
            transition_id=transition_id,
            from_state=from_state,
            to_state=to_state,
            action_type=action_type,
            action_label=action_label,
            from_fingerprint=before.fingerprint,
            to_fingerprint=after.fingerprint,
            url_changed=before.url != after.url,
            confidence=min(before.state.confidence, after.state.confidence),
        )

        self.memory.begin_pass()
        stored = self.memory.record_transition(transition)

        # Same action, different destination than last time = a behaviour
        # change worth surfacing rather than silently overwriting.
        action_signature = f"{from_state}|{action_type}|{action_label}".strip()
        previous_outcome = self.memory.note_action_outcome(action_signature, to_state)
        if previous_outcome:
            contradiction = detect_behaviour_contradiction(
                action_signature=action_signature,
                previous_outcome_state=previous_outcome,
                current_outcome_state=to_state,
            )
            if contradiction is not None and self.memory.record_contradiction(contradiction):
                trace_record(
                    "adaptive_understanding.contradiction",
                    contradiction_type=contradiction.contradiction_type,
                    contradiction_id=contradiction.contradiction_id,
                    previous=contradiction.previous_observation,
                    current=contradiction.current_observation,
                )

        self.memory.end_pass()
        return stored

    def note_navigation_result(self, url: str, *, reachable: bool, failure_detail: str = "") -> None:
        """Track destination reachability so a route that stops working
        mid-run is reported rather than silently absorbed."""
        if not url:
            return
        self.memory.begin_pass()
        if not reachable and self.memory.was_reachable(url):
            contradiction = detect_navigation_contradiction(
                url=url,
                previously_reachable=True,
                currently_reachable=False,
                failure_detail=failure_detail,
            )
            if contradiction is not None and self.memory.record_contradiction(contradiction):
                trace_record(
                    "adaptive_understanding.contradiction",
                    contradiction_type=contradiction.contradiction_type,
                    contradiction_id=contradiction.contradiction_id,
                )
        self.memory.note_url_reachable(url, reachable)
        self.memory.end_pass()

    def note_reobservation(self) -> None:
        self.memory.record_reobservation()

    # -- thin pass-throughs ---------------------------------------------------

    def statistics(self):
        return self.query_engine.statistics()
