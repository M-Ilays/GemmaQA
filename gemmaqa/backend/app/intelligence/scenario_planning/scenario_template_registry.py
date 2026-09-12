"""Application-neutral scenario templates -- reusable SEMANTIC investigation
structures, never application-specific instructions. A template is an
ordered list of `StepBlueprint`s; `scenario_step_builder.py` expands each
blueprint into a concrete `ScenarioStep` using the goal's resolved
requirements. Extensible: add a new `ScenarioTemplate` and register it in
`TEMPLATES_BY_SCENARIO_TYPE` without touching anything else.

Not every scenario is forced through a template if the graph doesn't
support it -- `scenario_step_builder.py` skips or marks-optional any
blueprint step whose required requirement never resolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.intelligence.goal_generation.schemas import GOAL_TYPES
from app.intelligence.scenario_planning.schemas import SCENARIO_TYPES


@dataclass(frozen=True)
class StepBlueprint:
    step_type: str
    semantic_action: str
    mutation_type: str = "none"
    checkpoint_before: str | None = None
    checkpoint_after: str | None = None
    optional: bool = False
    # When True, the step builder expands this ONE blueprint into one
    # concrete step PER resolved workflow step (ordered) rather than a
    # single step -- used for the "perform the workflow" blueprint.
    repeats_per_workflow_step: bool = False


@dataclass(frozen=True)
class ScenarioTemplate:
    template_id: str
    scenario_type: str
    title: str
    objective: str
    steps: tuple[StepBlueprint, ...] = field(default_factory=tuple)


# -- goal_type -> scenario_type(s). A tuple means "ambiguous, resolve from
#    evidence at candidate-build time" (only verify_permission is genuinely
#    ambiguous -- everything else has exactly one natural mapping). ---------

GOAL_TYPE_TO_SCENARIO_TYPES: dict[str, tuple[str, ...]] = {
    "verify_workflow": ("workflow_verification",),
    "verify_dependency": ("dependency_verification",),
    "verify_kpi": ("metric_verification",),
    "verify_permission": ("permission_positive_verification", "permission_negative_verification"),
    "verify_actor_capability": ("actor_capability_verification",),
    "verify_state_transition": ("state_transition_verification",),
    "verify_entity_lifecycle": ("entity_lifecycle_verification",),
    "verify_business_rule": ("business_rule_verification",),
    "resolve_contradiction": ("contradiction_resolution",),
    "resolve_graph_gap": ("graph_gap_resolution",),
    "resolve_unresolved_reference": ("unresolved_reference_resolution",),
    "increase_confidence": ("confidence_increase",),
    "validate_inferred_relationship": ("inferred_relationship_validation",),
    "validate_workflow_branch": ("branch_verification",),
    "validate_prerequisite": ("prerequisite_verification",),
    "validate_dashboard_output": ("output_verification",),
    "validate_report": ("report_verification",),
    "validate_notification": ("notification_verification",),
    "validate_aggregation_rule": ("aggregation_verification",),
    "validate_actor_hand_off": ("actor_handoff_verification",),
    "validate_ownership": ("ownership_verification",),
    "validate_scope": ("scope_verification",),
}

assert set(GOAL_TYPE_TO_SCENARIO_TYPES) == GOAL_TYPES, "every goal type must map to at least one scenario type"


def scenario_types_for_goal_type(goal_type: str) -> tuple[str, ...]:
    return GOAL_TYPE_TO_SCENARIO_TYPES.get(goal_type, ("unknown",))


# -- templates ----------------------------------------------------------------

_GENERIC_OBSERVATION = ScenarioTemplate(
    template_id="generic_observation",
    scenario_type="unknown",
    title="Observe subject and capture evidence",
    objective="Gather enough evidence to resolve the current uncertainty without mutating application state.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the required actor"),
        StepBlueprint("navigate", "navigate to the subject"),
        StepBlueprint("observe", "observe the subject's current state"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_WORKFLOW_VERIFICATION = ScenarioTemplate(
    template_id="workflow_verification",
    scenario_type="workflow_verification",
    title="Verify workflow",
    objective="Confirm the workflow's steps, actor(s), and outcome match what the graph currently records.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the workflow's actor"),
        StepBlueprint("navigate", "navigate to the workflow's entry point"),
        StepBlueprint("locate_subject", "locate the subject entity"),
        StepBlueprint("establish_baseline", "record the entity's starting state", checkpoint_before="pre_transition_state"),
        StepBlueprint("perform_workflow_step", "perform the workflow's ordered steps", mutation_type="transition", repeats_per_workflow_step=True),
        StepBlueprint("verify_state", "verify the resulting entity state", checkpoint_after="post_transition_state"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_BEFORE_AFTER = ScenarioTemplate(
    template_id="before_after_dependency",
    scenario_type="dependency_verification",
    title="Verify dependency via before/after comparison",
    objective="Establish a baseline, trigger the source transition, and compare the output before and after.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the required actor"),
        StepBlueprint("navigate", "navigate to the source entity"),
        StepBlueprint("locate_subject", "locate the source entity"),
        StepBlueprint("observe_output", "observe the output baseline", checkpoint_before="output_before"),
        StepBlueprint("perform_workflow_step", "perform the source transition", mutation_type="transition"),
        StepBlueprint("verify_state", "verify the resulting entity state", checkpoint_after="post_transition_state"),
        StepBlueprint("wait_for_effect", "allow for a possible delayed effect", optional=True),
        StepBlueprint("observe_output", "observe the output again", checkpoint_after="output_after"),
        StepBlueprint("compare", "compare the baseline and final output"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_METRIC_VERIFICATION = ScenarioTemplate(
    template_id="metric_verification",
    scenario_type="metric_verification",
    title="Verify metric via before/after comparison",
    objective=_BEFORE_AFTER.objective,
    steps=_BEFORE_AFTER.steps,
)

_OUTPUT_OBSERVATION = ScenarioTemplate(
    template_id="output_observation",
    scenario_type="output_verification",
    title="Verify output",
    objective="Observe the output directly and capture evidence of its current state.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the required actor"),
        StepBlueprint("navigate", "navigate to the output's location"),
        StepBlueprint("observe_output", "observe the output", checkpoint_after="output_after"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_PERMISSION_POSITIVE = ScenarioTemplate(
    template_id="permission_positive",
    scenario_type="permission_positive_verification",
    title="Verify permission is granted",
    objective="Confirm the actor can access and safely perform the operation.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the permitted actor"),
        StepBlueprint("navigate", "navigate to the operation's entry point"),
        StepBlueprint("verify_permission", "confirm the control is available"),
        StepBlueprint("perform_operation", "attempt the operation safely", mutation_type="update"),
        StepBlueprint("verify_state", "observe acceptance of the operation"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_PERMISSION_NEGATIVE = ScenarioTemplate(
    template_id="permission_negative",
    scenario_type="permission_negative_verification",
    title="Verify permission is denied",
    objective="Confirm the restricted actor cannot perform the operation and no state mutation occurs.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the restricted actor"),
        StepBlueprint("navigate", "navigate to the operation's entry point"),
        StepBlueprint("verify_denial", "attempt the operation safely and observe the denial"),
        StepBlueprint("verify_state", "confirm no state mutation occurred"),
        StepBlueprint("capture_evidence", "capture denial evidence"),
    ),
)

_ACTOR_CAPABILITY = ScenarioTemplate(
    template_id="actor_capability",
    scenario_type="actor_capability_verification",
    title="Verify actor capability",
    objective="Confirm which operations this actor can and cannot perform.",
    steps=_PERMISSION_POSITIVE.steps,
)

_STATE_TRANSITION = ScenarioTemplate(
    template_id="state_transition",
    scenario_type="state_transition_verification",
    title="Verify state transition",
    objective="Confirm the entity transitions from the expected source state to the expected target state.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the required actor"),
        StepBlueprint("navigate", "navigate to the subject entity"),
        StepBlueprint("locate_subject", "locate the subject entity"),
        StepBlueprint("establish_baseline", "record the entity's source state", checkpoint_before="pre_transition_state"),
        StepBlueprint("trigger_transition", "trigger the transition", mutation_type="transition"),
        StepBlueprint("verify_state", "verify the entity's target state", checkpoint_after="post_transition_state"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_ENTITY_LIFECYCLE = ScenarioTemplate(
    template_id="entity_lifecycle",
    scenario_type="entity_lifecycle_verification",
    title="Verify entity lifecycle",
    objective="Confirm the entity's known states, relationships, and actor associations match the graph.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the required actor"),
        StepBlueprint("navigate", "navigate to the entity list"),
        StepBlueprint("locate_subject", "locate the subject entity"),
        StepBlueprint("observe", "observe the entity's current lifecycle state"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_BUSINESS_RULE = ScenarioTemplate(
    template_id="business_rule",
    scenario_type="business_rule_verification",
    title="Verify business rule",
    objective="Confirm the structural relationship between the two entities holds as recorded.",
    steps=_ENTITY_LIFECYCLE.steps,
)

_PREREQUISITE = ScenarioTemplate(
    template_id="prerequisite",
    scenario_type="prerequisite_verification",
    title="Verify prerequisite",
    objective="Confirm whether the prerequisite is currently satisfied.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the required actor"),
        StepBlueprint("navigate", "navigate to the prerequisite's context"),
        StepBlueprint("locate_subject", "locate the subject entity"),
        StepBlueprint("verify_state", "verify whether the prerequisite is satisfied"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_BRANCH = ScenarioTemplate(
    template_id="branch",
    scenario_type="branch_verification",
    title="Verify workflow branch",
    objective="Confirm the branch's known outcomes are each reachable and distinguishable.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the required actor"),
        StepBlueprint("navigate", "navigate to the decision point"),
        StepBlueprint("locate_subject", "locate the subject entity"),
        StepBlueprint("establish_baseline", "record the entity's state before the decision", checkpoint_before="pre_transition_state"),
        StepBlueprint("perform_workflow_step", "reach the decision point", mutation_type="transition"),
        StepBlueprint("verify_state", "verify which branch outcome occurred", checkpoint_after="post_transition_state"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_ACTOR_HANDOFF = ScenarioTemplate(
    template_id="actor_handoff",
    scenario_type="actor_handoff_verification",
    title="Verify actor hand-off",
    objective="Confirm the workflow correctly hands off from one actor to the next.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the first actor"),
        StepBlueprint("perform_workflow_step", "perform the first actor's step", mutation_type="transition"),
        StepBlueprint("switch_actor", "represent the hand-off to the second actor"),
        StepBlueprint("establish_session", "authenticate as the second actor"),
        StepBlueprint("perform_workflow_step", "perform the second actor's step", mutation_type="transition"),
        StepBlueprint("verify_state", "verify the hand-off completed correctly"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_OWNERSHIP = ScenarioTemplate(
    template_id="ownership",
    scenario_type="ownership_verification",
    title="Verify ownership",
    objective="Confirm the ownership relationship between the actor and the entity holds.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the required actor"),
        StepBlueprint("navigate", "navigate to the entity"),
        StepBlueprint("locate_subject", "locate the subject entity"),
        StepBlueprint("verify_state", "verify the ownership relationship"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_SCOPE = ScenarioTemplate(
    template_id="scope",
    scenario_type="scope_verification",
    title="Verify scope",
    objective="Confirm the output/relationship is scoped as recorded and comparisons stay within compatible scope.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the required actor"),
        StepBlueprint("resolve_scope", "resolve the applicable scope"),
        StepBlueprint("navigate", "navigate to the scoped subject"),
        StepBlueprint("observe_output", "observe the scoped output"),
        StepBlueprint("compare", "compare within compatible scope"),
        StepBlueprint("capture_evidence", "capture supporting evidence"),
    ),
)

_AGGREGATION = ScenarioTemplate(
    template_id="aggregation",
    scenario_type="aggregation_verification",
    title="Verify aggregation rule",
    objective="Confirm the aggregated output reflects its known source records.",
    steps=_OUTPUT_OBSERVATION.steps,
)

_NOTIFICATION = ScenarioTemplate(
    template_id="notification",
    scenario_type="notification_verification",
    title="Verify notification",
    objective="Confirm the notification appears as recorded.",
    steps=_OUTPUT_OBSERVATION.steps,
)

_REPORT = ScenarioTemplate(
    template_id="report",
    scenario_type="report_verification",
    title="Verify report",
    objective="Confirm the report reflects its known source records.",
    steps=_OUTPUT_OBSERVATION.steps,
)

_CONTRADICTION_RESOLUTION = ScenarioTemplate(
    template_id="contradiction_resolution",
    scenario_type="contradiction_resolution",
    title="Resolve contradictory graph evidence",
    objective="Design one observation that distinguishes between the two conflicting claims.",
    steps=(
        StepBlueprint("establish_session", "authenticate as the required actor"),
        StepBlueprint("navigate", "navigate to the contested subject"),
        StepBlueprint("locate_subject", "locate the subject"),
        StepBlueprint("observe", "make the distinguishing observation"),
        StepBlueprint("compare", "compare against both conflicting claims"),
        StepBlueprint("capture_evidence", "capture evidence for both possible outcomes"),
    ),
)

TEMPLATES_BY_SCENARIO_TYPE: dict[str, ScenarioTemplate] = {
    t.scenario_type: t
    for t in (
        _WORKFLOW_VERIFICATION, _BEFORE_AFTER, _METRIC_VERIFICATION, _OUTPUT_OBSERVATION,
        _PERMISSION_POSITIVE, _PERMISSION_NEGATIVE, _ACTOR_CAPABILITY, _STATE_TRANSITION,
        _ENTITY_LIFECYCLE, _BUSINESS_RULE, _PREREQUISITE, _BRANCH, _ACTOR_HANDOFF, _OWNERSHIP,
        _SCOPE, _AGGREGATION, _NOTIFICATION, _REPORT, _CONTRADICTION_RESOLUTION,
    )
}
# dependency_verification's PRIMARY template is the before/after one, keyed
# under its own scenario_type too (both `_BEFORE_AFTER` and
# `_METRIC_VERIFICATION` describe the same shape for two scenario types).
TEMPLATES_BY_SCENARIO_TYPE["dependency_verification"] = _BEFORE_AFTER

# Every remaining scenario type not covered by a dedicated template above
# (graph_gap_resolution, inferred_relationship_validation,
# confidence_increase, unresolved_reference_resolution,
# stale_knowledge_refresh, exploratory_observation, unknown) uses the
# generic observation template -- these are fundamentally "go observe X
# and capture evidence," not multi-step workflows.
for _scenario_type in SCENARIO_TYPES - set(TEMPLATES_BY_SCENARIO_TYPE):
    TEMPLATES_BY_SCENARIO_TYPE[_scenario_type] = _GENERIC_OBSERVATION


def template_for_scenario_type(scenario_type: str) -> ScenarioTemplate:
    return TEMPLATES_BY_SCENARIO_TYPE.get(scenario_type, _GENERIC_OBSERVATION)
