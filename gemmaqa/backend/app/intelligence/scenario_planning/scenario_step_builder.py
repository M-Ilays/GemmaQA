"""Expands a `ScenarioTemplate` into concrete `ScenarioStep`/`ScenarioCheckpoint`
records using the goal's resolved requirements. Every step is a SEMANTIC
planning action ("perform the workflow's ordered steps") -- never a
browser-implementation instruction (no selectors, no Playwright calls).
"""

from __future__ import annotations

from app.safety.action_levels import PROHIBITED_INTENT_PATTERNS
from app.intelligence.scenario_planning.schemas import ScenarioCheckpoint, ScenarioPostcondition, ScenarioPrecondition, ScenarioStep
from app.intelligence.scenario_planning.scenario_template_registry import ScenarioTemplate, template_for_scenario_type

_MUTATION_TO_SAFETY_CLASS = {"none": "read_only", "create": "low", "update": "low", "transition": "moderate", "delete": "high", "unknown": "unknown"}
_MUTATION_TO_REVERSIBILITY = {"none": "reversible", "create": "conditionally_reversible", "update": "conditionally_reversible", "transition": "conditionally_reversible", "delete": "irreversible", "unknown": "unknown"}


def _safety_class(mutation_type: str, *texts: str) -> str:
    combined = " ".join(t.lower() for t in texts if t)
    if any(pattern in combined for pattern in PROHIBITED_INTENT_PATTERNS):
        return "prohibited"
    return _MUTATION_TO_SAFETY_CLASS.get(mutation_type, "unknown")


def _workflow_source_target_states(graph, workflow_node_id: str) -> tuple[str, str]:
    if graph is None or not workflow_node_id:
        return "", ""
    try:
        steps = graph.query_engine.workflow_steps(workflow_node_id)
    except Exception:
        return "", ""
    source, target = "", ""
    for step in steps:
        for e_id in graph.memory.outgoing_edge_ids(step.node_id):
            edge = graph.memory.edges.get(e_id)
            if edge is None or edge.stale:
                continue
            if edge.edge_type == "from_state" and not source:
                source = edge.target_node_id
            elif edge.edge_type == "to_state":
                target = edge.target_node_id
    return source, target


class StepBuildContext:
    """The shared per-candidate planning context threaded through the whole
    downstream pipeline (steps, evidence, comparisons, branches,
    feasibility, risk, gaps) -- built once per candidate, never mutated
    after construction except for appending ids onto already-built step
    objects (e.g. `step.evidence_requirements.append(...)`)."""

    def __init__(self, *, candidate, goal, graph, actor_reqs, permission_reqs, entity_reqs, state_reqs, workflow_reqs, output_reqs, data_reqs) -> None:
        self.candidate = candidate
        self.goal = goal
        self.graph = graph
        self.actor_reqs = actor_reqs
        self.permission_reqs = permission_reqs
        self.entity_reqs = entity_reqs
        self.state_reqs = state_reqs
        self.workflow_reqs = workflow_reqs
        self.output_reqs = output_reqs
        self.data_reqs = data_reqs

    @property
    def primary_actor(self):
        return self.actor_reqs[0] if self.actor_reqs else None

    @property
    def primary_workflow(self):
        return self.workflow_reqs[0] if self.workflow_reqs else None

    @property
    def primary_entity(self):
        return self.entity_reqs[0] if self.entity_reqs else None

    @property
    def primary_output(self):
        return self.output_reqs[0] if self.output_reqs else None

    @property
    def primary_permission(self):
        return self.permission_reqs[0] if self.permission_reqs else None


