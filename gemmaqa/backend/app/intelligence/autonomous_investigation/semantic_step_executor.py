"""Converts one declarative `ScenarioStep` into a runtime action -- WITHOUT
ever issuing a raw Playwright call. `ScenarioStep` carries only semantic/
graph-level references (entity_ids, workflow_id, permission_id, actor_id),
never a concrete DOM selector, so the only architecturally honest way to
resolve "locate the subject" into a real click/fill is to hand the step's
semantic intent to the EXISTING runtime `Planner` (which already knows how
to read the live `PageState` via `FrontierBuilder`) — never to invent a
parallel selector-matching heuristic here.

Every browser-driving step type therefore delegates to
`Planner.plan_by_priority`, the SAME deterministic, safety-neutral
candidate-selection path Gemma's own fallback already uses — nudged only by
a `testing_objective` string built from the step's own description. Pure
observation/assertion step types never touch the Planner at all: they are
evaluated directly against whatever has already been observed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.planner import screenshot_budget_exhausted
from app.schemas import ActionCategory, ActionType, BrowserAction, RiskLevel

EVIDENCE_STEP_TYPES = frozenset({"establish_baseline", "observe", "capture_evidence"})
OBSERVATION_STEP_TYPES = frozenset({"verify_state", "verify_permission", "verify_denial", "observe_output", "compare"})
WAIT_STEP_TYPES = frozenset({"wait_for_effect"})
# Everything else in STEP_TYPES (establish_session, authenticate_actor,
# navigate, locate_subject, prepare_data, perform_operation,
# perform_workflow_step, trigger_transition, switch_actor, restore_context,
# cleanup, rollback, resolve_scope, unknown) drives the browser via Planner.


@dataclass
class StepResolution:
    kind: str  # "action" | "instant" | "skip"
    action: BrowserAction | None = None
    reason: str = ""


def resolve_step(step, scenario, page_state, memory, planner, context: dict[str, Any]) -> StepResolution:
    step_type = step.step_type

    if step_type in WAIT_STEP_TYPES:
        return StepResolution(
            kind="action",
            action=BrowserAction(
                action=ActionType.WAIT,
                wait_ms=1000,
                reason=step.description or step.semantic_action or "Wait for expected effect",
                expected_result=step.expected_state_change or step.expected_output_change or "Effect settles.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EXPLORATION,
                metadata={"investigation_step_id": step.step_id},
            ),
        )

    if step_type in EVIDENCE_STEP_TYPES:
        if screenshot_budget_exhausted(context):
            return StepResolution(
                kind="instant",
                reason="screenshot budget exhausted; using already-captured observation",
            )
        return StepResolution(
            kind="action",
            action=BrowserAction(
                action=ActionType.TAKE_SCREENSHOT,
                reason=step.description or step.semantic_action or "Capture evidence",
                expected_result="Evidence captured for this checkpoint.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EVIDENCE_CAPTURE,
                metadata={"investigation_step_id": step.step_id},
            ),
        )

    if step_type in OBSERVATION_STEP_TYPES:
        return StepResolution(kind="instant", reason="observation-only step; evaluated from current state")

    step_context = dict(context)
    step_context["testing_objective"] = _objective_for_step(step, scenario)
    step_context["investigation_hints"] = _hints_for_step(step, scenario, page_state, memory)
    action = planner.plan_by_priority(page_state, memory, step_context)
    if action.action == ActionType.FINISH:
        return StepResolution(kind="skip", reason=action.reason or "planner had no viable candidate for this step")
    action.metadata = {**(action.metadata or {}), "investigation_step_id": step.step_id}
    return StepResolution(kind="action", action=action)


def _objective_for_step(step, scenario) -> str:
    parts = [scenario.title or scenario.objective, step.description or step.semantic_action]
    return " — ".join(p for p in parts if p) or "Investigate this scenario step"


# step_type -> the tier a matching frontier candidate should be tagged at
# (see app.agent.planner._apply_investigation_alignment /
# app.agent.priority_engine POSITIVE_WEIGHTS["investigation_alignment"]).
# "cleanup"/"rollback" score lower than an ordinary operation step: they are
# important (must not be starved by unrelated navigation) but are not the
# scenario's PRIMARY objective.
_CLEANUP_STEP_TYPES = frozenset({"cleanup", "rollback"})


def _hints_for_step(step, scenario, page_state, memory) -> dict[str, Any]:
    """Declarative context for THIS step, handed to the Planner/Frontier as
    `context["investigation_hints"]` — never a fixed selector. Cross-
    references `memory.crud_registry` (app.intelligence.crud_discovery,
    when available) to resolve a concrete `required_control_ids` hint from
    an already-corroborated CRUD hypothesis for the same operation/entity —
    the two systems were designed independently but describe the same
    add/edit/delete surfaces, so reusing one from the other here is a real
    integration, not a coincidence."""
    step_type = getattr(step, "step_type", "unknown")
    semantic_action = getattr(step, "semantic_action", "") or ""
    entity_ids = list(getattr(step, "entity_ids", None) or [])
    target_entity = entity_ids[0] if entity_ids else None
    operation = semantic_action or step_type

    required_control_ids: list[str] = []
    crud_registry = getattr(memory, "crud_registry", None)
    if crud_registry is not None and target_entity:
        op_key = _infer_crud_operation(operation)
        if op_key:
            for hyp in crud_registry.by_operation(op_key):
                if hyp.entity_hypothesis and target_entity.lower() in hyp.entity_hypothesis.lower():
                    required_control_ids.extend(hyp.required_controls)

    return {
        "intended_operation": operation,
        "target_entity": target_entity,
        "current_page_context": getattr(page_state, "url", ""),
        "expected_control_semantics": semantic_action,
        "expected_next_state": (
            getattr(step, "expected_state_change", "")
            or getattr(step, "expected_output_change", "")
            or getattr(step, "target_state_id", "")
            or ""
        ),
        "required_evidence": list(getattr(step, "evidence_requirements", None) or []),
        "allowed_mutation_class": getattr(step, "mutation_type", "none"),
        "temporary_record_identity": _temporary_record_identity(scenario, step),
        "forbidden_alternatives": _forbidden_alternatives(step),
        "required_control_ids": required_control_ids,
        "is_cleanup_step": step_type in _CLEANUP_STEP_TYPES,
    }


def _infer_crud_operation(operation: str) -> str | None:
    lowered = (operation or "").lower()
    if any(k in lowered for k in ("add", "create", "new")):
        return "create"
    if any(k in lowered for k in ("edit", "update")):
        return "edit"
    if any(k in lowered for k in ("delete", "remove")):
        return "delete"
    return None


def _temporary_record_identity(scenario, step) -> str | None:
    """A stable, human-readable identity for a record THIS scenario itself
    creates (so a later `cleanup`/`verify` step can refer to "the record
    this scenario made", never an arbitrary pre-existing one). Derived
    deterministically from the scenario id -- never randomly generated,
    per this package's own determinism discipline (see schemas.py)."""
    step_type = getattr(step, "step_type", "unknown")
    if getattr(step, "mutation_type", "none") not in {"create", "unknown"} and step_type not in {"prepare_data", "perform_operation"}:
        return None
    scenario_id = getattr(scenario, "scenario_id", "") or "scenario"
    return f"gemmaqa-test-{scenario_id[-12:]}"


def _forbidden_alternatives(step) -> list[str]:
    """Verbs this step must NOT resolve to, derived from its OWN mutation
    type -- e.g. a `verify_state`/read-shaped step must never resolve to a
    delete/edit action, and a `cleanup` step must never resolve to `create`
    (a cleanup step growing the very state it's meant to remove)."""
    mutation = getattr(step, "mutation_type", "none")
    if getattr(step, "step_type", "unknown") in _CLEANUP_STEP_TYPES:
        return ["add", "create", "new"]
    if mutation == "none":
        return ["delete", "remove", "save", "submit"]
    if mutation == "delete":
        return ["add", "create", "new"]
    return []
