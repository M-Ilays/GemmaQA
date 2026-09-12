"""Workflow step extraction — action semantics + turning raw
`WorkflowCandidate`s (plus, when available, the action actually executed and
the transitions detected around it) into concrete `WorkflowStep` objects.

Action semantics reuse and EXTEND entity/actor discovery's generic verb
lexicon (`app.intelligence.entity_discovery.entity_candidate_builder.
OPERATION_VERBS`) rather than forking it — workflow steps additionally need
verbs entity/permission inference deliberately treats as non-entity chrome
(submit/cancel/complete ARE real workflow-progression semantics) plus a
handful entity discovery never needed at all (approve/reject/escalate/...).
Unknown verbs are preserved as their own semantic_action rather than
discarded — the task requires supporting unknown actions, not a closed list.

Status is never optimistic: a structurally-present control (a button that
exists on the page) is `unverified` until the SAME control is the one an
action actually targeted; only then — and only if that action succeeded —
does the step become `observed`.
"""

from __future__ import annotations

from app.intelligence.entity_discovery.entity_candidate_builder import OPERATION_VERBS
from app.intelligence.workflow_discovery.schemas import WorkflowCandidate, WorkflowEvidence, WorkflowStep
from app.intelligence.workflow_discovery.state_transition_detector import DetectedTransitions

# Verbs entity discovery doesn't need (either excluded there as form chrome,
# or never relevant to entity CRUD) but ARE first-class workflow actions.
WORKFLOW_EXTRA_VERBS: dict[str, str] = {
    "accept": "accept",
    "decline": "decline",
    "verify": "verify",
    "validate": "verify",
    "acknowledge": "acknowledge",
    "ack": "acknowledge",
    "publish": "publish",
    "release": "publish",
    "schedule": "schedule",
    "dispatch": "dispatch",
    "transfer": "transfer",
    "escalate": "escalate",
    "resolve": "resolve",
    "pay": "pay",
    "refund": "refund",
    "notify": "notify",
    "unassign": "unassign",
    "reassign": "transfer",
    "complete": "complete",
    "finish": "complete",
    "done": "complete",
    "reopen": "reopen",
    "close": "complete",
    "next": "continue",
    "continue": "continue",
    "proceed": "continue",
}

# candidate_type -> fallback semantic_action when the candidate carries no
# explicit action_verb of its own (e.g. a status control's own label rarely
# names a verb; its TYPE already implies one).
CANDIDATE_TYPE_DEFAULT_ACTION: dict[str, str] = {
    "create_form": "create",
    "edit_form": "edit",
    "status_control": "update",
    "assignment_control": "assign",
    "approval_control": "approve",
    "rejection_control": "reject",
    "confirmation_dialog": "confirm",
    "multi_step_form": "continue",
    "list_to_detail_navigation": "view",
    "actor_queue": "view",
    "entity_cross_reference": "",
}

CONTROL_ID_FIELDS = ("control_id", "form_id", "dialog_id")


def classify_semantic_action(action_verb: str, candidate_type: str = "") -> str:
    """Never discards an unrecognized verb — an unknown action is preserved
    verbatim (lowercased) as its own semantic_action rather than dropped,
    per the task's explicit "support unknown semantic actions" requirement."""
    verb = (action_verb or "").strip().lower()
    if verb in OPERATION_VERBS:
        return OPERATION_VERBS[verb]
    if verb in WORKFLOW_EXTRA_VERBS:
        return WORKFLOW_EXTRA_VERBS[verb]
    if verb:
        return verb  # unknown but preserved
    return CANDIDATE_TYPE_DEFAULT_ACTION.get(candidate_type, "")


def _candidate_control_id(candidate: WorkflowCandidate) -> str | None:
    return candidate.evidence.element_id


class WorkflowStepExtractor:
    def extract(
        self,
        candidates: list[WorkflowCandidate],
        *,
        page_id: str,
        page_state_fingerprint: str,
        sequence_hint: int,
        executed_element_id: str | None = None,
        executed_action_succeeded: bool = True,
        transitions: DetectedTransitions | None = None,
    ) -> list[WorkflowStep]:
        steps: list[WorkflowStep] = []
        for candidate in candidates:
            control_id = _candidate_control_id(candidate)
            semantic_action = classify_semantic_action(candidate.action_verb, candidate.candidate_type)

            if control_id is not None and executed_element_id is not None and control_id == executed_element_id:
                status = "observed" if executed_action_succeeded else "blocked"
                confidence = 0.75 if executed_action_succeeded else 0.4
            else:
                status = "unverified"
                confidence = 0.3

            evidence = [candidate.evidence]
            if status == "observed" and transitions is not None:
                evidence.extend(self._transition_evidence(transitions))
                confidence = min(1.0, confidence + 0.1 * len(transitions.signals[:3]))

            steps.append(
                WorkflowStep(
                    sequence_hint=sequence_hint,
                    action_verb=candidate.action_verb or semantic_action,
                    semantic_action=semantic_action,
                    entity_id=candidate.entity_id,
                    actor_id=candidate.actor_id,
                    page_id=page_id,
                    page_state_fingerprint=page_state_fingerprint,
                    control_id=control_id if candidate.candidate_type not in {"create_form", "edit_form", "multi_step_form"} else None,
                    form_id=control_id if candidate.candidate_type in {"create_form", "edit_form", "multi_step_form"} else None,
                    dialog_id=control_id if candidate.candidate_type == "confirmation_dialog" else None,
                    confidence=confidence,
                    evidence=evidence,
                    status=status,
                )
            )
        return steps

    @staticmethod
    def _transition_evidence(transitions: DetectedTransitions) -> list[WorkflowEvidence]:
        out: list[WorkflowEvidence] = []
        for signal in transitions.signals[:3]:
            out.append(
                WorkflowEvidence(
                    source_kind="before_after_page_state" if signal.kind not in {"entity_status_changed", "assignment_changed"}
                    else "before_after_entity_state",
                    observed_text=signal.description[:160],
                )
            )
        return out
