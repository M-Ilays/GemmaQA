"""Workflow memory — persistence and merging of discovered workflows across
observations.

Owns the anchor -> WorkflowDescriptor store (mirrors
`app.intelligence.entity_discovery.entity_memory` / `actor_memory`). A
workflow is anchored primarily by its PRIMARY ENTITY (all steps touching
the same entity type — create, review, approve, status-change — belong to
one lifecycle workflow, matching the task's own illustrative example),
falling back to a page-group anchor when no entity is known yet. This is
the deterministic identity that stops one workflow being duplicated merely
because it was observed on a different page or in a different session.
"""

from __future__ import annotations

from datetime import datetime

from app.intelligence.workflow_discovery import workflow_confidence
from app.intelligence.workflow_discovery.schemas import (
    WorkflowActorParticipation,
    WorkflowBranch,
    WorkflowDescriptor,
    WorkflowEntityParticipation,
    WorkflowEvidence,
    WorkflowOutcome,
    WorkflowPrerequisite,
    WorkflowState,
    WorkflowStep,
    WorkflowTransition,
    WorkflowTrigger,
)

MAX_EVIDENCE_PER_WORKFLOW = 80
# A step is "the same step" as an existing one when its control/form/dialog
# id AND semantic_action match — repeats merge in, they never duplicate.
_STATUS_STRENGTH = {
    "inferred": 0,
    "unverified": 1,
    "blocked": 2,
    "partially_observed": 3,
    "contradicted": 3,
    "observed": 4,
    "completed": 5,
}


def _dedupe_key(ev: WorkflowEvidence) -> tuple[str, str, str]:
    return (ev.source_kind, ev.observed_text, ev.page_url)


def _merge_evidence_list(existing: list[WorkflowEvidence], incoming: list[WorkflowEvidence], cap: int = MAX_EVIDENCE_PER_WORKFLOW) -> None:
    seen = {_dedupe_key(e) for e in existing}
    for ev in incoming:
        key = _dedupe_key(ev)
        if key in seen or len(existing) >= cap:
            continue
        seen.add(key)
        existing.append(ev)


def _step_identity(step: WorkflowStep) -> tuple[str, str]:
    control = step.control_id or step.form_id or step.dialog_id or ""
    return (control, step.semantic_action or step.action_verb)


