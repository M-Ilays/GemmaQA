"""Cross-role hand-off detection, single-observation prerequisite discovery,
and branch discovery — the "connections between atomic observations" layer
(mirrors `app.intelligence.entity_discovery.entity_relationship_builder`'s
role, one level up).

Scope note: prerequisites/gaps that need the FULL accumulated registry
(e.g. "no creation step has ever been observed for this entity anywhere in
the run") are deliberately left to `workflow_gap_analyzer.py`, which runs
over the whole registry. This module only derives what a SINGLE observation
plus the already-computed Actor Registry can support — the same scope
discipline `entity_relationship_builder` already established.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.intelligence.workflow_discovery.schemas import (
    WorkflowBranch,
    WorkflowEvidence,
    WorkflowPrerequisite,
)
from app.intelligence.workflow_discovery.workflow_candidate_builder import PROGRESSION_VERBS
from app.intelligence.workflow_discovery.workflow_step_extractor import classify_semantic_action

if TYPE_CHECKING:
    from app.intelligence.actor_discovery import ActorRegistry
    from app.intelligence.workflow_discovery.schemas import WorkflowCandidate
    from app.perception.models import CanonicalPageModel

_OWNERSHIP_TEXT_RE = re.compile(
    r"\b(?:assigned to|submitted by|approved by|owned by|reviewed by|created by)\s*[:\-]?\s*([A-Za-z][\w .]{1,40})",
    re.IGNORECASE,
)

_DRAFT_VERBS = {"save"}
_SUBMIT_VERBS = {"submit"}
_COMPLETE_VERBS = {"complete"}
_CANCEL_VERBS = {"cancel"}
_RETRY_HINTS = ("retry", "try again")
_ABANDON_HINTS = ("abandon", "cancel", "give up")


class ActorHandoffFinding:
    def __init__(self, from_actor_term: str | None, to_actor_hint: str, evidence: WorkflowEvidence, confidence: float = 0.4) -> None:
        self.from_actor_term = from_actor_term
        self.to_actor_hint = to_actor_hint  # a resolvable actor term, or a generic "unknown_other_actor"
        self.evidence = evidence
        self.confidence = confidence


class WorkflowRelationshipBuilder:
    def build_handoffs(
        self,
        model: "CanonicalPageModel",
        candidates: list["WorkflowCandidate"],
        *,
        current_actor_term: str | None,
        actor_registry: "ActorRegistry | None" = None,
    ) -> list[ActorHandoffFinding]:
        findings: list[ActorHandoffFinding] = []
        known_actor_terms = {a.canonical_name for a in actor_registry.all_actors()} if actor_registry else set()

        # 1. Ownership/assignment TEXT patterns ("Assigned to: Jane", "Approved
        # by Manager") — resolve against known actors when possible.
        for block in (model.text_blocks or []):
            for match in _OWNERSHIP_TEXT_RE.finditer(block.text or ""):
                mentioned = match.group(1).strip().lower()
                hint = mentioned if mentioned in known_actor_terms else "unknown_other_actor"
                findings.append(
                    ActorHandoffFinding(
                        current_actor_term, hint,
                        WorkflowEvidence(source_kind="actor_handoff", observed_text=match.group(0)[:160], page_url=model.url),
                        confidence=0.6 if hint != "unknown_other_actor" else 0.35,
                    )
                )

        # 2. Assignment/approval controls whose OPTIONS name another actor.
        for candidate in candidates:
            if candidate.candidate_type not in {"assignment_control", "approval_control", "rejection_control"}:
                continue
            findings.append(
                ActorHandoffFinding(
                    current_actor_term, "unknown_other_actor",
                    WorkflowEvidence(
                        source_kind="actor_handoff",
                        observed_text=f"{candidate.candidate_type} implies another actor may act next",
                        page_url=model.url,
                    ),
                    confidence=0.3,
                )
            )

        # 3. Permission mismatch: an approval control is present, but the
        # CURRENT actor's own known permissions show no approve-ish grant —
        # strong evidence this step belongs to a DIFFERENT actor.
        if actor_registry is not None and current_actor_term:
            current = actor_registry.get(current_actor_term)
            has_approval_control = any(c.candidate_type == "approval_control" for c in candidates)
            if has_approval_control and current is not None:
                granted = set(current.granted_permission_ids())
                if not any(p.startswith("can_approve") for p in granted):
                    findings.append(
                        ActorHandoffFinding(
                            current_actor_term, "unknown_other_actor",
                            WorkflowEvidence(
                                source_kind="actor_permission",
                                observed_text="current actor has no observed approval permission",
                                page_url=model.url,
                            ),
                            confidence=0.5,
                        )
                    )
        return findings

    # -- prerequisites (single-observation scope) -----------------------------

    def build_prerequisites(
        self,
        model: "CanonicalPageModel",
        candidates: list["WorkflowCandidate"],
        *,
        authenticated: bool,
        current_actor_term: str | None,
        actor_registry: "ActorRegistry | None" = None,
        handoffs: list[ActorHandoffFinding] | None = None,
    ) -> list[WorkflowPrerequisite]:
        prerequisites: list[WorkflowPrerequisite] = []

        if not authenticated:
            prerequisites.append(
                WorkflowPrerequisite(
                    type="authentication", target="authenticated session",
                    evidence=[WorkflowEvidence(source_kind="prerequisite_check", observed_text="session is not authenticated", page_url=model.url)],
                    confidence=0.7, satisfied=False, blocking_reason="no authenticated session",
                )
            )

        for handoff in handoffs or []:
            prerequisites.append(
                WorkflowPrerequisite(
                    type="required_actor", target=handoff.to_actor_hint,
                    evidence=[handoff.evidence], confidence=handoff.confidence,
                    satisfied=(handoff.to_actor_hint != "unknown_other_actor"),
                    blocking_reason=None if handoff.to_actor_hint != "unknown_other_actor" else "no specific actor identified yet",
                )
            )

        # Approval/completion control co-present with an assignment control ->
        # "cannot approve/complete until assigned". Derived from the
        # CANDIDATES list (already merged from both before/after snapshots by
        # the engine) rather than re-querying `model.forms` directly — the
        # assignment field and the approval control routinely appear on
        # different sides of the same before/after pair (e.g. the field was
        # on the pre-action page), so re-scanning just one model missed it.
        assignment_candidates = [c for c in candidates if c.candidate_type == "assignment_control"]
        actionable_candidates = [c for c in candidates if c.candidate_type in {"approval_control", "rejection_control"}]
        if actionable_candidates and assignment_candidates:
            for assignment in assignment_candidates:
                prerequisites.append(
                    WorkflowPrerequisite(
                        type="required_assignment", target="an assignee",
                        evidence=[
                            WorkflowEvidence(
                                source_kind="prerequisite_check",
                                observed_text="approval control present alongside an assignment control",
                                page_url=model.url, element_id=assignment.evidence.element_id,
                            )
                        ],
                        confidence=0.4, satisfied=False, blocking_reason="assignment not confirmed yet",
                    )
                )
        return prerequisites

    # -- branches: mutually exclusive controls, never merged into one path ----

    def build_branches(self, model: "CanonicalPageModel", candidates: list["WorkflowCandidate"]) -> list[WorkflowBranch]:
        branches: list[WorkflowBranch] = []

        approvals = [c for c in candidates if c.candidate_type == "approval_control"]
        rejections = [c for c in candidates if c.candidate_type == "rejection_control"]
        if approvals and rejections:
            branches.append(
                self._branch("approve_vs_reject", approvals + rejections,
                              [c.evidence.observed_text for c in approvals + rejections])
            )

        progression_labels = {
            c.evidence.observed_text.strip().lower(): c
            for c in candidates
            if c.candidate_type == "progression_button"
        }
        verbs_present = {classify_semantic_action(c.action_verb, c.candidate_type) for c in progression_labels.values()}
        if "save" in verbs_present and "submit" in verbs_present:
            branches.append(self._branch("save_vs_submit", list(progression_labels.values()), sorted(verbs_present & {"save", "submit"})))
        if "complete" in verbs_present and "cancel" in verbs_present:
            branches.append(self._branch("complete_vs_cancel", list(progression_labels.values()), sorted(verbs_present & {"complete", "cancel"})))

        retry_present = any(any(h in (c.evidence.observed_text or "").lower() for h in _RETRY_HINTS) for c in candidates)
        abandon_present = any(any(h in (c.evidence.observed_text or "").lower() for h in _ABANDON_HINTS) for c in candidates)
        if retry_present and abandon_present:
            branches.append(self._branch("retry_vs_abandon", candidates, ["retry", "abandon"]))

        for table in model.tables or []:
            headers = [h or "" for h in (table.headers or [])]
            if any(h.lower() in {"status", "state"} for h in headers):
                values = {
                    str(row[headers.index(next(h for h in headers if h.lower() in {"status", "state"}))]).strip().lower()
                    for row in (table.sample_rows or [])
                    if row
                }
                if {"active", "archived"} <= values:
                    branches.append(self._branch("active_vs_archived", [], ["active", "archived"], table_id=table.table_id))
                if {"assigned", "unassigned"} <= values:
                    branches.append(self._branch("assigned_vs_unassigned", [], ["assigned", "unassigned"], table_id=table.table_id))
        return branches

    @staticmethod
    def _branch(branch_type: str, candidates: list, option_labels: list[str], *, table_id: str | None = None) -> WorkflowBranch:
        evidence = [c.evidence for c in candidates if hasattr(c, "evidence")]
        if table_id:
            evidence.append(WorkflowEvidence(source_kind="branch_control", observed_text=f"table {table_id} status column", element_id=table_id))
        return WorkflowBranch(
            description=f"{branch_type.replace('_vs_', ' vs ')} decision point",
            branch_type=branch_type, option_labels=option_labels, evidence=evidence, confidence=0.5,
        )
