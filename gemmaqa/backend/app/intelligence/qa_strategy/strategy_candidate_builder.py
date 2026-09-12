"""Builds one `ExecutionCandidate` per scenario. Runs in two phases:

Phase 1 (`build_candidate`, per-scenario, LOCAL information only): derives
priority signals + a provisional `recommended_action` from risk,
feasibility, and the goal's own lifecycle status alone.

Phase 2 (`refine_actions`, cross-candidate, called once all candidates AND
execution dependencies exist): finalises `execute` vs `defer` based on
whether prerequisites will actually run, and marks non-primary scenario
variants `skip` when their primary sibling is already being executed
(redundancy handling) -- promoting the best surviving alternative only
when the primary itself is blocked.
"""

from __future__ import annotations

from app.intelligence.qa_strategy.schemas import ExecutionCandidate
from app.intelligence.qa_strategy.strategy_scoring import compute_priority, derive_signals


def _workflow_centrality(scenario, graph, max_degree: int) -> float:
    if graph is None or max_degree <= 0 or not scenario.source_graph_nodes:
        return 0.0
    memory = graph.memory
    degrees = [
        len(memory.outgoing_edge_ids(n)) + len(memory.incoming_edge_ids(n))
        for n in scenario.source_graph_nodes if n in memory.nodes
    ]
    if not degrees:
        return 0.0
    return max(0.0, min(1.0, (sum(degrees) / len(degrees)) / max_degree))


def _local_recommended_action(scenario, *, risk_class: str, dismissed: bool) -> str:
    if dismissed:
        return "skip"
    if risk_class == "prohibited":
        return "block"
    if scenario.feasibility_status == "blocked":
        return "block"
    if scenario.feasibility_status == "incomplete":
        return "skip"
    return "execute"


def _build_explanation(scenario, signals: dict[str, float], final_score: float, penalties: list[str], action: str) -> str:
    top_signals = sorted(signals.items(), key=lambda kv: -kv[1])[:3]
    signal_text = ", ".join(f"{name}={value:.2f}" for name, value in top_signals)
    parts = [f"Recommended: {action} (priority={final_score:.3f}).", f"Strongest signals: {signal_text}."]
    if penalties:
        parts.append(f"Penalties applied: {', '.join(penalties)}.")
    if scenario.feasibility_assessment and scenario.feasibility_assessment.blocking_reasons:
        parts.append("Blocked because: " + "; ".join(scenario.feasibility_assessment.blocking_reasons) + ".")
    elif scenario.feasibility_assessment and scenario.feasibility_assessment.conditional_reasons:
        parts.append("Conditional on: " + "; ".join(scenario.feasibility_assessment.conditional_reasons) + ".")
    return " ".join(parts)


