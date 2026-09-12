"""Heuristic, explicitly-approximate forecasts of coverage/confidence/risk
AFTER the candidates currently recommended `execute` actually run.

These are projections, not guarantees: `confidence_in_forecast` is kept
deliberately low-to-moderate (never above 0.6) and every forecast's
`basis` spells out the exact formula used, so a caller can judge how much
weight to give it rather than trusting it blindly.
"""

from __future__ import annotations

from app.intelligence.qa_strategy.schemas import ExecutionCandidate, ExecutionForecast


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def build_forecasts(candidates: list[ExecutionCandidate], graph_statistics) -> list[ExecutionForecast]:
    executing = [c for c in candidates if c.recommended_action == "execute"]
    total_candidates = max(1, len(candidates))
    execute_fraction = len(executing) / total_candidates

    total_nodes = graph_statistics.total_nodes if graph_statistics else 0
    gap_count = graph_statistics.gap_count if graph_statistics else 0
    consistency_issues = graph_statistics.consistency_issue_count if graph_statistics else 0

    baseline_coverage = _clamp(1.0 - (gap_count / total_nodes)) if total_nodes else 0.5
    avg_coverage_gain = sum(c.coverage_gain for c in executing) / max(1, len(executing)) if executing else 0.0
    projected_coverage = _clamp(baseline_coverage + avg_coverage_gain * execute_fraction * 0.5)

    baseline_confidence = _clamp(1.0 - (consistency_issues / total_nodes)) if total_nodes else 0.5
    avg_confidence_gain = sum(c.confidence_gain for c in executing) / max(1, len(executing)) if executing else 0.0
    projected_confidence = _clamp(baseline_confidence + avg_confidence_gain * execute_fraction * 0.5)

    baseline_risk = _clamp(sum(c.risk_score for c in candidates) / total_candidates) if candidates else 0.0
    avg_risk_reduction = sum(c.risk_reduction_value for c in executing) / max(1, len(executing)) if executing else 0.0
    projected_risk = _clamp(baseline_risk - avg_risk_reduction * execute_fraction * 0.5)

    return [
        ExecutionForecast(
            forecast_id="forecast:coverage",
            forecast_type="coverage",
            baseline_value=baseline_coverage,
            projected_value=projected_coverage,
            projected_delta=projected_coverage - baseline_coverage,
            basis=f"baseline=1-(gap_count/total_nodes); projected=+avg(candidate.coverage_gain)*execute_fraction*0.5 over {len(executing)} executing candidates.",
            confidence_in_forecast=0.4,
            explanation="Approximate: assumes each executing candidate contributes its own estimated coverage_gain independently.",
        ),
        ExecutionForecast(
            forecast_id="forecast:confidence",
            forecast_type="confidence",
            baseline_value=baseline_confidence,
            projected_value=projected_confidence,
            projected_delta=projected_confidence - baseline_confidence,
            basis=f"baseline=1-(consistency_issue_count/total_nodes); projected=+avg(candidate.confidence_gain)*execute_fraction*0.5 over {len(executing)} executing candidates.",
            confidence_in_forecast=0.4,
            explanation="Approximate: assumes each executing candidate contributes its own estimated confidence_gain independently.",
        ),
        ExecutionForecast(
            forecast_id="forecast:risk",
            forecast_type="risk",
            baseline_value=baseline_risk,
            projected_value=projected_risk,
            projected_delta=projected_risk - baseline_risk,
            basis=f"baseline=avg(candidate.risk_score); projected=-avg(candidate.risk_reduction_value)*execute_fraction*0.5 over {len(executing)} executing candidates.",
            confidence_in_forecast=0.35,
            explanation="Approximate: does not model residual risk from candidates that remain blocked, deferred, or skipped.",
        ),
    ]
