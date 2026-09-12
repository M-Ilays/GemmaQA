"""Defines what evidence would be SUFFICIENT, SUPPORTING, or OPTIONAL for
each step -- this engine never captures evidence itself, only declares
what would count. See `schemas.EVIDENCE_ROLES`/`SCENARIO_EVIDENCE_TYPES`.
"""

from __future__ import annotations

from app.intelligence.scenario_planning.schemas import ScenarioEvidenceRequirement

_STEP_TYPE_TO_EVIDENCE_TYPE = {
    "observe_output": "metric_value",
    "verify_state": "entity_state",
    "verify_permission": "form_state",
    "verify_denial": "permission_denial",
    "capture_evidence": "screenshot_reference",
    "establish_baseline": "page_state",
    "observe": "page_state",
    "trigger_transition": "workflow_trace",
    "perform_workflow_step": "workflow_trace",
    "authenticate_actor": "actor_identity",
    "establish_session": "actor_identity",
}
_OUTPUT_TYPE_TO_EVIDENCE_TYPE = {
    "counter": "counter_value", "chart": "chart_value", "report": "report_row", "queue": "queue_membership",
    "notification": "notification", "badge": "badge_value", "derived_output": "metric_value",
}
_SUFFICIENT_STEP_TYPES = {"observe_output", "verify_state", "verify_denial", "verify_permission"}


def plan_evidence(ctx, steps: list, checkpoints: list) -> list[ScenarioEvidenceRequirement]:
    requirements: list[ScenarioEvidenceRequirement] = []
    for step in steps:
        evidence_type = _STEP_TYPE_TO_EVIDENCE_TYPE.get(step.step_type)
        if evidence_type is None:
            continue
        if step.step_type == "observe_output" and ctx.primary_output is not None:
            evidence_type = _OUTPUT_TYPE_TO_EVIDENCE_TYPE.get(ctx.primary_output.output_type, "metric_value")

        checkpoint_id = next(iter(step.postconditions), "") or next(iter(step.preconditions), "")
        role = "sufficient" if step.step_type in _SUFFICIENT_STEP_TYPES else "supporting"
        target = step.entity_ids[0] if step.entity_ids else (step.workflow_id or step.actor_id)

        req_id = f"{ctx.candidate.candidate_id}:evidence:{len(requirements)}"
        requirements.append(
            ScenarioEvidenceRequirement(
                evidence_requirement_id=req_id, evidence_type=evidence_type, target_object_id=target,
                checkpoint_id=checkpoint_id, mandatory=not step.optional, expected_condition=step.title,
                sufficiency_weight=0.8 if role == "sufficient" else 0.4, evidence_role=role,
                source_graph_relationship=",".join(step.source_graph_nodes[:1]),
            )
        )
        step.evidence_requirements.append(req_id)

    return requirements
