"""Builds one explainable `ExecutionRecommendation` per candidate. Every
recommendation traces its decision back to fields already computed on the
candidate -- no new scoring happens here, only explanation."""

from __future__ import annotations

from app.intelligence.qa_strategy.schemas import ExecutionCandidate, ExecutionRecommendation


def build_recommendation(candidate: ExecutionCandidate, *, policy_id: str) -> ExecutionRecommendation:
    reasons = [f"priority_score={candidate.priority_score:.2f} under policy '{policy_id}'"]
    if candidate.applied_penalties:
        reasons.append("penalties applied: " + ", ".join(candidate.applied_penalties))
    if candidate.blocking_reasons:
        reasons.append("blocked by: " + "; ".join(candidate.blocking_reasons))
    if candidate.depends_on_candidate_ids:
        reasons.append("depends on: " + ", ".join(candidate.depends_on_candidate_ids))
    reasons.append(f"queue={candidate.queue_type}, batch={candidate.batch_id or 'none'}")

    return ExecutionRecommendation(
        recommendation_id=f"recommendation:{candidate.candidate_id}",
        candidate_id=candidate.candidate_id,
        decision=candidate.recommended_action,
        reasons=reasons,
        expected_gain=candidate.priority_score,
        expected_coverage=candidate.coverage_gain,
        expected_confidence=candidate.confidence_gain,
        expected_risk=candidate.risk_score,
        estimated_duration_class=candidate.estimated_duration_class,
        required_actors=list(candidate.required_actors),
        required_cleanup=candidate.required_cleanup,
        required_data=candidate.required_data,
    )


def build_recommendations(candidates: list[ExecutionCandidate], *, policy_id: str) -> list[ExecutionRecommendation]:
    return [build_recommendation(c, policy_id=policy_id) for c in sorted(candidates, key=lambda c: c.candidate_id)]