def build_steps(ctx: StepBuildContext) -> tuple[list[ScenarioStep], list[ScenarioCheckpoint]]:
    template: ScenarioTemplate = template_for_scenario_type(ctx.candidate.scenario_type)
    scenario_id = ctx.candidate.candidate_id
    steps: list[ScenarioStep] = []
    checkpoints: list[ScenarioCheckpoint] = []
    source_state, target_state = _workflow_source_target_states(ctx.graph, ctx.primary_workflow.workflow_id if ctx.primary_workflow else "")
    if not source_state and ctx.state_reqs:
        source_state = ctx.state_reqs[0].requirement_id
    if not target_state and len(ctx.state_reqs) > 1:
        target_state = ctx.state_reqs[-1].requirement_id

    def add_checkpoint(checkpoint_type: str) -> str:
        idx = len(checkpoints)
        cp_id = f"{scenario_id}:checkpoint:{checkpoint_type}:{idx}"
        checkpoints.append(
            ScenarioCheckpoint(
                checkpoint_id=cp_id, scenario_id=scenario_id, sequence_index=idx, checkpoint_type=checkpoint_type,
                title=checkpoint_type.replace("_", " ").title(),
                actor_context=ctx.primary_actor.canonical_name if ctx.primary_actor else "",
                entity_context=ctx.primary_entity.canonical_name if ctx.primary_entity else "",
            )
        )
        return cp_id

    def make_step(*, seq_index: int, step_type: str, title: str, semantic_action: str, mutation_type: str, optional: bool, checkpoint_before: str | None, checkpoint_after: str | None) -> ScenarioStep:
        # Only the controlled `semantic_action` text is scanned for
        # prohibited intent -- never the goal's free-text title, which
        # legitimately discusses subjects like "permission" as an
        # investigation topic rather than an instruction to act on one
        # (the same false-positive class already fixed in
        # `scenario_risk_analyzer.py`; see docs/SCENARIO_PLANNING_ENGINE.md's
        # "Live verification findings").
        safety = _safety_class(mutation_type, semantic_action)
        confidence_sources = [r.confidence for r in (ctx.primary_actor, ctx.primary_workflow, ctx.primary_entity) if r is not None]
        confidence = (sum(confidence_sources) / len(confidence_sources)) if confidence_sources else 0.3
        source_nodes = [n for r in (ctx.primary_actor, ctx.primary_workflow, ctx.primary_entity, ctx.primary_output) if r is not None for n in r.supporting_graph_nodes]
        return ScenarioStep(
            step_id=f"{scenario_id}:step:{seq_index}:{step_type}", scenario_id=scenario_id, sequence_index=seq_index,
            step_type=step_type, title=title, description=title, semantic_action=semantic_action,
            actor_id=ctx.primary_actor.actor_id if ctx.primary_actor else "",
            actor_requirement_id=ctx.primary_actor.requirement_id if ctx.primary_actor else "",
            entity_ids=[ctx.primary_entity.entity_id] if ctx.primary_entity and ctx.primary_entity.entity_id else [],
            workflow_id=ctx.primary_workflow.workflow_id if ctx.primary_workflow else "",
            permission_id=ctx.primary_permission.permission_id if ctx.primary_permission else "",
            source_state_id=source_state if step_type in {"establish_baseline", "trigger_transition"} else "",
            target_state_id=target_state if step_type in {"trigger_transition", "verify_state"} else "",
            preconditions=[cp for cp in [checkpoint_before] if cp], postconditions=[cp for cp in [checkpoint_after] if cp],
            safety_class=safety, mutation_type=mutation_type, reversibility=_MUTATION_TO_REVERSIBILITY.get(mutation_type, "unknown"),
            optional=optional, blocking=not optional,
            depends_on_step_ids=[steps[-1].step_id] if steps else [],
            confidence=confidence, source_graph_nodes=sorted(set(source_nodes)),
        )

    seq = 0
    for blueprint in template.steps:
        if blueprint.repeats_per_workflow_step and ctx.primary_workflow is not None and ctx.primary_workflow.required_steps:
            for step_name in ctx.primary_workflow.required_steps:
                steps.append(
                    make_step(
                        seq_index=seq, step_type=blueprint.step_type, title=f"Perform workflow step: {step_name}",
                        semantic_action=f"{blueprint.semantic_action} ({step_name})", mutation_type=blueprint.mutation_type,
                        optional=blueprint.optional, checkpoint_before=None, checkpoint_after=None,
                    )
                )
                seq += 1
            continue

        cp_before = add_checkpoint(blueprint.checkpoint_before) if blueprint.checkpoint_before else None
        step = make_step(
            seq_index=seq, step_type=blueprint.step_type, title=blueprint.semantic_action.capitalize(),
            semantic_action=blueprint.semantic_action, mutation_type=blueprint.mutation_type,
            optional=blueprint.optional, checkpoint_before=cp_before, checkpoint_after=None,
        )
        steps.append(step)
        seq += 1
        if blueprint.checkpoint_after:
            cp_after = add_checkpoint(blueprint.checkpoint_after)
            step.postconditions.append(cp_after)

    return steps, checkpoints


