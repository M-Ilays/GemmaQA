"""Goal priority scoring -- a transparent, fully explainable model.

The commissioning task describes priority as combining eight factors with
"x" between them. Multiplying eight independent [0, 1] signals together
collapses to (near) zero the moment ANY single factor is low -- e.g. a
brand-new gap on an otherwise-isolated node would always have
`graph_centrality == 0`, which would zero out an otherwise legitimate,
high-business-value goal. That would make the model useless in exactly the
cases it exists to prioritise (freshly-discovered, poorly-connected gaps).

Every other confidence/scoring model already in this codebase (see
`graph_confidence.py`, `dependency_confidence.py`) uses a weighted
combination plus an explicit, separately-explained penalty step instead of
raw multiplication -- so this module follows the same, consistent shape:
a WEIGHTED SUM of the eight signals produces a base score, and the
"Penalty:" factors the task lists separately (already verified, duplicate,
superseded, low business impact) are then applied as an explicit
multiplicative discount on top. Every component -- each raw signal, its
weight, the resulting weighted score, every applied penalty, and the final
number -- is preserved on the returned `GoalPriority`, never collapsed to
just the final float.
"""

from __future__ import annotations

from app.intelligence.goal_generation.schemas import GoalCandidate, GoalPriority
from app.intelligence.knowledge_graph.schemas import GraphTraversalConstraint

_BUSINESS_VALUE_BY_GOAL_TYPE: dict[str, float] = {
    "verify_kpi": 0.85,
    "validate_report": 0.75,
    "verify_dependency": 0.70,
    "validate_aggregation_rule": 0.70,
    "resolve_contradiction": 0.60,
    "validate_dashboard_output": 0.60,
    "verify_business_rule": 0.60,
    "verify_workflow": 0.60,
    "verify_permission": 0.55,
    "verify_actor_capability": 0.50,
    "validate_actor_hand_off": 0.50,
    "verify_entity_lifecycle": 0.45,
    "verify_state_transition": 0.45,
    "validate_ownership": 0.45,
    "validate_prerequisite": 0.40,
    "validate_workflow_branch": 0.40,
    "validate_inferred_relationship": 0.35,
    "validate_notification": 0.35,
    "validate_scope": 0.30,
    "resolve_graph_gap": 0.30,
    "resolve_unresolved_reference": 0.25,
    "increase_confidence": 0.20,
}

_HIGH_VALUE_CONSUMER_EDGE_TYPES = {"visible_to", "generated_by"}
_CENTRALITY_TRAVERSAL_DEPTH = 2
_CENTRALITY_MAX_RESULTS = 50

# Weights sum to 1.0 -- chosen so business value and risk (the two most
# directly "should we care" signals) dominate, while centrality/dependency
# impact/confidence gap act as smaller tie-breaking nudges.
WEIGHTS: dict[str, float] = {
    "business_value": 0.20,
    "risk": 0.15,
    "knowledge_gain": 0.15,
    "coverage_improvement": 0.15,
    "dependency_impact": 0.10,
    "graph_centrality": 0.10,
    "blocking_severity": 0.10,
    "confidence_gap": 0.05,
}

PENALTY_ALREADY_VERIFIED = 0.1
PENALTY_SUPERSEDED = 0.05
PENALTY_DUPLICATE = 0.2
PENALTY_LOW_BUSINESS_IMPACT = 0.5
LOW_BUSINESS_IMPACT_THRESHOLD = 0.15


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_priority(
    goal_id: str,
    *,
    business_value: float,
    risk: float,
    knowledge_gain: float,
    coverage_improvement: float,
    dependency_impact: float,
    graph_centrality: float,
    blocking_severity: float,
    confidence_gap: float,
    already_verified: bool = False,
    superseded: bool = False,
    duplicate: bool = False,
) -> GoalPriority:
    signals = {
        "business_value": _clamp(business_value),
        "risk": _clamp(risk),
        "knowledge_gain": _clamp(knowledge_gain),
        "coverage_improvement": _clamp(coverage_improvement),
        "dependency_impact": _clamp(dependency_impact),
        "graph_centrality": _clamp(graph_centrality),
        "blocking_severity": _clamp(blocking_severity),
        "confidence_gap": _clamp(confidence_gap),
    }
    weighted_score = sum(signals[k] * WEIGHTS[k] for k in WEIGHTS)

    penalties: list[str] = []
    penalty_multiplier = 1.0
    if already_verified:
        penalty_multiplier *= PENALTY_ALREADY_VERIFIED
        penalties.append("already_verified")
    if superseded:
        penalty_multiplier *= PENALTY_SUPERSEDED
        penalties.append("superseded")
    if duplicate:
        penalty_multiplier *= PENALTY_DUPLICATE
        penalties.append("duplicate")
    if signals["business_value"] < LOW_BUSINESS_IMPACT_THRESHOLD:
        penalty_multiplier *= PENALTY_LOW_BUSINESS_IMPACT
        penalties.append("low_business_impact")

    final_score = _clamp(weighted_score * penalty_multiplier)

    breakdown = " + ".join(f"{k}={signals[k]:.2f}×{WEIGHTS[k]:.2f}" for k in WEIGHTS)
    penalty_text = f"penalties: {', '.join(penalties)} (×{penalty_multiplier:.2f})" if penalties else "penalties: none"
    explanation = f"{breakdown} = weighted {weighted_score:.3f}; {penalty_text}; final {final_score:.3f}"

    return GoalPriority(
        goal_id=goal_id,
        business_value=signals["business_value"],
        risk=signals["risk"],
        knowledge_gain=signals["knowledge_gain"],
        coverage_improvement=signals["coverage_improvement"],
        dependency_impact=signals["dependency_impact"],
        graph_centrality=signals["graph_centrality"],
        blocking_severity=signals["blocking_severity"],
        confidence_gap=signals["confidence_gap"],
        weighted_score=weighted_score,
        penalty_multiplier=penalty_multiplier,
        applied_penalties=penalties,
        final_score=final_score,
        explanation=explanation,
    )


