"""Before/after comparison planning (task section "BEFORE/AFTER PLANNING").
Only produced when the step builder actually emitted an `output_before`/
`output_after` checkpoint pair (dependency/metric-verification templates).
Never invents a numerical delta -- direction comes ONLY from the
dependency edge's own `effect_direction` attribute; when that is
"unknown", the comparison operator stays "unknown" too.
"""

from __future__ import annotations

from app.intelligence.scenario_planning.schemas import ScenarioComparison

_DIRECTION_TO_OPERATOR = {"increase": ("increases", "increase"), "decrease": ("decreases", "decrease")}


def _effect_direction(ctx) -> str:
    graph = ctx.graph
    if graph is None:
        return "unknown"
    for edge_id in ctx.goal.supporting_graph_edges:
        edge = graph.memory.edges.get(edge_id)
        if edge is not None and "effect_direction" in edge.attributes:
            direction = edge.attributes.get("effect_direction")
            if direction in _DIRECTION_TO_OPERATOR:
                return direction
    return "unknown"


def plan_comparisons(ctx, steps: list, checkpoints: list) -> list[ScenarioComparison]:
    before_cp = next((c for c in checkpoints if c.checkpoint_type == "output_before"), None)
    after_cp = next((c for c in checkpoints if c.checkpoint_type == "output_after"), None)
    if before_cp is None or after_cp is None:
        return []

    direction = _effect_direction(ctx)
    operator, expected_direction = _DIRECTION_TO_OPERATOR.get(direction, ("unknown", "unknown"))

    inconclusive: list[str] = []
    if any(s.step_type == "wait_for_effect" for s in steps):
        inconclusive.append("output may update after a delay longer than the observation window")
    scope_requirements: list[str] = []
    if ctx.primary_output is not None and ctx.primary_output.scope:
        scope_requirements.append(ctx.primary_output.scope)
        inconclusive.append(f"comparison is only valid within scope: {ctx.primary_output.scope}")
    if operator == "unknown":
        inconclusive.append("dependency graph does not record a known effect direction")

    comparison = ScenarioComparison(
        comparison_id=f"{ctx.candidate.candidate_id}:comparison:0",
        subject_type="output", subject_id=ctx.primary_output.output_id if ctx.primary_output else "",
        baseline_checkpoint_id=before_cp.checkpoint_id, final_checkpoint_id=after_cp.checkpoint_id,
        comparison_operator=operator, expected_direction=expected_direction, expected_delta=None, tolerance=None,
        scope_requirements=scope_requirements, inconclusive_conditions=inconclusive,
        evidence_requirements=[],
    )
    return [comparison]
