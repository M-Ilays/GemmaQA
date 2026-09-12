"""Converts unresolved/missing planning information into explicit
`ScenarioGap` records -- never fabricated as satisfied, never silently
dropped. This is the mechanism behind "blocked scenarios are not
fabricated as feasible" (Definition of Done). Gaps only EXPOSE a
recommended future goal type; they never create a new `InvestigationGoal`
themselves (that stays Goal Generation's exclusive responsibility).
"""

from __future__ import annotations

from app.intelligence.scenario_planning.schemas import ScenarioGap

_RESOLUTION_GOAL_TYPE = {
    "missing_actor": "verify_actor_capability",
    "missing_permission": "verify_permission",
    "permission_contradiction": "verify_permission",
    "missing_entity": "verify_entity_lifecycle",
    "missing_entity_state": "verify_state_transition",
    "missing_workflow": "verify_workflow",
    "incomplete_workflow": "verify_workflow",
    "missing_prerequisite": "validate_prerequisite",
    "unsatisfied_prerequisite": "validate_prerequisite",
    "missing_transition": "verify_state_transition",
    "missing_output": "validate_dashboard_output",
    "unresolved_actor_handoff": "validate_actor_hand_off",
    "unresolved_reference": "resolve_unresolved_reference",
    "contradictory_graph_context": "resolve_contradiction",
}


def _prerequisite_unsatisfied(ctx) -> bool:
    graph = ctx.graph
    if graph is None:
        return False
    for node_id in ctx.goal.supporting_graph_nodes:
        node = graph.memory.nodes.get(node_id)
        if node is not None and node.node_type == "prerequisite" and node.attributes.get("satisfied") is False:
            return True
    return False


def analyze_gaps(ctx, validation, *, steps: list, comparisons: list, evidence_requirements: list, cleanup_plan, risk_assessment) -> list[ScenarioGap]:
    gaps: list[ScenarioGap] = []
    scenario_id = ctx.candidate.candidate_id

    def add(gap_type: str, description: str, *, blocking: bool = False, severity: str = "low", confidence: float = 0.3) -> None:
        gaps.append(
            ScenarioGap(
                gap_id=f"{scenario_id}:gap:{gap_type}:{len(gaps)}", scenario_id=scenario_id, gap_type=gap_type,
                description=description, blocking=blocking, severity=severity, confidence=confidence,
                source_goal_id=ctx.goal.goal_id, recommended_resolution_goal_type=_RESOLUTION_GOAL_TYPE.get(gap_type, ""),
            )
        )

    for req in ctx.actor_reqs:
        if req.status == "unresolved":
            add("missing_actor", f"Actor '{req.canonical_name}' is unknown to the graph.", blocking=True, severity="high")
        elif req.current_availability == "unknown":
            add("missing_actor_session", f"Session availability for actor '{req.canonical_name}' is unknown.", severity="medium")

    for req in ctx.permission_reqs:
        if req.status == "unresolved":
            add("missing_permission", f"Permission '{req.requirement_id}' is unknown to the graph.", blocking=True, severity="high")
        elif req.status == "contradicted":
            add("permission_contradiction", f"Permission '{req.requirement_id}' has contradictory evidence.", blocking=True, severity="high")

    for req in ctx.entity_reqs:
        if req.status == "unresolved":
            add("missing_entity", f"Entity '{req.canonical_name}' is unknown to the graph.", blocking=True, severity="high")

    for req in ctx.state_reqs:
        if req.status == "unresolved":
            add("missing_entity_state", f"State '{req.state_label}' is unknown to the graph.", severity="medium")

    for req in ctx.workflow_reqs:
        if req.status == "unresolved":
            add("missing_workflow", f"Workflow '{req.canonical_name}' is unknown to the graph.", blocking=True, severity="high")
        elif not req.required_steps:
            add("incomplete_workflow", f"Workflow '{req.canonical_name}' has no known steps.", severity="medium")

    for req in ctx.output_reqs:
        if req.status == "unresolved":
            add("missing_output", f"Output '{req.requirement_id}' is unknown to the graph.", severity="medium")

    for req in ctx.data_reqs:
        if req.status not in {"satisfied"}:
            add("missing_test_data", f"Data for entity type '{req.entity_type}' in state '{req.required_state or 'any'}' is not confirmed available.", severity="low")

    if ctx.candidate.scenario_type == "prerequisite_verification" and _prerequisite_unsatisfied(ctx):
        add("unsatisfied_prerequisite", "The prerequisite is currently recorded as unsatisfied.", severity="medium", confidence=0.5)
    if ctx.candidate.scenario_type == "prerequisite_verification" and not ctx.state_reqs and not ctx.workflow_reqs:
        add("missing_prerequisite", "No prerequisite context could be resolved from the graph.", blocking=True, severity="high")

    if ctx.candidate.scenario_type in {"state_transition_verification", "dependency_verification", "metric_verification", "workflow_verification"}:
        has_source_or_target = any(s.source_state_id or s.target_state_id for s in steps)
        if not has_source_or_target:
            add("missing_transition", "No known source/target state transition to trigger or verify.", severity="medium")

    if ctx.candidate.scenario_type == "scope_verification" and not any(r.scope for r in ctx.output_reqs):
        add("missing_scope", "No scope could be resolved for this output.", severity="medium")

    if ctx.candidate.scenario_type in {"dependency_verification", "metric_verification"} and not comparisons:
        add("missing_comparison_rule", "No before/after comparison could be planned.", severity="medium")

    if not evidence_requirements:
        add("missing_evidence_requirement", "No evidence requirement could be planned for this scenario.", severity="medium")

    if cleanup_plan.cleanup_required and cleanup_plan.unresolved_cleanup_gaps:
        add(
            "missing_cleanup_path", "; ".join(cleanup_plan.unresolved_cleanup_gaps),
            blocking=(cleanup_plan.cleanup_feasibility == "blocked"),
            severity="high" if cleanup_plan.cleanup_feasibility == "blocked" else "medium",
        )

    if any(s.reversibility == "irreversible" for s in steps):
        add("irreversible_mutation", "Scenario includes an irreversible mutation with no known rollback.", blocking=True, severity="high")

    if ctx.candidate.scenario_type == "actor_handoff_verification" and any(r.current_availability == "unknown" for r in ctx.actor_reqs[1:]):
        add("unresolved_actor_handoff", "Availability of the second (hand-off) actor's session is unknown.", severity="medium")

    if ctx.goal.goal_type == "resolve_unresolved_reference":
        add("unresolved_reference", f"Goal targets an unresolved graph reference: {ctx.goal.description}", severity="medium")

    if validation.missing_node_ids:
        add("stale_graph_context", f"{len(validation.missing_node_ids)} required graph node(s) are missing or stale.", severity="medium")

    if validation.contradiction_ids:
        add("contradictory_graph_context", f"Goal references {len(validation.contradiction_ids)} contradiction(s).", blocking=True, severity="high")

    if risk_assessment.risk_class == "prohibited":
        add("unsafe_scenario", "Scenario matches a prohibited safety-sensitive pattern.", blocking=True, severity="high")

    if ctx.candidate.scenario_type == "unknown":
        add("unsupported_goal_type", f"Goal type '{ctx.goal.goal_type}' has no dedicated scenario template.", severity="medium")

    if not validation.has_context and not gaps:
        add("insufficient_information", "Not enough graph context to plan this scenario.", blocking=True, severity="high")

    return gaps
