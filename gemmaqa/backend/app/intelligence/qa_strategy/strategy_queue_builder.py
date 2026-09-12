"""Assigns each `ExecutionCandidate` to exactly one of the 10 named queues.

Deterministic, first-match-wins classification in a FIXED priority order
(Blocked, Unknown, Deferred, Immediate, Cross-Actor, Cleanup, Regression,
Exploration, Mutation, Read-only) so a candidate's queue never depends on
iteration order -- only on its own already-computed fields.
"""

from __future__ import annotations

from app.intelligence.qa_strategy.schemas import ExecutionCandidate, ExecutionQueue

REGRESSION_SCENARIO_TYPES = frozenset({"contradiction_resolution", "graph_gap_resolution", "stale_knowledge_refresh"})
EXPLORATION_SCENARIO_TYPES = frozenset(
    {"exploratory_observation", "unresolved_reference_resolution", "confidence_increase", "inferred_relationship_validation"}
)

QUEUE_ORDER = (
    "blocked", "unknown", "deferred", "immediate", "cross_actor",
    "cleanup", "regression", "exploration", "mutation", "read_only",
)

_QUEUE_DESCRIPTIONS = {
    "blocked": "Candidates that cannot execute: prohibited risk, blocked feasibility, or an unmet blocking prerequisite.",
    "unknown": "Candidates whose feasibility could not be determined and need investigation before scheduling.",
    "deferred": "Candidates deferred until a higher-priority prerequisite runs first, or redundant scenario variants.",
    "immediate": "Ready to run now: no dependencies, no cleanup, a single actor, and high priority.",
    "cross_actor": "Require more than one actor and therefore an actor hand-off during execution.",
    "cleanup": "Require non-trivial cleanup after execution.",
    "regression": "Re-verify previously observed contradictions, graph gaps, or stale knowledge.",
    "exploration": "Exploratory or confidence-building observations rather than targeted verification.",
    "mutation": "Mutate application state and do not fit a more specific queue.",
    "read_only": "Read-only observations that do not fit a more specific queue.",
}


def assign_queue(candidate: ExecutionCandidate) -> str:
    if candidate.recommended_action == "block":
        return "blocked"
    if candidate.feasibility_status == "unknown":
        return "unknown"
    if candidate.recommended_action in {"defer", "skip"}:
        return "deferred"

    # From here candidate.recommended_action == "execute".
    if (
        not candidate.depends_on_candidate_ids
        and not candidate.required_cleanup
        and len(candidate.required_actors) <= 1
        and candidate.priority_score >= 0.6
    ):
        return "immediate"
    if len(candidate.required_actors) > 1:
        return "cross_actor"
    if candidate.required_cleanup:
        return "cleanup"
    if candidate.scenario_type in REGRESSION_SCENARIO_TYPES:
        return "regression"
    if candidate.scenario_type in EXPLORATION_SCENARIO_TYPES:
        return "exploration"
    if candidate.risk_class == "read_only":
        return "read_only"
    return "mutation"


def build_queues(candidates: list[ExecutionCandidate]) -> list[ExecutionQueue]:
    """Assigns `candidate.queue_type` in place and returns the 10 queues in
    a stable order, each with its candidates ordered by descending priority
    (ties broken by candidate_id for determinism)."""
    buckets: dict[str, list[ExecutionCandidate]] = {name: [] for name in QUEUE_ORDER}
    for candidate in candidates:
        queue_type = assign_queue(candidate)
        candidate.queue_type = queue_type
        buckets[queue_type].append(candidate)

    queues = []
    for queue_type in QUEUE_ORDER:
        ordered = sorted(buckets[queue_type], key=lambda c: (-c.priority_score, c.candidate_id))
        queues.append(
            ExecutionQueue(
                queue_id=f"queue:{queue_type}",
                queue_type=queue_type,
                candidate_ids=[c.candidate_id for c in ordered],
                description=_QUEUE_DESCRIPTIONS[queue_type],
            )
        )
    return queues
