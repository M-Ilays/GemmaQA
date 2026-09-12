"""Scenario execution eligibility — a thin TRANSLATION layer over signals
Scenario Planning and QA Strategy already compute (`ScenarioFeasibilityAssessment`,
`ScenarioGap.gap_type`, `ExecutionCandidate.recommended_action`/`risk_class`),
re-expressed in the exact vocabulary this milestone's reporting needs.

Deliberately NOT a second feasibility engine: every branch below reads an
already-computed field from `scenario_planning`/`qa_strategy` and maps it to
one `ELIGIBILITY_STATUSES` value plus a human-readable reason — it never
re-derives feasibility from scratch, so there is exactly one place a
scenario's "can this run" answer is actually decided (scenario_feasibility_
analyzer.py), and exactly one place that answer is re-labelled for this
task's reporting contract (here).
"""

from __future__ import annotations

from typing import Any

ELIGIBILITY_STATUSES = frozenset(
    {
        "executable",
        "executable_with_controlled_writes",
        "read_only_executable",
        "blocked_by_missing_actor",
        "blocked_by_missing_state",
        "blocked_by_missing_test_data",
        "blocked_by_unsupported_control",
        "blocked_by_missing_verification",
        "blocked_by_safety_policy",
        "blocked_by_configuration",
        "duplicate",
        "stale",
        "completed",
    }
)

_TERMINAL_SCENARIO_STATUSES = frozenset(
    {"completed", "passed", "failed", "contradicted", "inconclusive", "rejected", "cleaned_up"}
)

_GAP_TYPE_TO_ELIGIBILITY = {
    "missing_actor": "blocked_by_missing_actor",
    "missing_actor_session": "blocked_by_missing_actor",
    "unresolved_actor_handoff": "blocked_by_missing_actor",
    "missing_permission": "blocked_by_missing_actor",
    "permission_contradiction": "blocked_by_missing_actor",
    "missing_entity": "blocked_by_missing_state",
    "missing_entity_state": "blocked_by_missing_state",
    "missing_workflow": "blocked_by_missing_state",
    "incomplete_workflow": "blocked_by_missing_state",
    "missing_workflow_step": "blocked_by_missing_state",
    "missing_transition": "blocked_by_missing_state",
    "missing_prerequisite": "blocked_by_missing_state",
    "unsatisfied_prerequisite": "blocked_by_missing_state",
    "missing_scope": "blocked_by_missing_state",
    "incompatible_scope": "blocked_by_missing_state",
    "missing_output": "blocked_by_missing_state",
    "missing_output_location": "blocked_by_missing_state",
    "unresolved_reference": "blocked_by_missing_state",
    "missing_test_data": "blocked_by_missing_test_data",
    "unknown_data_constraints": "blocked_by_missing_test_data",
    "missing_baseline_method": "blocked_by_missing_verification",
    "missing_observation_method": "blocked_by_missing_verification",
    "missing_comparison_rule": "blocked_by_missing_verification",
    "missing_evidence_requirement": "blocked_by_missing_verification",
    "unsafe_scenario": "blocked_by_safety_policy",
    "irreversible_mutation": "blocked_by_safety_policy",
    "missing_cleanup_path": "blocked_by_missing_verification",
    "stale_graph_context": "stale",
    "contradictory_graph_context": "stale",
    "unsupported_goal_type": "blocked_by_unsupported_control",
    "insufficient_information": "blocked_by_unsupported_control",
    "unknown": "blocked_by_unsupported_control",
}

_NO_MUTATION_TYPES = frozenset({"none"})
_CONTROLLED_WRITE_RISK_CLASSES = frozenset({"moderate", "high"})


class ScenarioEligibility:
    __slots__ = ("status", "reason")

    def __init__(self, status: str, reason: str) -> None:
        self.status = status
        self.reason = reason

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "reason": self.reason}


def classify_eligibility(
    scenario: Any,
    candidate: Any,
    *,
    scenario_engine: Any = None,
    allow_controlled_writes: bool = False,
    allow_safe_test_data_creation: bool = False,
) -> ScenarioEligibility:
    if scenario.status in _TERMINAL_SCENARIO_STATUSES:
        return ScenarioEligibility("completed", f"scenario already reached a terminal outcome ({scenario.status})")
    if scenario.status == "stale" or getattr(scenario, "stale", False):
        return ScenarioEligibility("stale", "scenario was not regenerated this pass (source goal/graph context moved on)")

    recommended = getattr(candidate, "recommended_action", None)
    if recommended == "skip":
        return ScenarioEligibility(
            "duplicate",
            getattr(candidate, "explanation", "") or "a higher-priority alternative for the same goal is already selected",
        )

    if getattr(candidate, "risk_class", "unknown") == "prohibited":
        return ScenarioEligibility("blocked_by_safety_policy", "strategy candidate risk_class is prohibited")

    gaps = []
    if scenario_engine is not None:
        try:
            gaps = scenario_engine.memory.gaps_for_scenario(scenario.scenario_id)
        except Exception:
            gaps = []
    blocking_gaps = [g for g in gaps if getattr(g, "blocking", False)]
    if blocking_gaps:
        gap = blocking_gaps[0]
        mapped = _GAP_TYPE_TO_ELIGIBILITY.get(gap.gap_type, "blocked_by_unsupported_control")
        return ScenarioEligibility(mapped, f"{gap.gap_type}: {gap.description or 'blocking gap recorded by scenario planning'}")

    if recommended == "block":
        reasons = getattr(candidate, "blocking_reasons", None) or []
        return ScenarioEligibility(
            "blocked_by_safety_policy" if not reasons else "blocked_by_missing_state",
            "; ".join(reasons) or "candidate marked block by QA Strategy",
        )
    if recommended == "defer":
        return ScenarioEligibility(
            "blocked_by_missing_state",
            getattr(candidate, "explanation", "") or "deferred pending a prerequisite candidate",
        )

    risk_class = getattr(candidate, "risk_class", "unknown")
    # Whether a step MUTATES state is `mutation_type` (none/create/update/
    # delete/transition/unknown) -- NOT `safety_class`, which grades a
    # step's general risk/significance (a read-only permission check can
    # still be "moderate" safety_class without writing anything).
    requires_write = any(
        getattr(step, "mutation_type", "none") not in _NO_MUTATION_TYPES for step in getattr(scenario, "steps", [])
    )
    if requires_write and not (allow_controlled_writes or allow_safe_test_data_creation):
        return ScenarioEligibility(
            "blocked_by_configuration",
            "scenario requires a controlled write but allow_controlled_writes/allow_safe_test_data_creation is disabled",
        )

    if not requires_write:
        return ScenarioEligibility("read_only_executable", "every step is read-only; no write policy required")
    if risk_class in _CONTROLLED_WRITE_RISK_CLASSES and allow_controlled_writes:
        return ScenarioEligibility(
            "executable_with_controlled_writes",
            "scenario requires a controlled (moderate/high risk) write and allow_controlled_writes permits it",
        )
    return ScenarioEligibility(
        "executable",
        "scenario requires a scoped safe-test-data write and allow_safe_test_data_creation permits it",
    )
