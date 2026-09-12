"""Goal deduplication -- a defensive second pass over candidates already
grouped by `subject_key` in `goal_candidate_builder.py`.

Subject-key identity prevents the common case of duplication (two signals
about the SAME node/edge/gap/contradiction never produce two candidates,
because the builder merges them into one dict entry as it scans). This
module catches the rarer case: two DIFFERENT subject keys that ended up
describing the exact same investigation (same goal_type, same graph nodes)
-- e.g. two distinct standalone gap-derived candidates that both turned
out to reference identical node sets. Those are merged here, and the
merge itself is recorded as a `GoalConflict` so it stays auditable rather
than silently disappearing.
"""

from __future__ import annotations

from app.intelligence.goal_generation.schemas import GoalCandidate, GoalConflict


def deduplicate(candidates: list[GoalCandidate]) -> tuple[list[GoalCandidate], list[GoalConflict]]:
    ordered = sorted(candidates, key=lambda c: c.subject_key)
    by_signature: dict[tuple[str, frozenset], GoalCandidate] = {}
    conflicts: list[GoalConflict] = []

    for candidate in ordered:
        signature = (candidate.goal_type, frozenset(candidate.supporting_graph_nodes))
        if not candidate.supporting_graph_nodes:
            # No shared-node signature to merge on (e.g. a contradiction or
            # reference candidate with a synthetic subject) -- keep as-is.
            by_signature[(candidate.subject_key, frozenset())] = candidate
            continue

        existing = by_signature.get(signature)
        if existing is None:
            by_signature[signature] = candidate
            continue

        _merge_into(existing, candidate)
        conflicts.append(
            GoalConflict(
                conflict_id=f"conflict:{existing.subject_key}<->{candidate.subject_key}",
                goal_ids=[existing.subject_key, candidate.subject_key],
                conflict_type="duplicate",
                reason=f"Both '{existing.subject_key}' and '{candidate.subject_key}' target the same {candidate.goal_type} investigation over {sorted(candidate.supporting_graph_nodes)}.",
                resolution="merged",
            )
        )

    return list(by_signature.values()), conflicts


def _merge_into(target: GoalCandidate, other: GoalCandidate) -> None:
    target.supporting_evidence.extend(other.supporting_evidence)
    target.supporting_graph_nodes = sorted(set(target.supporting_graph_nodes) | set(other.supporting_graph_nodes))
    target.supporting_graph_edges = sorted(set(target.supporting_graph_edges) | set(other.supporting_graph_edges))
    target.contradictions = sorted(set(target.contradictions) | set(other.contradictions))
    target.blocking_gaps = sorted(set(target.blocking_gaps) | set(other.blocking_gaps))
    target.source_gap_ids = sorted(set(target.source_gap_ids) | set(other.source_gap_ids))
    target.source_contradiction_ids = sorted(set(target.source_contradiction_ids) | set(other.source_contradiction_ids))
    target.source_consistency_issue_ids = sorted(set(target.source_consistency_issue_ids) | set(other.source_consistency_issue_ids))
    target.source_inference_rule_ids = sorted(set(target.source_inference_rule_ids) | set(other.source_inference_rule_ids))
    target.source_reference_ids = sorted(set(target.source_reference_ids) | set(other.source_reference_ids))
    target.required_entities = sorted(set(target.required_entities) | set(other.required_entities))
    target.required_actors = sorted(set(target.required_actors) | set(other.required_actors))
    target.required_workflows = sorted(set(target.required_workflows) | set(other.required_workflows))
    target.required_outputs = sorted(set(target.required_outputs) | set(other.required_outputs))
    target.required_permissions = sorted(set(target.required_permissions) | set(other.required_permissions))
    target.required_states = sorted(set(target.required_states) | set(other.required_states))
    target.required_context = sorted(set(target.required_context) | set(other.required_context))
    target.already_verified = target.already_verified and other.already_verified
    target.source_confidence = max(target.source_confidence, other.source_confidence)