def build_preconditions_postconditions(ctx, *, mutating: bool) -> tuple[list[ScenarioPrecondition], list[ScenarioPostcondition]]:
    preconditions: list[ScenarioPrecondition] = []
    postconditions: list[ScenarioPostcondition] = []
    scenario_id = ctx.candidate.candidate_id

    for actor_req in ctx.actor_reqs:
        preconditions.append(
            ScenarioPrecondition(
                precondition_id=f"{scenario_id}:precondition:actor_session:{actor_req.canonical_name}",
                precondition_type="actor_session", description=f"An authenticated session for '{actor_req.canonical_name}' must be available.",
                referenced_object_id=actor_req.actor_id, status=actor_req.status, mandatory=True, confidence=actor_req.confidence,
            )
        )
    for perm_req in ctx.permission_reqs:
        preconditions.append(
            ScenarioPrecondition(
                precondition_id=f"{scenario_id}:precondition:permission:{perm_req.permission_id or perm_req.requirement_id}",
                precondition_type="permission", description="The required permission must be resolvable (granted or denied, per the scenario's polarity).",
                referenced_object_id=perm_req.permission_id, status=perm_req.status, mandatory=True, confidence=perm_req.confidence,
            )
        )
    for state_req in ctx.state_reqs:
        preconditions.append(
            ScenarioPrecondition(
                precondition_id=f"{scenario_id}:precondition:entity_state:{state_req.state_label}",
                precondition_type="entity_state", description=f"An entity in state '{state_req.state_label}' must be available.",
                referenced_object_id=state_req.entity_id, status=state_req.status, mandatory=True, confidence=state_req.confidence,
            )
        )
    if mutating:
        preconditions.append(
            ScenarioPrecondition(
                precondition_id=f"{scenario_id}:precondition:test_data", precondition_type="test_data",
                description="Test data suitable for this mutation must be available or creatable.", status="unknown", mandatory=True, confidence=0.3,
            )
        )
    if ctx.primary_output is not None:
        preconditions.append(
            ScenarioPrecondition(
                precondition_id=f"{scenario_id}:precondition:output_baseline", precondition_type="output_baseline",
                description="The output must be observable before the triggering action.", referenced_object_id=ctx.primary_output.output_id,
                status=ctx.primary_output.status, mandatory=True, confidence=ctx.primary_output.confidence,
            )
        )

    if ctx.state_reqs:
        postconditions.append(
            ScenarioPostcondition(
                postcondition_id=f"{scenario_id}:postcondition:entity_state", postcondition_type="entity_state",
                description="The entity reaches the expected target state.", referenced_object_id=ctx.state_reqs[-1].entity_id,
                expected_value=ctx.state_reqs[-1].state_label, confidence=ctx.state_reqs[-1].confidence,
            )
        )
    if ctx.primary_output is not None:
        postconditions.append(
            ScenarioPostcondition(
                postcondition_id=f"{scenario_id}:postcondition:output_state", postcondition_type="output_state",
                description="The output reflects the expected effect of the triggering action.", referenced_object_id=ctx.primary_output.output_id,
                confidence=ctx.primary_output.confidence,
            )
        )
    if ctx.primary_workflow is not None:
        postconditions.append(
            ScenarioPostcondition(
                postcondition_id=f"{scenario_id}:postcondition:workflow_outcome", postcondition_type="workflow_outcome",
                description="The workflow reaches its recorded outcome.", referenced_object_id=ctx.primary_workflow.workflow_id,
                expected_value=ctx.primary_workflow.required_outcome, confidence=ctx.primary_workflow.confidence,
            )
        )
    postconditions.append(
        ScenarioPostcondition(
            postcondition_id=f"{scenario_id}:postcondition:evidence_captured", postcondition_type="evidence_captured",
            description="Sufficient evidence has been captured to support or contradict the goal.", confidence=0.5,
        )
    )
    return preconditions, postconditions


_MUTATING_STEP_TYPES = {"perform_workflow_step", "trigger_transition", "perform_operation"}


def to_observational(steps: list[ScenarioStep]) -> list[ScenarioStep]:
    """Transforms a mutating step sequence into a read-only one: instead of
    PERFORMING the action, locate an already-completed example of it and
    compare evidence. Without this transform, the "observational"
    alternative would be byte-for-byte identical to the primary scenario
    (same template, same steps) and get silently deduplicated away --
    exactly the bug this function exists to prevent."""
    transformed: list[ScenarioStep] = []
    for step in steps:
        if step.step_type in _MUTATING_STEP_TYPES:
            transformed.append(
                step.model_copy(update={
                    "step_type": "locate_subject",
                    "title": f"Locate an already-completed example ({step.title})",
                    "semantic_action": f"locate an already-completed example instead of performing: {step.semantic_action}",
                    "mutation_type": "none", "safety_class": "read_only", "reversibility": "reversible",
                })
            )
        else:
            transformed.append(step)
    return transformed


def to_reduced_scope(steps: list[ScenarioStep]) -> list[ScenarioStep]:
    """Keeps only the FIRST workflow-action step -- "verify one transition
    before attempting the full lifecycle." Re-sequences the survivors so
    downstream ordering checks stay consistent."""
    kept: list[ScenarioStep] = []
    seen_workflow_step = False
    for step in steps:
        if step.step_type == "perform_workflow_step":
            if seen_workflow_step:
                continue
            seen_workflow_step = True
        kept.append(step)
    for index, step in enumerate(kept):
        step.sequence_index = index
    return kept
