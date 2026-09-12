"""Transparent scenario scoring + complexity estimation. Mirrors
`goal_generation/goal_priority.py`'s shape (weighted sum + explicit
penalty, not raw multiplication -- for the same reason: multiplying eight
independent [0,1] signals collapses to near-zero the moment any single one
is low, which would make a freshly-discovered but genuinely important
scenario score near 0 just because, say, `scope_clarity` happens to be
unresolved). A scenario is only driven near zero deliberately, via the
penalty step, when it is actually blocked or prohibited.

This module does NOT select the globally best scenario -- it only makes
every component available so a future QA Strategy Engine can compare
alternatives on a level, explainable footing.
"""

from __future__ import annotations

_SCORE_WEIGHTS = {
    "information_gain": 0.20,
    "goal_coverage": 0.15,
    "evidence_strength": 0.15,
    "feasibility": 0.15,
    "determinism": 0.10,
    "reversibility": 0.10,
    "graph_confidence": 0.10,
    "scope_clarity": 0.05,
}

_FEASIBILITY_SCORE = {"feasible": 1.0, "conditionally_feasible": 0.6, "incomplete": 0.2, "unknown": 0.3, "blocked": 0.0}
_REVERSIBILITY_SCORE = {"reversible": 1.0, "conditionally_reversible": 0.5, "irreversible": 0.0, "unknown": 0.4}

_COMPLEXITY_THRESHOLDS = (("trivial", 3), ("simple", 6), ("moderate", 10), ("complex", 16))


def _avg(values: list[float], default: float = 0.5) -> float:
    return (sum(values) / len(values)) if values else default


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def estimate_counts(steps: list) -> dict:
    return {
        "estimated_action_count": len(steps),
        "estimated_actor_switches": sum(1 for s in steps if s.step_type == "switch_actor"),
        "estimated_navigation_count": sum(1 for s in steps if s.step_type == "navigate"),
        "estimated_state_mutations": sum(1 for s in steps if s.mutation_type != "none"),
    }


def estimate_complexity(*, steps: list, actor_requirements: list, branches: list, comparisons: list, data_requirements: list, cleanup_plan) -> tuple[float, str]:
    actor_switches = sum(1 for s in steps if s.step_type == "switch_actor")
    mutation_count = sum(1 for s in steps if s.mutation_type != "none")
    cleanup_count = len(cleanup_plan.cleanup_steps) if cleanup_plan is not None else 0
    raw = len(steps) + 2 * actor_switches + len(branches) + mutation_count + len(comparisons) + len(data_requirements) + 0.5 * cleanup_count
    score = min(1.0, raw / 20.0)
    complexity_class = "very_complex"
    for label, threshold in _COMPLEXITY_THRESHOLDS:
        if raw <= threshold:
            complexity_class = label
            break
    return score, complexity_class


def score_scenario(*, scenario_confidence: float, goal_priority_score: float, evidence_requirements: list, feasibility_status: str, branches: list, steps: list, output_requirements: list) -> dict:
    information_gain = _clamp(1.0 - scenario_confidence)
    goal_coverage = _clamp(goal_priority_score)
    evidence_strength = _clamp(_avg([e.sufficiency_weight for e in evidence_requirements], default=0.3))
    feasibility_component = _FEASIBILITY_SCORE.get(feasibility_status, 0.3)
    determinism = _clamp(1.0 - 0.15 * len(branches))
    reversibility = _clamp(_avg([_REVERSIBILITY_SCORE.get(s.reversibility, 0.4) for s in steps], default=0.5))
    graph_confidence = _clamp(scenario_confidence)
    scope_clarity = 1.0 if not output_requirements else _avg([1.0 if r.scope or r.status == "satisfied" else 0.5 for r in output_requirements])

    signals = {
        "information_gain": information_gain, "goal_coverage": goal_coverage, "evidence_strength": evidence_strength,
        "feasibility": feasibility_component, "determinism": determinism, "reversibility": reversibility,
        "graph_confidence": graph_confidence, "scope_clarity": scope_clarity,
    }
    weighted_score = sum(signals[k] * _SCORE_WEIGHTS[k] for k in _SCORE_WEIGHTS)

    return {
        "information_gain_score": information_gain,
        "confidence_gain_estimate": _clamp(information_gain * feasibility_component),
        "reversibility_score": reversibility,
        "determinism_score": determinism,
        "priority_hint": weighted_score,
    }
