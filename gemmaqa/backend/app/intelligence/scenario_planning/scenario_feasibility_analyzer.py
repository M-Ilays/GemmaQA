"""Assesses whether a scenario is currently feasible -- and, critically,
EXPLAINS why (task section "FEASIBILITY ANALYSIS": "Feasibility must be
explainable"). Never marks a scenario feasible merely because a graph
node exists; a node existing only means the requirement is
resolved/satisfiable, not that live execution would succeed.
"""

from __future__ import annotations

from app.intelligence.scenario_planning.schemas import ScenarioFeasibilityAssessment


def assess_feasibility(ctx, validation) -> ScenarioFeasibilityAssessment:
    scenario_id = ctx.candidate.candidate_id
    blocking_reasons: list[str] = []
    conditional_reasons: list[str] = []
    satisfied: list[str] = []
    unresolved: list[str] = []

    all_requirements = [
        *ctx.actor_reqs, *ctx.permission_reqs, *ctx.entity_reqs, *ctx.state_reqs,
        *ctx.workflow_reqs, *ctx.output_reqs, *ctx.data_reqs,
    ]
    for req in all_requirements:
        if req.status in {"satisfied", "satisfiable"}:
            satisfied.append(req.requirement_id)
        else:
            unresolved.append(req.requirement_id)
        if req.mandatory and req.status == "unresolved":
            blocking_reasons.append(f"required {req.requirement_type} '{req.requirement_id}' is unresolved ({req.missing_reason or 'not found in knowledge graph'})")
        elif req.status == "contradicted":
            blocking_reasons.append(f"required {req.requirement_type} '{req.requirement_id}' has contradictory evidence")
        elif req.status == "blocked":
            blocking_reasons.append(f"required {req.requirement_type} '{req.requirement_id}' is blocked")

    for actor_req in ctx.actor_reqs:
        if actor_req.current_availability == "unknown":
            conditional_reasons.append(f"session availability for actor '{actor_req.canonical_name}' is not known")
        if actor_req.role_switch_required:
            conditional_reasons.append(f"actor '{actor_req.canonical_name}' requires a role switch, which this engine only represents, never performs")

    if validation.stale_graph_context:
        conditional_reasons.append(f"goal was planned against an older graph version than the current one")
    if validation.blocked or validation.unresolved_dependency_goal_ids:
        conditional_reasons.append("the source goal has unresolved goal-level dependencies")

    if validation.contradiction_ids:
        blocking_reasons.append("the goal references contradictory graph evidence")
    if validation.already_verified:
        blocking_reasons.append("the underlying knowledge is already verified; no further investigation is needed")
    if not validation.has_context:
        conditional_reasons.append("the goal carries little graph context to plan against")

    if blocking_reasons:
        status = "blocked"
    elif not validation.has_context and not satisfied:
        status = "incomplete"
    elif conditional_reasons:
        status = "conditionally_feasible"
    elif satisfied or not all_requirements:
        status = "feasible"
    else:
        status = "unknown"

    total = len(all_requirements) or 1
    confidence = len(satisfied) / total

    explanation_parts = [f"Status: {status}."]
    if satisfied:
        explanation_parts.append(f"{len(satisfied)}/{total} requirement(s) resolved.")
    if blocking_reasons:
        explanation_parts.append("Blocked because: " + "; ".join(blocking_reasons) + ".")
    elif conditional_reasons:
        explanation_parts.append("Conditional on: " + "; ".join(conditional_reasons) + ".")

    return ScenarioFeasibilityAssessment(
        feasibility_id=f"{scenario_id}:feasibility", scenario_id=scenario_id, feasibility_status=status,
        explanation=" ".join(explanation_parts), blocking_reasons=blocking_reasons, conditional_reasons=conditional_reasons,
        satisfied_requirements=satisfied, unresolved_requirements=unresolved, confidence=confidence,
    )
