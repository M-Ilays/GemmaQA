"""Represents KNOWN branches -- from actual `workflow_branch` graph nodes,
from unresolved actor-session availability, and from the possibility of a
delayed effect (mirroring an emitted `wait_for_effect` step). Never
invents a branch the graph, goal, or generic safety requirements don't
support.
"""

from __future__ import annotations

from app.intelligence.scenario_planning.schemas import ScenarioBranch

_APPROVAL_WORDS = {"approve", "approved", "reject", "rejected", "approval", "rejection"}
_SUCCESS_WORDS = {"success", "failure", "succeeded", "failed", "valid", "invalid"}
_PERMISSION_WORDS = {"allow", "allowed", "deny", "denied", "permit", "permitted"}


def _classify_branch_type(canonical_name: str, option_labels: list[str]) -> str:
    lowered = {canonical_name.lower(), *(o.lower() for o in option_labels)}
    if lowered & _APPROVAL_WORDS:
        return "approval_rejection"
    if lowered & _SUCCESS_WORDS:
        return "success_failure"
    if lowered & _PERMISSION_WORDS:
        return "permission_allow_deny"
    return "unknown"


def build_branches(ctx, steps: list) -> list[ScenarioBranch]:
    branches: list[ScenarioBranch] = []
    graph = ctx.graph
    scenario_id = ctx.candidate.candidate_id
    decision_step_id = next((s.step_id for s in steps if s.step_type in {"perform_workflow_step", "trigger_transition"}), (steps[0].step_id if steps else ""))

    if graph is not None and ctx.primary_workflow is not None and ctx.primary_workflow.workflow_id:
        for e_id in sorted(graph.memory.outgoing_edge_ids(ctx.primary_workflow.workflow_id)):
            edge = graph.memory.edges.get(e_id)
            if edge is None or edge.stale or edge.edge_type != "branches_to":
                continue
            branch_node = graph.memory.nodes.get(edge.target_node_id)
            if branch_node is None:
                continue
            option_labels = list(branch_node.attributes.get("option_labels", []))
            branches.append(
                ScenarioBranch(
                    branch_id=f"{scenario_id}:branch:{len(branches)}", source_step_id=decision_step_id,
                    condition=branch_node.canonical_name, condition_source=branch_node.node_id,
                    confidence=branch_node.confidence, branch_type=_classify_branch_type(branch_node.canonical_name, option_labels),
                    expected_outcome="; ".join(option_labels), terminal=True, fallback=False, unresolved=not option_labels,
                )
            )

    for actor_req in ctx.actor_reqs:
        if actor_req.current_availability == "unknown":
            branches.append(
                ScenarioBranch(
                    branch_id=f"{scenario_id}:branch:{len(branches)}", source_step_id=decision_step_id,
                    condition=f"actor '{actor_req.canonical_name}' session available vs unavailable",
                    condition_source=actor_req.requirement_id, confidence=0.3, branch_type="actor_session_availability",
                    expected_outcome="proceeds if available; scenario becomes conditionally feasible otherwise",
                    terminal=False, fallback=True, unresolved=True,
                )
            )

    if any(s.step_type == "wait_for_effect" for s in steps):
        wait_step_id = next(s.step_id for s in steps if s.step_type == "wait_for_effect")
        branches.append(
            ScenarioBranch(
                branch_id=f"{scenario_id}:branch:{len(branches)}", source_step_id=wait_step_id,
                condition="output updates immediately vs after a delay", condition_source="template",
                confidence=0.3, branch_type="immediate_vs_delayed",
                expected_outcome="comparison may be inconclusive if the delay exceeds the observation window",
                terminal=False, fallback=True, unresolved=True,
            )
        )

    return branches