def build_candidate(
    scenario, goal, graph, *, max_degree: int, policy_id: str = "balanced", weight_overrides: dict[str, float] | None = None,
    blocking_impact: float = 0.0,
) -> ExecutionCandidate:
    candidate_id = f"candidate:{scenario.scenario_id}"

    primary_actor = scenario.actor_requirements[0].canonical_name if scenario.actor_requirements else ""
    primary_workflow = scenario.workflow_requirements[0].canonical_name if scenario.workflow_requirements else ""
    primary_entity = scenario.entity_requirements[0].canonical_name if scenario.entity_requirements else ""
    primary_output = scenario.output_requirements[0].requirement_id if scenario.output_requirements else ""

    risk_class = scenario.risk_assessment.risk_class if scenario.risk_assessment else "unknown"
    feasibility_status = scenario.feasibility_status

    business_value = goal.business_value if goal is not None else 0.4
    goal_priority = goal.priority_score if goal is not None else 0.3
    risk_reduction_value = goal.risk_score if goal is not None else 0.2
    coverage_gain = goal.coverage_value if goal is not None else 0.3
    knowledge_gain = scenario.information_gain_score
    confidence_gain = scenario.confidence_gain_estimate
    workflow_centrality = _workflow_centrality(scenario, graph, max_degree)

    signals = derive_signals(
        business_value=business_value, knowledge_gain=knowledge_gain, coverage_gain=coverage_gain,
        confidence_gain=confidence_gain, risk_reduction_value=risk_reduction_value, workflow_centrality=workflow_centrality,
        blocking_impact=blocking_impact, goal_priority=goal_priority, feasibility_status=feasibility_status,
        complexity_score=scenario.complexity_score, risk_score=scenario.risk_score, risk_class=risk_class,
    )

    cleanup_required = bool(scenario.cleanup_plan and scenario.cleanup_plan.cleanup_required)
    cleanup_feasibility = scenario.cleanup_plan.cleanup_feasibility if scenario.cleanup_plan else "feasible"
    actor_unknown = any(r.current_availability == "unknown" for r in scenario.actor_requirements)
    data_unresolved = any(r.status != "satisfied" for r in scenario.data_requirements)
    dismissed = goal is not None and goal.goal_status == "dismissed"
    already_completed = goal is not None and goal.goal_status == "completed"

    final_score, weighted_score, penalties = compute_priority(
        signals, policy_id=policy_id, weight_overrides=weight_overrides, risk_class=risk_class,
        cleanup_required=cleanup_required, cleanup_feasibility=cleanup_feasibility,
        actor_availability_unknown=actor_unknown, data_unresolved=data_unresolved,
        already_completed=already_completed, dismissed=dismissed,
    )

    recommended_action = _local_recommended_action(scenario, risk_class=risk_class, dismissed=dismissed)
    explanation = _build_explanation(scenario, signals, final_score, penalties, recommended_action)

    return ExecutionCandidate(
        candidate_id=candidate_id, scenario_id=scenario.scenario_id, goal_id=scenario.goal_id, scenario_type=scenario.scenario_type,
        priority_score=final_score, business_value=business_value, risk_score=scenario.risk_score, coverage_gain=coverage_gain,
        confidence_gain=confidence_gain, knowledge_gain=knowledge_gain, risk_reduction_value=risk_reduction_value,
        workflow_centrality=workflow_centrality, blocking_impact=blocking_impact, feasibility_status=feasibility_status, risk_class=risk_class,
        recommended_action=recommended_action, primary_actor=primary_actor, primary_workflow=primary_workflow,
        primary_entity=primary_entity, primary_output=primary_output,
        required_actors=sorted({r.canonical_name for r in scenario.actor_requirements if r.canonical_name}),
        required_cleanup=cleanup_required, required_data=bool(scenario.data_requirements),
        estimated_duration_class=scenario.estimated_runtime_class, explanation=explanation, priority_breakdown=signals,
        applied_penalties=penalties, graph_version=scenario.graph_version, goal_version=scenario.goal_version,
        blocking_reasons=list(scenario.feasibility_assessment.blocking_reasons) if scenario.feasibility_assessment else [],
    )


def refine_actions(candidates: list[ExecutionCandidate], dependencies: list) -> None:
    """Mutates candidates in place: finalises execute-vs-defer from
    dependency readiness, and marks non-primary scenario variants
    redundant when their primary sibling already executes."""
    by_id = {c.candidate_id: c for c in candidates}
    deps_by_candidate: dict[str, list] = {}
    for dep in dependencies:
        deps_by_candidate.setdefault(dep.candidate_id, []).append(dep)

    # -- redundancy: skip non-primary siblings of an executing primary ------
    by_goal: dict[str, list[ExecutionCandidate]] = {}
    for c in candidates:
        by_goal.setdefault(c.goal_id, []).append(c)
    for goal_id, siblings in by_goal.items():
        if len(siblings) < 2:
            continue
        primary = next((c for c in siblings if c.scenario_id.endswith(":primary")), None)
        if primary is None:
            continue
        if primary.recommended_action == "execute":
            for sibling in siblings:
                if sibling is primary or sibling.recommended_action in {"block", "skip"}:
                    continue
                sibling.recommended_action = "skip"
                sibling.explanation += " Skipped: redundant with the primary scenario for the same goal."

    # -- dependency readiness: execute -> defer when a blocking prerequisite
    #    won't itself run yet (either still deferred, or executing later) --
    for candidate in sorted(candidates, key=lambda c: c.candidate_id):
        if candidate.recommended_action != "execute":
            continue
        required_ids = []
        for dep in deps_by_candidate.get(candidate.candidate_id, []):
            if not dep.blocking:
                continue
            required = by_id.get(dep.required_candidate_id)
            if required is None:
                continue
            required_ids.append(required.candidate_id)
            if required.recommended_action in {"block", "skip"}:
                candidate.recommended_action = "block"
                candidate.blocking_reasons.append(f"prerequisite '{required.scenario_id}' will not execute")
                candidate.explanation += f" Blocked: prerequisite '{required.scenario_id}' will not execute."
                break
            if required.recommended_action != "execute" or required.priority_score < candidate.priority_score:
                # covers "required is itself deferred" and "required hasn't
                # been prioritised ahead of this one yet" -- either way,
                # this candidate must wait its turn.
                pass
        candidate.depends_on_candidate_ids = sorted(set(required_ids))
        if candidate.recommended_action == "execute" and required_ids:
            unmet = [rid for rid in required_ids if by_id[rid].recommended_action == "execute" and by_id[rid].priority_score < candidate.priority_score]
            if unmet:
                candidate.recommended_action = "defer"
                candidate.explanation += f" Deferred: waiting on prerequisite(s) {unmet}."
