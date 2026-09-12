"""Groups executable candidates into `ExecutionBatch` records, minimising
actor switching and navigation by picking the MOST SPECIFIC shared
dimension available for each candidate: actor+workflow, then
actor+entity, then actor+output, then actor alone, then "ungrouped" as a
last resort. Only candidates that are actually going to run are batched --
blocked/unknown/deferred candidates have nothing to schedule.
"""

from __future__ import annotations

from app.intelligence.qa_strategy.schemas import ExecutionBatch, ExecutionCandidate

_BATCHED_QUEUES = frozenset(
    {"immediate", "cross_actor", "cleanup", "regression", "exploration", "mutation", "read_only"}
)


def _batch_dimension(candidate: ExecutionCandidate) -> tuple[str, str]:
    actor = candidate.primary_actor or ""
    if candidate.primary_workflow:
        return "actor_workflow", f"{actor}|{candidate.primary_workflow}"
    if candidate.primary_entity:
        return "actor_entity", f"{actor}|{candidate.primary_entity}"
    if candidate.primary_output:
        return "actor_output", f"{actor}|{candidate.primary_output}"
    if actor:
        return "actor", actor
    return "ungrouped", ""


def build_batches(candidates: list[ExecutionCandidate]) -> list[ExecutionBatch]:
    """Assigns `candidate.batch_id` in place and returns batches ordered by
    descending average member priority (ties broken by batch_id)."""
    groups: dict[tuple[str, str], list[ExecutionCandidate]] = {}
    for candidate in candidates:
        if candidate.queue_type not in _BATCHED_QUEUES:
            continue
        key = _batch_dimension(candidate)
        groups.setdefault(key, []).append(candidate)

    batches: list[ExecutionBatch] = []
    for (batch_type, batch_key), members in groups.items():
        members.sort(key=lambda c: (-c.priority_score, c.candidate_id))
        batch_id = f"batch:{batch_type}:{batch_key}" if batch_key else f"batch:{batch_type}"
        for member in members:
            member.batch_id = batch_id
        distinct_actors = {m.primary_actor for m in members if m.primary_actor}
        estimated_switches = max(0, len(distinct_actors) - 1)
        batches.append(
            ExecutionBatch(
                batch_id=batch_id,
                batch_type=batch_type,
                batch_key=batch_key,
                candidate_ids=[m.candidate_id for m in members],
                primary_actor=members[0].primary_actor if len(distinct_actors) <= 1 else "",
                estimated_actor_switches=estimated_switches,
            )
        )

    # Order by descending average candidate priority, ties by batch_id.
    priority_by_candidate = {c.candidate_id: c.priority_score for c in candidates}
    batches.sort(
        key=lambda b: (
            -(sum(priority_by_candidate.get(cid, 0.0) for cid in b.candidate_ids) / max(1, len(b.candidate_ids))),
            b.batch_id,
        )
    )
    return batches
