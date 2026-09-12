"""Transparent execution priority scoring. Mirrors `goal_generation/
goal_priority.py` and `scenario_planning/scenario_scoring.py`'s shape: a
weighted sum of independent [0,1] signals, then an explicit multiplicative
penalty step -- never raw multiplication of every signal together, which
would collapse a legitimately important-but-imperfect candidate to near
zero the moment any single signal is low.

Twelve signals are always computed for every candidate; each named
`ExecutionPolicy` just re-weights the SAME twelve differently (rather than
swapping which signals exist per policy), so switching policy never
changes what is measured, only how much each measurement matters.
"""

from __future__ import annotations

BASE_SIGNALS = (
    "business_value", "knowledge_gain", "coverage_gain", "confidence_gain", "risk_reduction_value",
    "workflow_centrality", "blocking_impact", "goal_priority", "scenario_feasibility",
    "speed_value", "safety_value", "read_only_value",
)

POLICY_WEIGHTS: dict[str, dict[str, float]] = {
    "balanced": {
        "business_value": 0.15, "knowledge_gain": 0.10, "coverage_gain": 0.10, "confidence_gain": 0.08,
        "risk_reduction_value": 0.07, "workflow_centrality": 0.08, "blocking_impact": 0.10, "goal_priority": 0.10,
        "scenario_feasibility": 0.12, "speed_value": 0.04, "safety_value": 0.04, "read_only_value": 0.02,
    },
    "fastest_first": {"speed_value": 0.5, "business_value": 0.1, "scenario_feasibility": 0.2, "safety_value": 0.1, "goal_priority": 0.1},
    "highest_value_first": {"business_value": 0.5, "goal_priority": 0.2, "scenario_feasibility": 0.2, "knowledge_gain": 0.1},
    "lowest_risk_first": {"safety_value": 0.45, "read_only_value": 0.15, "scenario_feasibility": 0.2, "business_value": 0.1, "risk_reduction_value": 0.1},
    "read_only_first": {"read_only_value": 0.5, "safety_value": 0.2, "scenario_feasibility": 0.2, "business_value": 0.1},
    "coverage_first": {"coverage_gain": 0.5, "scenario_feasibility": 0.2, "workflow_centrality": 0.15, "goal_priority": 0.15},
    "confidence_first": {"confidence_gain": 0.5, "scenario_feasibility": 0.2, "knowledge_gain": 0.15, "goal_priority": 0.15},
    "business_critical_first": {"business_value": 0.35, "risk_reduction_value": 0.25, "goal_priority": 0.2, "scenario_feasibility": 0.2},
    "dependency_first": {"blocking_impact": 0.5, "scenario_feasibility": 0.2, "workflow_centrality": 0.15, "goal_priority": 0.15},
}
# "custom_weighted" starts from "balanced" and applies the caller's
# `weight_overrides` on top -- see `compute_priority`.

_FEASIBILITY_SCORE = {"feasible": 1.0, "conditionally_feasible": 0.6, "incomplete": 0.2, "unknown": 0.3, "blocked": 0.0}
_READ_ONLY_VALUE = {"read_only": 1.0, "low": 0.5}

_CLEANUP_PENALTY = {"blocked": 0.7, "conditionally_feasible": 0.9, "feasible": 1.0, "unknown": 0.85}
_RISK_PENALTY = {"prohibited": 0.0, "high": 0.5, "moderate": 0.8, "low": 0.95, "read_only": 1.0, "unknown": 0.7}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def derive_signals(
    *, business_value: float, knowledge_gain: float, coverage_gain: float, confidence_gain: float,
    risk_reduction_value: float, workflow_centrality: float, blocking_impact: float, goal_priority: float,
    feasibility_status: str, complexity_score: float, risk_score: float, risk_class: str,
) -> dict[str, float]:
    return {
        "business_value": _clamp(business_value),
        "knowledge_gain": _clamp(knowledge_gain),
        "coverage_gain": _clamp(coverage_gain),
        "confidence_gain": _clamp(confidence_gain),
        "risk_reduction_value": _clamp(risk_reduction_value),
        "workflow_centrality": _clamp(workflow_centrality),
        "blocking_impact": _clamp(blocking_impact),
        "goal_priority": _clamp(goal_priority),
        "scenario_feasibility": _FEASIBILITY_SCORE.get(feasibility_status, 0.3),
        "speed_value": _clamp(1.0 - complexity_score),
        "safety_value": _clamp(1.0 - risk_score),
        "read_only_value": _READ_ONLY_VALUE.get(risk_class, 0.0),
    }


def compute_priority(
    signals: dict[str, float],
    *,
    policy_id: str = "balanced",
    weight_overrides: dict[str, float] | None = None,
    risk_class: str = "unknown",
    cleanup_required: bool = False,
    cleanup_feasibility: str = "feasible",
    actor_availability_unknown: bool = False,
    data_unresolved: bool = False,
    already_completed: bool = False,
    dismissed: bool = False,
) -> tuple[float, float, list[str]]:
    """Returns (final_score, weighted_score, applied_penalty_names)."""
    weights = dict(POLICY_WEIGHTS.get(policy_id, POLICY_WEIGHTS["balanced"]))
    if policy_id == "custom_weighted" and weight_overrides:
        weights = dict(POLICY_WEIGHTS["balanced"])
        weights.update(weight_overrides)
    weighted_score = sum(signals.get(name, 0.0) * weight for name, weight in weights.items())

    penalties: list[str] = []
    multiplier = 1.0

    risk_penalty = _RISK_PENALTY.get(risk_class, 0.7)
    if risk_penalty < 1.0:
        multiplier *= risk_penalty
        penalties.append(f"risk_class:{risk_class}")

    if cleanup_required:
        cleanup_penalty = _CLEANUP_PENALTY.get(cleanup_feasibility, 0.85)
        if cleanup_penalty < 1.0:
            multiplier *= cleanup_penalty
            penalties.append(f"cleanup_feasibility:{cleanup_feasibility}")

    if actor_availability_unknown:
        multiplier *= 0.85
        penalties.append("actor_availability_unknown")

    if data_unresolved:
        multiplier *= 0.9
        penalties.append("data_unresolved")

    if dismissed:
        multiplier *= 0.0
        penalties.append("dismissed")
    elif already_completed:
        multiplier *= 0.05
        penalties.append("already_completed")

    final_score = _clamp(weighted_score * multiplier)
    return final_score, weighted_score, penalties
