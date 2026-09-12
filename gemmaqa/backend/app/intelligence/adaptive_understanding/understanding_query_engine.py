"""Query API over understanding memory -- the only way callers read it.

Same role and discipline as `strategy_query_engine.py`/
`investigation_query_engine.py`: simple, bounded, deterministic lookups,
never a place for new reasoning.
"""

from __future__ import annotations

from app.intelligence.adaptive_understanding.schemas import UnderstandingStatistics


class UnderstandingQueryEngine:
    def __init__(self, memory) -> None:
        self.memory = memory

    # -- assessments --------------------------------------------------------

    def latest_assessment(self):
        history = self.memory.assessment_history
        return history[-1] if history else None

    def assessment_history(self) -> list:
        return list(self.memory.assessment_history)

    def assessment_for_fingerprint(self, fingerprint: str):
        return self.memory.assessments_by_fingerprint.get(fingerprint)

    def assessments_by_state(self, state: str) -> list:
        return [a for a in self.memory.assessment_history if a.state.primary_state == state]

    def low_confidence_assessments(self, threshold: float = 0.6) -> list:
        return [a for a in self.memory.assessment_history if a.observation_confidence.value < threshold]

    def degraded_assessments(self) -> list:
        return [a for a in self.memory.assessment_history if a.decision == "accept_degraded"]

    def unknown_state_assessments(self) -> list:
        return self.assessments_by_state("unknown")

    # -- contradictions -----------------------------------------------------

    def contradictions(self) -> list:
        return sorted(self.memory.contradictions.values(), key=lambda c: c.contradiction_id)

    def contradictions_by_type(self, contradiction_type: str) -> list:
        return [c for c in self.contradictions() if c.contradiction_type == contradiction_type]

    def open_contradictions(self) -> list:
        return [c for c in self.contradictions() if c.status == "open"]

    # -- transitions --------------------------------------------------------

    def transitions(self) -> list:
        return sorted(self.memory.transitions.values(), key=lambda t: t.transition_id)

    def transitions_from(self, state: str) -> list:
        return [t for t in self.transitions() if t.from_state == state]

    def transitions_to(self, state: str) -> list:
        return [t for t in self.transitions() if t.to_state == state]

    def known_states(self) -> list[str]:
        states = {a.state.primary_state for a in self.memory.assessment_history}
        return sorted(states)

    # -- statistics ---------------------------------------------------------

    def statistics(self) -> UnderstandingStatistics:
        history = self.memory.assessment_history
        by_state: dict[str, int] = {}
        by_decision: dict[str, int] = {}
        readiness_sum = 0.0
        confidence_sum = 0.0

        for a in history:
            by_state[a.state.primary_state] = by_state.get(a.state.primary_state, 0) + 1
            by_decision[a.decision] = by_decision.get(a.decision, 0) + 1
            readiness_sum += a.readiness.readiness_score
            confidence_sum += a.observation_confidence.value

        by_contradiction_type: dict[str, int] = {}
        for c in self.memory.contradictions.values():
            by_contradiction_type[c.contradiction_type] = by_contradiction_type.get(c.contradiction_type, 0) + 1

        count = len(history) or 1
        return UnderstandingStatistics(
            total_assessments=len(history),
            total_reobservations=self.memory.total_reobservations,
            degraded_acceptances=self.memory.degraded_acceptances,
            assessments_by_state=by_state,
            assessments_by_decision=by_decision,
            average_readiness_score=readiness_sum / count,
            average_observation_confidence=confidence_sum / count,
            contradiction_count=len(self.memory.contradictions),
            contradictions_by_type=by_contradiction_type,
            transition_count=len(self.memory.transitions),
            unknown_state_count=by_state.get("unknown", 0),
            understanding_version=self.memory.understanding_version,
        )
