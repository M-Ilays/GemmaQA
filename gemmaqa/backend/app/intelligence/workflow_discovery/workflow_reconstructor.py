"""Workflow reconstruction — connects one observation's steps/transitions/
hand-offs/prerequisites/branches into (new-or-existing) `WorkflowDescriptor`s
in the registry.

Anchoring: a workflow is identified primarily by its PRIMARY ENTITY — every
step touching the same entity (create, review, approve, status-change)
belongs to ONE lifecycle workflow, matching the task's own illustrative
example. This is what stops the same workflow being duplicated merely
because it was observed on a different page or in a different session
(the entity anchor is stable across both). Steps with no known entity fall
back to a page-group anchor.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.intelligence.workflow_discovery.schemas import (
    WorkflowEvidence,
    WorkflowOutcome,
    WorkflowState,
    WorkflowStep,
    WorkflowTransition,
    WorkflowTrigger,
)
from app.intelligence.workflow_discovery.state_transition_detector import DetectedTransitions
from app.intelligence.workflow_discovery.workflow_registry import WorkflowRegistry
from app.intelligence.workflow_discovery.workflow_relationship_builder import ActorHandoffFinding

_ROLE_FOR_ACTION = {
    "approve": "approver",
    "reject": "approver",
    "verify": "reviewer",
    "assign": "assignee",
    "escalate": "reviewer",
}

_ENTRY_ACTIONS = {"create"}
_EXIT_ACTIONS = {"complete", "archive", "publish", "resolve"}


def _url_group(url: str) -> str:
    try:
        path = urlparse(url).path.strip("/")
    except Exception:
        return url
    return path.split("/", 1)[0] if path else url


def _anchor_for(step: WorkflowStep, fallback_page_url: str) -> str:
    if step.entity_id:
        return f"entity:{step.entity_id}"
    return f"page:{_url_group(fallback_page_url)}"


def _canonical_name_for(anchor: str, entity_id: str | None) -> str:
    if entity_id:
        return f"{entity_id} workflow"
    return f"{anchor.split(':', 1)[-1]} workflow"


class WorkflowReconstructor:
    def reconstruct(
        self,
        registry: WorkflowRegistry,
        *,
        steps: list[WorkflowStep],
        transitions: DetectedTransitions,
        handoffs: list[ActorHandoffFinding],
        prerequisites: list,
        branches: list,
        page_url: str,
        iteration: int,
        primary_entity_term: str | None = None,
    ) -> dict[str, Any]:
        touched_workflow_ids: set[str] = set()
        groups: dict[str, list[WorkflowStep]] = {}
        for step in steps:
            groups.setdefault(_anchor_for(step, page_url), []).append(step)
        if not groups and (transitions.any_detected() or prerequisites or branches):
            # No concrete steps this round, but something structurally
            # happened (a prerequisite, a transition) — still worth a
            # record rather than silently discarding the observation.
            # Entity-anchor when the entity is already known (e.g. a bare
            # authentication-prerequisite check on an otherwise-empty page
            # still belongs to that entity's workflow), page-anchor only
            # when no entity context exists at all.
            fallback_anchor = f"entity:{primary_entity_term}" if primary_entity_term else f"page:{_url_group(page_url)}"
            groups[fallback_anchor] = []

        for anchor, group_steps in groups.items():
            entity_id = group_steps[0].entity_id if group_steps else primary_entity_term
            descriptor = registry.memory.get_or_create(anchor, canonical_name=_canonical_name_for(anchor, entity_id))
            touched_workflow_ids.add(descriptor.workflow_id)

            new_step_ids = registry.memory.merge_steps(descriptor, group_steps)
            _merge_evidence_into_supporting(descriptor, group_steps)

            for step in group_steps:
                if step.actor_id:
                    role = _ROLE_FOR_ACTION.get(step.semantic_action, "initiator" if not descriptor.actors else "observer")
                    registry.memory.merge_actor_participation(
                        descriptor, step.actor_id, role=role, step_id=step.step_id, evidence=step.evidence
                    )
                if step.entity_id:
                    registry.memory.merge_entity_participation(
                        descriptor, step.entity_id, role="primary_subject", step_id=step.step_id, evidence=step.evidence
                    )
                if step.semantic_action in _ENTRY_ACTIONS and step.status == "observed" and page_url not in descriptor.known_entry_points:
                    descriptor.known_entry_points.append(page_url)
                    registry.memory.merge_trigger(
                        descriptor,
                        WorkflowTrigger(
                            trigger_type="form_submission" if step.form_id else "button_click",
                            description=f"{step.semantic_action} observed as a workflow entry point",
                            evidence=list(step.evidence), confidence=0.5,
                        ),
                    )
                if step.semantic_action in _EXIT_ACTIONS and step.status == "observed" and page_url not in descriptor.known_exit_points:
                    descriptor.known_exit_points.append(page_url)

            for handoff in handoffs:
                if handoff.to_actor_hint != "unknown_other_actor" and handoff.to_actor_hint not in {p.actor_id for p in descriptor.actors}:
                    registry.memory.merge_actor_participation(
                        descriptor, handoff.to_actor_hint, role="observer", step_id="", evidence=[handoff.evidence]
                    )

            self._merge_transitions(registry, descriptor, transitions, entity_id=entity_id, page_url=page_url, steps=group_steps)
            self._merge_outcomes(registry, descriptor, transitions, steps=group_steps, page_url=page_url)

            for prereq in prerequisites:
                registry.memory.merge_prerequisite(descriptor, prereq)
            for branch in branches:
                registry.memory.merge_branch(descriptor, branch)

            registry.memory.finalize(descriptor, page_url=page_url)

        return {"touched_workflow_ids": sorted(touched_workflow_ids), "groups": {k: len(v) for k, v in groups.items()}}

    # -- transitions: explicit (entity status) + structural fallback ----------

    @staticmethod
    def _merge_transitions(registry: WorkflowRegistry, descriptor, transitions: DetectedTransitions, *, entity_id, page_url, steps) -> None:
        observed_step = next((s for s in steps if s.status == "observed"), None)
        action_verb = observed_step.semantic_action if observed_step else ""

        explicit_found = False
        for signal in transitions.signals:
            if signal.kind != "entity_status_changed":
                continue
            explicit_found = True
            source_state, target_state = signal.before_value or "unknown", signal.after_value or "unknown"
            registry.memory.merge_state(descriptor, WorkflowState(label=source_state, is_explicit=True, source_kind="status_badge", confidence=0.6))
            registry.memory.merge_state(descriptor, WorkflowState(label=target_state, is_explicit=True, source_kind="status_badge", confidence=0.6))
            registry.memory.merge_transition(
                descriptor,
                WorkflowTransition(
                    entity_id=entity_id, source_state=source_state, target_state=target_state,
                    action_verb=action_verb, is_explicit=True, page_url=page_url,
                    confidence=signal.confidence,
                    evidence=[WorkflowEvidence(source_kind="before_after_entity_state", observed_text=signal.description[:160], page_url=page_url)],
                ),
            )

        if not explicit_found and observed_step is not None and transitions.any_detected():
            # No explicit label anywhere -> a structural placeholder, always
            # lower confidence, always marked non-explicit.
            source_label, target_label = f"state before {action_verb or 'action'}", f"state after {action_verb or 'action'}"
            registry.memory.merge_state(descriptor, WorkflowState(label=source_label, is_explicit=False, confidence=0.2))
            registry.memory.merge_state(descriptor, WorkflowState(label=target_label, is_explicit=False, confidence=0.2))
            registry.memory.merge_transition(
                descriptor,
                WorkflowTransition(
                    entity_id=entity_id, source_state=source_label, target_state=target_label,
                    action_verb=action_verb, is_explicit=False, page_url=page_url, confidence=0.25,
                    evidence=[WorkflowEvidence(source_kind="before_after_page_state", observed_text="structural transition, no explicit label", page_url=page_url)],
                ),
            )

    # -- outcomes ---------------------------------------------------------

    @staticmethod
    def _merge_outcomes(registry: WorkflowRegistry, descriptor, transitions: DetectedTransitions, *, steps, page_url) -> None:
        observed_step = next((s for s in steps if s.status == "observed"), None)
        for signal in transitions.signals:
            outcome_type = {
                "notification_appeared": "notification",
                "success_feedback": "state_change",
                "failure_feedback": "unknown",
                "row_appeared": "new_entity",
                "counter_changed": "dashboard_update",
            }.get(signal.kind)
            if outcome_type is None:
                continue
            registry.memory.merge_outcome(
                descriptor,
                WorkflowOutcome(
                    description=signal.description, outcome_type=outcome_type,
                    produced_by_step_id=observed_step.step_id if observed_step else None,
                    evidence=[WorkflowEvidence(source_kind="before_after_page_state", observed_text=signal.description[:160], page_url=page_url)],
                    confidence=signal.confidence,
                ),
            )
            if signal.kind == "success_feedback" and page_url not in descriptor.known_success_paths:
                descriptor.known_success_paths.append(page_url)
            if signal.kind == "failure_feedback" and page_url not in descriptor.known_failure_paths:
                descriptor.known_failure_paths.append(page_url)


def _merge_evidence_into_supporting(descriptor, steps: list[WorkflowStep]) -> None:
    seen = {(e.source_kind, e.observed_text, e.page_url) for e in descriptor.supporting_evidence}
    for step in steps:
        for ev in step.evidence:
            key = (ev.source_kind, ev.observed_text, ev.page_url)
            if key in seen:
                continue
            seen.add(key)
            descriptor.supporting_evidence.append(ev)
