"""Maps a validated goal onto one or more `ScenarioCandidate` drafts:
scenario-type selection (`scenario_template_registry`'s deterministic
mapping, evidence-resolved for the one genuinely ambiguous case --
`verify_permission`), plus a bounded set of materially-distinct
alternatives (never cosmetic duplicates).
"""

from __future__ import annotations

from app.intelligence.scenario_planning.scenario_decomposer import GoalValidation
from app.intelligence.scenario_planning.scenario_template_registry import scenario_types_for_goal_type
from app.intelligence.scenario_planning.schemas import ScenarioCandidate

MAX_SCENARIOS_PER_GOAL = 4  # primary + up to 3 alternatives

_MUTATING_SCENARIO_TYPES = {
    "workflow_verification", "dependency_verification", "metric_verification",
    "state_transition_verification", "branch_verification", "actor_handoff_verification",
    "permission_positive_verification",
}


def _candidate_id(goal_id: str, scenario_type: str, variant: str) -> str:
    return f"scenario:{scenario_type}:{goal_id}:{variant}"


def build_candidates(validation: GoalValidation) -> list[ScenarioCandidate]:
    goal = validation.goal

    if not validation.can_plan:
        return [
            ScenarioCandidate(
                candidate_id=_candidate_id(goal.goal_id, "unknown", "incomplete"),
                goal_id=goal.goal_id, scenario_type="unknown", variant="incomplete",
                title=f"Incomplete plan for '{goal.title}'",
                objective="The goal's supporting graph context is missing or stale; planning cannot proceed.",
                mutation_level="read_only", actor_count_estimate=0,
                rationale="; ".join(validation.warnings) or "goal cannot currently be planned",
            )
        ]

    if goal.goal_type == "verify_permission":
        candidates = _permission_candidates(goal, validation)
    else:
        primary_type = scenario_types_for_goal_type(goal.goal_type)[0]
        candidates = [_candidate(goal, primary_type, "primary", _mutation_level_for(primary_type), "Directly perform the investigation and observe the result.")]
        candidates.extend(_alternatives_for(goal, primary_type, validation))

    return candidates[:MAX_SCENARIOS_PER_GOAL]


def _mutation_level_for(scenario_type: str) -> str:
    return "mutating" if scenario_type in _MUTATING_SCENARIO_TYPES else "read_only"


def _candidate(goal, scenario_type: str, variant: str, mutation_level: str, rationale: str) -> ScenarioCandidate:
    return ScenarioCandidate(
        candidate_id=_candidate_id(goal.goal_id, scenario_type, variant),
        goal_id=goal.goal_id, scenario_type=scenario_type, variant=variant,
        title=f"{goal.title} ({variant})" if variant != "primary" else goal.title,
        objective=goal.description or goal.title,
        template_id=scenario_type, mutation_level=mutation_level,
        actor_count_estimate=max(1, len(goal.required_actors)),
        rationale=rationale,
    )


def _permission_candidates(goal, validation: GoalValidation) -> list[ScenarioCandidate]:
    graph = validation.graph
    has_positive, has_negative = True, False
    if graph is not None and goal.supporting_graph_nodes:
        permission_node_id = goal.supporting_graph_nodes[0]
        incoming = [
            graph.memory.edges[e] for e in graph.memory.incoming_edge_ids(permission_node_id)
            if e in graph.memory.edges and not graph.memory.edges[e].stale
        ]
        found_positive = any(e.edge_type == "has_permission" for e in incoming)
        found_negative = any(e.edge_type == "lacks_permission" for e in incoming)
        if found_positive or found_negative:
            has_positive, has_negative = found_positive, found_negative

    candidates = []
    if has_positive:
        candidates.append(_candidate(goal, "permission_positive_verification", "primary", "mutating", "Positive permission evidence is known; confirm the actor can perform the operation."))
    if has_negative:
        variant = "primary" if not has_positive else "negative_capability"
        candidates.append(_candidate(goal, "permission_negative_verification", variant, "low", "Negative permission evidence is known; confirm the actor is denied the operation."))
    if not candidates:
        candidates.append(_candidate(goal, "permission_positive_verification", "primary", "mutating", "No prior permission evidence; attempt the operation to discover the actual result."))
    return candidates


def _alternatives_for(goal, primary_type: str, validation: GoalValidation) -> list[ScenarioCandidate]:
    alternatives: list[ScenarioCandidate] = []
    if primary_type in _MUTATING_SCENARIO_TYPES:
        alternatives.append(
            _candidate(goal, primary_type, "observational", "read_only", "Locate an already-completed example and compare evidence without mutating state.")
        )
    if primary_type == "workflow_verification" and _workflow_has_multiple_steps(goal, validation.graph):
        alternatives.append(
            _candidate(goal, primary_type, "reduced_scope", "mutating", "Verify one transition before attempting the full workflow lifecycle.")
        )
    return alternatives


def _workflow_has_multiple_steps(goal, graph) -> bool:
    if graph is None or not goal.supporting_graph_nodes:
        return False
    workflow_node_id = next((n for n in goal.supporting_graph_nodes if n.startswith("workflow:")), None)
    if workflow_node_id is None:
        return False
    try:
        return len(graph.query_engine.workflow_steps(workflow_node_id)) > 1
    except Exception:
        return False