class WorkflowMemory:
    def __init__(self) -> None:
        self.records: dict[str, WorkflowDescriptor] = {}

    def resolve_anchor(self, anchor: str) -> str | None:
        return anchor if anchor in self.records else None

    def get_or_create(self, anchor: str, *, canonical_name: str, now: datetime | None = None) -> WorkflowDescriptor:
        record = self.records.get(anchor)
        if record is None:
            now = now or datetime.utcnow()
            record = WorkflowDescriptor(canonical_name=canonical_name, first_seen=now, last_seen=now)
            self.records[anchor] = record
        return record

    # -- merge operations (never lose evidence) ------------------------------

    def merge_steps(self, descriptor: WorkflowDescriptor, steps: list[WorkflowStep]) -> list[str]:
        """Returns the ids of steps that are new-or-strengthened this round."""
        touched: list[str] = []
        by_identity = {_step_identity(s): s for s in descriptor.steps}
        for step in steps:
            identity = _step_identity(step)
            existing = by_identity.get(identity)
            if existing is None:
                descriptor.steps.append(step)
                by_identity[identity] = step
                touched.append(step.step_id)
                continue
            _merge_evidence_list(existing.evidence, step.evidence)
            if _STATUS_STRENGTH.get(step.status, 0) > _STATUS_STRENGTH.get(existing.status, 0):
                existing.status = step.status
                existing.confidence = max(existing.confidence, step.confidence)
                touched.append(existing.step_id)
            else:
                existing.confidence = max(existing.confidence, step.confidence * 0.5)
        return touched

    def merge_actor_participation(self, descriptor: WorkflowDescriptor, actor_id: str, *, role: str, step_id: str, evidence: list[WorkflowEvidence]) -> None:
        existing = next((p for p in descriptor.actors if p.actor_id == actor_id), None)
        if existing is None:
            descriptor.actors.append(
                WorkflowActorParticipation(actor_id=actor_id, role_in_workflow=role, step_ids=[step_id], evidence=list(evidence))
            )
            return
        if step_id not in existing.step_ids:
            existing.step_ids.append(step_id)
        _merge_evidence_list(existing.evidence, evidence)

    def merge_entity_participation(self, descriptor: WorkflowDescriptor, entity_id: str, *, role: str, step_id: str, evidence: list[WorkflowEvidence]) -> None:
        existing = next((p for p in descriptor.entities if p.entity_id == entity_id), None)
        if existing is None:
            descriptor.entities.append(
                WorkflowEntityParticipation(entity_id=entity_id, role_in_workflow=role, step_ids=[step_id], evidence=list(evidence))
            )
            return
        if step_id not in existing.step_ids:
            existing.step_ids.append(step_id)
        _merge_evidence_list(existing.evidence, evidence)

    def merge_transition(self, descriptor: WorkflowDescriptor, transition: WorkflowTransition) -> bool:
        existing = next(
            (t for t in descriptor.transitions if t.source_state == transition.source_state and t.target_state == transition.target_state and t.entity_id == transition.entity_id),
            None,
        )
        if existing is None:
            descriptor.transitions.append(transition)
            return True
        _merge_evidence_list(existing.evidence, transition.evidence)
        existing.confidence = min(1.0, existing.confidence + 0.05)
        return False

    def merge_state(self, descriptor: WorkflowDescriptor, state: WorkflowState) -> None:
        existing = next((s for s in descriptor.states if s.label == state.label), None)
        if existing is None:
            descriptor.states.append(state)
            return
        _merge_evidence_list(existing.evidence, state.evidence)
        existing.is_explicit = existing.is_explicit or state.is_explicit

    def merge_prerequisite(self, descriptor: WorkflowDescriptor, prereq: WorkflowPrerequisite) -> None:
        existing = next((p for p in descriptor.prerequisites if p.type == prereq.type and p.target == prereq.target), None)
        if existing is None:
            descriptor.prerequisites.append(prereq)
            return
        _merge_evidence_list(existing.evidence, prereq.evidence)
        if prereq.satisfied is not None:
            existing.satisfied = prereq.satisfied
            existing.blocking_reason = prereq.blocking_reason

    def merge_branch(self, descriptor: WorkflowDescriptor, branch: WorkflowBranch) -> None:
        existing = next((b for b in descriptor.branches if b.branch_type == branch.branch_type), None)
        if existing is None:
            descriptor.branches.append(branch)
            return
        _merge_evidence_list(existing.evidence, branch.evidence)
        for label in branch.option_labels:
            if label not in existing.option_labels:
                existing.option_labels.append(label)

    def merge_trigger(self, descriptor: WorkflowDescriptor, trigger: WorkflowTrigger) -> None:
        existing = next((t for t in descriptor.triggers if t.trigger_type == trigger.trigger_type), None)
        if existing is None:
            descriptor.triggers.append(trigger)
            return
        _merge_evidence_list(existing.evidence, trigger.evidence)

    def merge_outcome(self, descriptor: WorkflowDescriptor, outcome: WorkflowOutcome) -> None:
        existing = next((o for o in descriptor.outcomes if o.description == outcome.description), None)
        if existing is None:
            descriptor.outcomes.append(outcome)
            return
        _merge_evidence_list(existing.evidence, outcome.evidence)
        if outcome.produced_by_step_id and not existing.produced_by_step_id:
            existing.produced_by_step_id = outcome.produced_by_step_id

    # -- finalize -------------------------------------------------------------

    def finalize(self, descriptor: WorkflowDescriptor, *, page_url: str = "", now: datetime | None = None) -> None:
        """Recompute confidence/status/version from current state. Called
        once per observation after all merges for that observation."""
        now = now or datetime.utcnow()
        if page_url and page_url not in descriptor.source_pages:
            descriptor.source_pages.append(page_url)
        descriptor.source_states = sorted({s.label for s in descriptor.states})
        descriptor.source_elements = sorted(
            {s.control_id or s.form_id or s.dialog_id for s in descriptor.steps if (s.control_id or s.form_id or s.dialog_id)}
        )
        descriptor.confidence = workflow_confidence.score_confidence(descriptor)
        descriptor.status = workflow_confidence.status_for(descriptor)
        descriptor.last_seen = now
        descriptor.observation_count += 1
        descriptor.version += 1