def max_node_degree(memory) -> int:
    """The highest (in + out) degree of any non-stale node this pass --
    the normalising denominator for `graph_centrality`. Recomputed once per
    generation pass, never cached across passes (the graph changes)."""
    best = 0
    for node_id, node in memory.nodes.items():
        if node.stale:
            continue
        degree = len(memory.outgoing_edge_ids(node_id)) + len(memory.incoming_edge_ids(node_id))
        if degree > best:
            best = degree
    return best


def derive_priority_signals(candidate: GoalCandidate, *, graph, max_degree: int) -> dict[str, float]:
    """Derives the eight priority signals for one candidate purely from
    graph evidence already attached to it plus a small number of bounded
    graph queries -- never from anything outside the Knowledge Graph."""
    memory = graph.memory

    business_value = _BUSINESS_VALUE_BY_GOAL_TYPE.get(candidate.goal_type, 0.40)
    consumer_hits = 0
    for node_id in candidate.supporting_graph_nodes:
        consumer_hits += sum(
            1 for e in memory.incoming_edge_ids(node_id) if e in memory.edges and memory.edges[e].edge_type in _HIGH_VALUE_CONSUMER_EDGE_TYPES
        )
    business_value = _clamp(business_value + min(0.15, consumer_hits * 0.05))

    evidence_count = len(candidate.supporting_evidence)
    risk = _clamp(
        0.15
        + (0.35 if candidate.contradictions else 0.0)
        + (0.25 if candidate.blocking_gaps else 0.0)
        + 0.25 * (1.0 - candidate.source_confidence)
    )

    knowledge_gain = _clamp(0.5 * (1.0 - candidate.source_confidence) + 0.5 * min(1.0, evidence_count / 3.0))

    if candidate.source_gap_ids:
        coverage_improvement = _clamp(len(candidate.source_gap_ids) / 4.0)
    elif candidate.goal_type in {"resolve_contradiction", "resolve_unresolved_reference", "validate_inferred_relationship"}:
        coverage_improvement = 0.5
    else:
        coverage_improvement = 0.3

    dependency_impact = 0.0
    pivot_node = candidate.supporting_graph_nodes[-1] if candidate.supporting_graph_nodes else None
    if pivot_node is not None and pivot_node in memory.nodes:
        constraint = GraphTraversalConstraint(max_depth=_CENTRALITY_TRAVERSAL_DEPTH, max_results=_CENTRALITY_MAX_RESULTS)
        descendants = graph.query_engine.descendants(pivot_node, constraint=constraint)
        dependency_impact = _clamp(len(descendants) / 8.0)

    graph_centrality = 0.0
    if candidate.supporting_graph_nodes and max_degree > 0:
        degrees = [
            len(memory.outgoing_edge_ids(n)) + len(memory.incoming_edge_ids(n))
            for n in candidate.supporting_graph_nodes if n in memory.nodes
        ]
        if degrees:
            graph_centrality = _clamp((sum(degrees) / len(degrees)) / max_degree)

    if candidate.blocking_gaps:
        blocking_severity = 1.0
    elif candidate.contradictions:
        blocking_severity = 0.6
    else:
        blocking_severity = 0.15

    confidence_gap = _clamp(1.0 - candidate.source_confidence)

    return {
        "business_value": business_value,
        "risk": risk,
        "knowledge_gain": knowledge_gain,
        "coverage_improvement": coverage_improvement,
        "dependency_impact": dependency_impact,
        "graph_centrality": graph_centrality,
        "blocking_severity": blocking_severity,
        "confidence_gap": confidence_gap,
    }
