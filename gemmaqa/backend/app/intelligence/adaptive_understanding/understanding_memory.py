"""Store for what this run has come to understand about the application's
behaviour.

Mirrors the pass-scoped, idempotent-versioning pattern used by
`KnowledgeGraphMemory`/`GoalMemory`/`ScenarioMemory`: re-observing an
unchanged screen must not manufacture new records or churn the version
counter.

Everything here is per-run and in-process. Prior-run knowledge is
deliberately NOT loaded: this engine's contract is that previous knowledge
is a hypothesis to be re-verified, and the honest way to honour that today
-- given GemmaQA has no cross-run persistence -- is to keep the memory
scoped to the run that observed it.
"""

from __future__ import annotations

from app.intelligence.adaptive_understanding.schemas import (
    ObservedStateTransition,
    UnderstandingAssessment,
    UnderstandingContradiction,
)


class UnderstandingMemory:
    def __init__(self) -> None:
        # Latest assessment per structural fingerprint -- the basis for
        # noticing that "this same screen" is now understood differently.
        self.assessments_by_fingerprint: dict[str, UnderstandingAssessment] = {}
        self.assessment_history: list[UnderstandingAssessment] = []
        self.contradictions: dict[str, UnderstandingContradiction] = {}
        self.transitions: dict[str, ObservedStateTransition] = {}

        # Reachability and behaviour ledgers, used to detect contradictions.
        self.reachable_urls: set[str] = set()
        self.unreachable_urls: set[str] = set()
        self.action_outcomes: dict[str, str] = {}

        self.understanding_version: int = 0
        self.total_reobservations: int = 0
        self.degraded_acceptances: int = 0

        self.dirty_this_pass: bool = False

    # -- pass bracketing ----------------------------------------------------

    def begin_pass(self) -> None:
        self.dirty_this_pass = False

    def end_pass(self) -> bool:
        if self.dirty_this_pass:
            self.understanding_version += 1
        return self.dirty_this_pass

    # -- assessments --------------------------------------------------------

    def previous_for_fingerprint(self, fingerprint: str) -> UnderstandingAssessment | None:
        return self.assessments_by_fingerprint.get(fingerprint) if fingerprint else None

    def record_assessment(self, assessment: UnderstandingAssessment) -> None:
        self.assessment_history.append(assessment)
        if assessment.fingerprint:
            previous = self.assessments_by_fingerprint.get(assessment.fingerprint)
            if previous is None or previous.state.primary_state != assessment.state.primary_state:
                self.dirty_this_pass = True
            self.assessments_by_fingerprint[assessment.fingerprint] = assessment
        else:
            self.dirty_this_pass = True
        if assessment.decision == "accept_degraded":
            self.degraded_acceptances += 1

    def record_reobservation(self) -> None:
        self.total_reobservations += 1

    # -- contradictions -----------------------------------------------------

    def record_contradiction(self, contradiction: UnderstandingContradiction) -> bool:
        """Returns True when this is a genuinely new contradiction. Seeing the
        same disagreement twice is not two findings."""
        if contradiction.contradiction_id in self.contradictions:
            return False
        self.contradictions[contradiction.contradiction_id] = contradiction
        self.dirty_this_pass = True
        return True

    # -- transitions --------------------------------------------------------

    def record_transition(self, transition: ObservedStateTransition) -> ObservedStateTransition:
        existing = self.transitions.get(transition.transition_id)
        if existing is None:
            self.transitions[transition.transition_id] = transition
            self.dirty_this_pass = True
            return transition
        existing.observation_count += 1
        # Repeated observation of the same transition raises confidence in it,
        # with diminishing returns and a hard ceiling below certainty.
        existing.confidence = min(0.95, existing.confidence + 0.1)
        return existing

    # -- reachability / behaviour ledgers -----------------------------------

    def note_url_reachable(self, url: str, reachable: bool) -> None:
        if not url:
            return
        if reachable:
            self.reachable_urls.add(url)
            self.unreachable_urls.discard(url)
        else:
            self.unreachable_urls.add(url)

    def was_reachable(self, url: str) -> bool:
        return url in self.reachable_urls

    def note_action_outcome(self, action_signature: str, outcome_state: str) -> str | None:
        """Records where an action led. Returns the PREVIOUS outcome for the
        same action signature, if there was one, so the caller can check for
        a behaviour contradiction."""
        if not action_signature:
            return None
        previous = self.action_outcomes.get(action_signature)
        self.action_outcomes[action_signature] = outcome_state
        return previous
