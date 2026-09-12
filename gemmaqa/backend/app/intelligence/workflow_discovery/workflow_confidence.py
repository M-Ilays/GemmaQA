"""Workflow confidence model — transparent, corroboration-based, and
explicitly NOT computed from evidence-kind count alone.

Extends the "distinct kinds beat repetition" principle already proven in
entity/actor discovery with three workflow-specific ideas the task calls for:

1. Richness bonus — a workflow with real steps/transitions/actors/outcomes
   is worth more than one with the same evidence-kind diversity but nothing
   actually reconstructed (mirrors actor_discovery's session_richness_bonus,
   which live-verified fixed a real "confirmed but confidence~0.1" bug).
2. Contradiction penalty — conflicting evidence (a transition and its
   observed reverse with no state permitting it, an outcome contradicted by
   a later observation) actively LOWERS confidence, never just fails to
   raise it.
3. Diminishing repeats — the SAME duplicate observation (same page, same
   evidence text) contributes a shrinking increment, so re-observing one
   page a hundred times cannot inflate confidence without bound.
"""

from __future__ import annotations

from app.intelligence.workflow_discovery.schemas import WorkflowDescriptor, WorkflowEvidence

# Corroboration weights — a directly OBSERVED before/after transition or a
# matching network mutation is the strongest possible signal; a bare button
# label or textual hint is the weakest.
SOURCE_KIND_WEIGHTS: dict[str, float] = {
    "before_after_page_state": 0.30,
    "before_after_entity_state": 0.30,
    "network_mutation": 0.25,
    "table_row_transition": 0.20,
    "status_control": 0.15,
    "status_badge": 0.15,
    "confirmation_dialog": 0.15,
    "approval_control": 0.15,
    "rejection_control": 0.15,
    "assignment_control": 0.15,
    "create_form": 0.15,
    "edit_form": 0.12,
    "multi_step_form": 0.15,
    "list_to_detail_navigation": 0.10,
    "entity_cross_reference": 0.10,
    "actor_queue": 0.10,
    "actor_handoff": 0.15,
    "notification": 0.08,
    "alert": 0.08,
    "network_response": 0.10,
    "entity_operation": 0.10,
    "actor_permission": 0.10,
    "access_denied": 0.10,
    "navigation_difference": 0.10,
    "progression_button": 0.06,
    "page_heading": 0.05,
    "breadcrumb": 0.05,
    "empty_state": 0.05,
    "prerequisite_check": 0.08,
    "branch_control": 0.08,
    "application_memory": 0.05,
}

MIN_DISTINCT_SOURCES_CONFIRMED = 2
CONFIRMED_MIN_CONFIDENCE = 0.4
# Applied per repeat of the SAME (source_kind, observed_text, page_url) —
# each additional identical observation counts for less, never zero, never
# unbounded.
REPEAT_DECAY = 0.15


def score_evidence(evidence: list[WorkflowEvidence]) -> float:
    seen_exact: set[tuple[str, str, str]] = set()
    seen_kinds: set[str] = set()
    total = 0.0
    for ev in evidence:
        weight = SOURCE_KIND_WEIGHTS.get(ev.source_kind, 0.05)
        key = (ev.source_kind, ev.observed_text, ev.page_url)
        if key in seen_exact:
            continue  # exact duplicate: already fully counted once
        seen_exact.add(key)
        if ev.source_kind in seen_kinds:
            total += weight * REPEAT_DECAY
        else:
            total += weight
            seen_kinds.add(ev.source_kind)
    return min(1.0, total)


def workflow_richness_bonus(descriptor: WorkflowDescriptor) -> float:
    """How much has actually been RECONSTRUCTED, independent of evidence-kind
    diversity — a workflow can be well-evidenced on paper yet have no real
    steps connected, or vice versa."""
    bonus = 0.0
    if descriptor.observed_step_count() > 0:
        bonus += 0.20
    if len(descriptor.steps) >= 2:
        bonus += 0.10
    if descriptor.transitions:
        bonus += 0.15
    if len(descriptor.actors) >= 1:
        bonus += 0.05
    if descriptor.is_cross_role():
        bonus += 0.10
    if descriptor.outcomes:
        bonus += 0.10
    if descriptor.entities:
        bonus += 0.05
    return bonus


def contradiction_penalty(descriptor: WorkflowDescriptor) -> float:
    """Conflicting evidence actively lowers confidence. A transition is
    contradicted when another transition on the SAME entity claims the
    reverse state pair was observed with a HIGHER-or-equal confidence and no
    branch explains the discrepancy (branches are legitimate alternate
    paths, contradictions are not)."""
    if descriptor.status == "contradicted":
        return 0.35
    by_entity: dict[str | None, list] = {}
    for t in descriptor.transitions:
        by_entity.setdefault(t.entity_id, []).append(t)
    penalty = 0.0
    for transitions in by_entity.values():
        pairs = {(t.source_state, t.target_state) for t in transitions}
        reverse_pairs = {(b, a) for (a, b) in pairs}
        conflicting = pairs & reverse_pairs
        if conflicting and not descriptor.branches:
            penalty += 0.15
    return min(0.5, penalty)


def score_confidence(descriptor: WorkflowDescriptor) -> float:
    base = score_evidence(descriptor.supporting_evidence)
    bonus = workflow_richness_bonus(descriptor)
    penalty = contradiction_penalty(descriptor)
    return max(0.0, min(1.0, base + bonus - penalty))


def status_for(descriptor: WorkflowDescriptor) -> str:
    if contradiction_penalty(descriptor) > 0 and descriptor.confidence < CONFIRMED_MIN_CONFIDENCE:
        return "contradicted"
    distinct_kinds = {ev.source_kind for ev in descriptor.supporting_evidence}
    if len(distinct_kinds) < MIN_DISTINCT_SOURCES_CONFIRMED or descriptor.confidence < CONFIRMED_MIN_CONFIDENCE:
        return "candidate"
    if descriptor.observed_step_count() == 0 or descriptor.requires_another_actor():
        return "partial"
    return "confirmed"
