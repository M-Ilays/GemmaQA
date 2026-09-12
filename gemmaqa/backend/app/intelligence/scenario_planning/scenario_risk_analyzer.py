"""Risk, cleanup, and rollback analysis. Reuses the existing Safety
Validator's `PROHIBITED_INTENT_PATTERNS` as an ADVISORY planning-time
signal (task: "Use graph evidence and existing Safety Validator semantics
where reusable... must not override Safety Validator decisions") -- this
engine never executes anything, so it can only ever flag a scenario as
prohibited at the planning level; the live `ActionValidator` remains the
sole authority at execution time.
"""

from __future__ import annotations

from app.safety.action_levels import PROHIBITED_INTENT_PATTERNS
from app.intelligence.scenario_planning.schemas import ScenarioCleanupPlan, ScenarioRiskAssessment, ScenarioRollbackPlan

_COMPONENT_WEIGHTS = {
    "destructive_mutation": 0.20,
    "irreversible_transition": 0.15,
    "cross_actor": 0.10,
    "permission_escalation": 0.10,
    "cleanup_uncertainty": 0.15,
    "prohibited_intent": 0.15,
    "long_workflow": 0.05,
    "evidence_ambiguity": 0.10,
}


def assess_risk(ctx, steps: list, comparisons: list) -> tuple[ScenarioRiskAssessment, ScenarioCleanupPlan, ScenarioRollbackPlan]:
    scenario_id = ctx.candidate.candidate_id
    # ONLY scan each step's own `semantic_action` -- our own controlled
    # template vocabulary, which for a real workflow step already embeds
    # its actual verb/name (e.g. "...({step_name})"), so a genuinely risky
    # step ("delete", "publish") still gets caught. Goal/step TITLES are
    # deliberately excluded: they are free text from Goal Generation that
    # routinely discusses "permission"/"notification"/etc. as the
    # investigation SUBJECT (not a dangerous action), and scanning them
    # produced a false "prohibited" classification on every permission- or
    # notification-related goal (discovered via smoke-testing this engine).
    combined_text = " ".join(s.semantic_action for s in steps).lower()
    applied_flags = sorted({p for p in PROHIBITED_INTENT_PATTERNS if p in combined_text})

    mutating_steps = [s for s in steps if s.mutation_type != "none"]
    is_mutating = bool(mutating_steps)
    has_delete = any(s.mutation_type == "delete" for s in steps)
    has_irreversible = any(s.reversibility == "irreversible" for s in steps)
    has_conditional = any(s.reversibility == "conditionally_reversible" for s in steps)
    cross_actor = len(ctx.actor_reqs) > 1

    cleanup_required = is_mutating
    unresolved_cleanup_gaps: list[str] = []
    if not cleanup_required:
        cleanup_feasibility = "feasible"
    elif has_irreversible:
        cleanup_feasibility = "blocked"
        unresolved_cleanup_gaps.append("mutation includes an irreversible step; no known rollback path")
    else:
        cleanup_feasibility = "conditionally_feasible"
        unresolved_cleanup_gaps.append("cleanup path is not directly known in the graph; relies on reversing the same transition")

    cleanup_plan = ScenarioCleanupPlan(
        cleanup_required=cleanup_required, cleanup_steps=[s.step_id for s in mutating_steps],
        cleanup_actor=ctx.primary_actor.canonical_name if ctx.primary_actor else "",
        cleanup_permissions=list(ctx.primary_actor.required_permissions) if ctx.primary_actor else [],
        cleanup_state=ctx.state_reqs[0].state_label if ctx.state_reqs else "",
        cleanup_feasibility=cleanup_feasibility, unresolved_cleanup_gaps=unresolved_cleanup_gaps,
    )

    rollback_possible = cleanup_required and not has_irreversible
    rollback_plan = ScenarioRollbackPlan(
        rollback_possible=rollback_possible,
        rollback_actor=ctx.primary_actor.canonical_name if ctx.primary_actor else "",
        rollback_risk="high" if has_irreversible else ("moderate" if cleanup_required else "read_only"),
        irreversible_reason="a step is marked irreversible with no known rollback transition" if has_irreversible else "",
        fallback_containment="prefer the observational/reduced-scope alternative instead" if has_irreversible else "",
    )

    components = {
        "destructive_mutation": 1.0 if has_delete else 0.0,
        "irreversible_transition": 1.0 if has_irreversible else (0.4 if has_conditional else 0.0),
        "cross_actor": 1.0 if cross_actor else 0.0,
        "permission_escalation": 0.5 if ctx.candidate.scenario_type == "permission_positive_verification" and is_mutating else 0.0,
        "cleanup_uncertainty": {"blocked": 1.0, "conditionally_feasible": 0.5}.get(cleanup_feasibility, 0.0),
        "prohibited_intent": 1.0 if applied_flags else 0.0,
        "long_workflow": min(1.0, len(steps) / 12.0),
        "evidence_ambiguity": 1.0 if any(c.inconclusive_conditions for c in comparisons) else 0.2,
    }
    weighted_score = sum(components[k] * _COMPONENT_WEIGHTS[k] for k in _COMPONENT_WEIGHTS)

    if applied_flags:
        risk_class = "prohibited"
    elif not is_mutating:
        risk_class = "read_only"
    elif has_delete or has_irreversible:
        risk_class = "high"
    elif cross_actor or components["permission_escalation"] > 0:
        risk_class = "moderate"
    else:
        risk_class = "low"

    explanation = f"risk_class={risk_class}; " + ", ".join(f"{k}={v:.2f}" for k, v in components.items())
    if applied_flags:
        explanation += f"; matched safety-sensitive terms: {', '.join(applied_flags)}"

    risk_assessment = ScenarioRiskAssessment(
        risk_id=f"{scenario_id}:risk", scenario_id=scenario_id, risk_class=risk_class,
        final_risk_score=weighted_score, components=components, applied_flags=applied_flags, explanation=explanation,
    )
    return risk_assessment, cleanup_plan, rollback_plan
