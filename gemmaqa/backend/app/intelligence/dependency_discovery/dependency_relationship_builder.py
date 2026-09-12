"""Dependency relationship reasoning — effect-direction inference (task
section 7) and cross-role producer/processor/consumer reasoning (task
section 10), run per observation, merged into descriptors later.

Effect direction is inferred STRUCTURALLY from already-reconstructed
Workflow Registry data (steps' `semantic_action`, transitions'
`source_state`/`target_state`) — never re-derived from scratch, and always
a CANDIDATE (`DependencyEffect.confidence` stays low) unless a before/after
correlation later confirms it (see `dependency_correlator.py`).

Cross-role reasoning never switches actors automatically. It only produces
`VerificationRequirement` PLANS — ordered steps a human or a future,
separately-scoped milestone would execute, never something this engine runs
itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term
from app.intelligence.dependency_discovery.schemas import DependencyCandidate, DependencyEffect, VerificationRequirement

if TYPE_CHECKING:
    from app.intelligence.actor_discovery import ActorRegistry
    from app.intelligence.workflow_discovery import WorkflowRegistry

_VERB_EFFECT_MAP: dict[str, str] = {
    "create": "increase",
    "delete": "decrease",
    "archive": "decrease",
    "restore": "increase",
    "assign": "move_between_categories",
    "unassign": "move_between_categories",
    "reassign": "move_between_categories",
    "approve": "move_between_categories",
    "reject": "move_between_categories",
    "accept": "move_between_categories",
    "decline": "move_between_categories",
    "acknowledge": "decrease",
    "complete": "move_between_categories",
    "resolve": "move_between_categories",
    "cancel": "move_between_categories",
    "reopen": "move_between_categories",
    "publish": "activate",
    "transfer": "move_between_categories",
    "escalate": "unknown",
    "dispatch": "unknown",
    "pay": "decrease",
    "refund": "increase",
}

_PRODUCER_ROLES = ("initiator",)
_PROCESSOR_ROLES = ("approver", "assignee", "reviewer")


@dataclass
class CrossRoleFinding:
    producer_actor_term: str | None = None
    processor_actor_term: str | None = None
    consumer_actor_term: str | None = None
    workflow_term: str | None = None
    verification_requirements: list[VerificationRequirement] = field(default_factory=list)
    unresolved_actor: bool = False


class DependencyRelationshipBuilder:
    # -- effect direction -----------------------------------------------------

    def infer_effect(self, candidate: DependencyCandidate, *, workflow_registry: "WorkflowRegistry | None" = None) -> DependencyEffect:
        if workflow_registry is None:
            return DependencyEffect()
        if candidate.source.state_label:
            return self._effect_from_state(candidate, workflow_registry)
        entity_id = candidate.source.entity_id
        if entity_id:
            return self._effect_from_entity_lifecycle(entity_id, workflow_registry)
        workflow_id = candidate.source.workflow_id
        if workflow_id:
            return self._effect_from_workflow_steps(workflow_id, workflow_registry)
        return DependencyEffect()

    @staticmethod
    def _effect_from_workflow_steps(workflow_term: str, workflow_registry) -> DependencyEffect:
        workflows = [w for w in workflow_registry.all_workflows() if w.canonical_name == workflow_term]
        observed_verbs = {s.semantic_action for wf in workflows for s in wf.steps if s.status == "observed"}
        for verb, direction in _VERB_EFFECT_MAP.items():
            if verb in observed_verbs:
                return DependencyEffect(direction=direction, trigger_semantic_action=verb, confidence=0.25)
        all_verbs = {s.semantic_action for wf in workflows for s in wf.steps}
        for verb, direction in _VERB_EFFECT_MAP.items():
            if verb in all_verbs:
                return DependencyEffect(direction=direction, trigger_semantic_action=verb, confidence=0.12)
        return DependencyEffect()

    @staticmethod
    def _effect_from_state(candidate: DependencyCandidate, workflow_registry) -> DependencyEffect:
        entity_id = candidate.source.entity_id
        state_term = normalize_term(candidate.source.state_label or "")
        workflows = workflow_registry.workflows_by_entity(entity_id) if entity_id else workflow_registry.all_workflows()
        enters, leaves = False, False
        verb = ""
        for wf in workflows:
            for t in wf.transitions:
                if normalize_term(t.target_state) == state_term:
                    enters = True
                    verb = t.action_verb or verb
                if normalize_term(t.source_state) == state_term:
                    leaves = True
                    verb = t.action_verb or verb
        if enters and leaves:
            direction = "unknown"
        elif enters:
            direction = "increase"
        elif leaves:
            direction = "decrease"
        else:
            direction = "unknown"
        return DependencyEffect(direction=direction, trigger_semantic_action=verb, confidence=0.25 if direction != "unknown" else 0.1)

    @staticmethod
    def _effect_from_entity_lifecycle(entity_id: str, workflow_registry) -> DependencyEffect:
        workflows = workflow_registry.workflows_by_entity(entity_id)
        observed_verbs = {s.semantic_action for wf in workflows for s in wf.steps if s.status == "observed"}
        for verb, direction in _VERB_EFFECT_MAP.items():
            if verb in observed_verbs:
                return DependencyEffect(direction=direction, trigger_semantic_action=verb, confidence=0.3)
        all_verbs = {s.semantic_action for wf in workflows for s in wf.steps}
        for verb, direction in _VERB_EFFECT_MAP.items():
            if verb in all_verbs:
                return DependencyEffect(direction=direction, trigger_semantic_action=verb, confidence=0.15)
        return DependencyEffect()

    # -- cross-role reasoning -------------------------------------------------

    def build_cross_role_finding(
        self,
        candidate: DependencyCandidate,
        *,
        consumer_actor_term: str | None,
        workflow_registry: "WorkflowRegistry | None" = None,
        actor_registry: "ActorRegistry | None" = None,
    ) -> CrossRoleFinding | None:
        if workflow_registry is None:
            return None
        workflow_term = candidate.source.workflow_id
        entity_id = candidate.source.entity_id
        workflows = []
        if workflow_term:
            workflows = [w for w in workflow_registry.all_workflows() if w.canonical_name == workflow_term]
        elif entity_id:
            workflows = workflow_registry.workflows_by_entity(entity_id)
        if not workflows:
            return None

        producer, processor = None, None
        for wf in workflows:
            for participation in wf.actors:
                if participation.role_in_workflow in _PRODUCER_ROLES and producer is None:
                    producer = participation.actor_id
                if participation.role_in_workflow in _PROCESSOR_ROLES and processor is None:
                    processor = participation.actor_id

        wf_term = workflows[0].canonical_name
        if producer is None:
            return CrossRoleFinding(workflow_term=wf_term, consumer_actor_term=consumer_actor_term, unresolved_actor=True)

        if consumer_actor_term is None or producer == consumer_actor_term:
            if processor is None or processor == producer:
                return None  # same actor throughout -> not cross-role

        requirements: list[VerificationRequirement] = []
        ordinal = 0
        requirements.append(
            VerificationRequirement(step_type="login_as_actor", description=f"Login as '{producer}' to create the source record", actor_term=producer, entity_term=entity_id, ordinal=ordinal)
        )
        ordinal += 1
        requirements.append(VerificationRequirement(step_type="create_source_record", description="Create or transition the source record", entity_term=entity_id, ordinal=ordinal))
        ordinal += 1
        if processor and processor != producer:
            requirements.append(
                VerificationRequirement(step_type="login_as_actor", description=f"Login as '{processor}' to perform the transition", actor_term=processor, entity_term=entity_id, ordinal=ordinal)
            )
            ordinal += 1
            requirements.append(VerificationRequirement(step_type="perform_transition", description="Perform the state-changing action", entity_term=entity_id, ordinal=ordinal))
            ordinal += 1
        if consumer_actor_term:
            requirements.append(
                VerificationRequirement(step_type="return_as_actor", description=f"Return as '{consumer_actor_term}' to observe the output", actor_term=consumer_actor_term, ordinal=ordinal)
            )
            ordinal += 1
        requirements.append(VerificationRequirement(step_type="compare_output", description="Compare the output before and after", ordinal=ordinal))

        return CrossRoleFinding(
            producer_actor_term=producer, processor_actor_term=processor, consumer_actor_term=consumer_actor_term,
            workflow_term=wf_term, verification_requirements=requirements, unresolved_actor=False,
        )
