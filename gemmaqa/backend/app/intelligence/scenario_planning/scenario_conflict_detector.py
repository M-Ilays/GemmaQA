"""Detects (and RETAINS -- never silently drops) conflicts within one
scenario. Every check here has a genuine graph/requirement signal behind
it; none are invented from proximity alone.
"""

from __future__ import annotations

from app.intelligence.scenario_planning.schemas import ScenarioConflict


def detect_conflicts(ctx, validation, steps: list, cleanup_plan) -> list[ScenarioConflict]:
    conflicts: list[ScenarioConflict] = []
    scenario_id = ctx.candidate.candidate_id

    def add(conflict_type: str, description: str, severity: str = "medium") -> None:
        conflicts.append(
            ScenarioConflict(
                conflict_id=f"{scenario_id}:conflict:{conflict_type}:{len(conflicts)}", scenario_ids=[scenario_id],
                conflict_type=conflict_type, description=description, severity=severity,
            )
        )

    for perm_req in ctx.permission_reqs:
        if perm_req.status == "contradicted":
            add("permission_contradiction", f"Permission '{perm_req.permission_id}' has both positive and negative evidence.", "high")

    scopes = {r.scope for r in ctx.output_reqs if r.scope}
    if len(scopes) > 1:
        add("incompatible_scope", f"Output requirements reference incompatible scopes: {sorted(scopes)}.", "medium")

    if ctx.candidate.mutation_level == "read_only" and any(s.mutation_type != "none" for s in steps):
        add("mixed_mutation_readonly", "Scenario is marked read-only but contains a mutating step.", "medium")

    if validation.stale_graph_context or validation.missing_node_ids:
        add("stale_reference", "Scenario references a stale or missing graph node.", "low")

    if cleanup_plan.cleanup_required:
        denied_permissions = {p.permission_id for p in ctx.permission_reqs if p.status == "blocked" and p.permission_id}
        if denied_permissions & set(cleanup_plan.cleanup_permissions):
            add("cleanup_permission_unavailable", "Cleanup requires a permission the actor is known to lack.", "high")

    return conflicts


def detect_duplicate_alternatives(scenarios_for_goal: list) -> list[ScenarioConflict]:
    """Cross-scenario check within ONE goal's alternatives -- flags (never
    removes) alternatives that ended up materially identical."""
    conflicts: list[ScenarioConflict] = []
    seen: dict[tuple, str] = {}
    for scenario in sorted(scenarios_for_goal, key=lambda s: s.scenario_id):
        signature = (
            scenario.scenario_type,
            tuple(sorted(r.actor_id for r in scenario.actor_requirements)),
            tuple(sorted(scenario.source_graph_nodes)),
            tuple(s.mutation_type for s in scenario.steps),
        )
        if signature in seen:
            conflicts.append(
                ScenarioConflict(
                    conflict_id=f"conflict:duplicate_alternative:{seen[signature]}<->{scenario.scenario_id}",
                    scenario_ids=[seen[signature], scenario.scenario_id], conflict_type="duplicate_alternative",
                    description=f"'{scenario.scenario_id}' and '{seen[signature]}' are materially identical alternatives.",
                    severity="low",
                )
            )
        else:
            seen[signature] = scenario.scenario_id
    return conflicts
