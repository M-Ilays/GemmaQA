"""Strategy memory -- the raw store for `ExecutionCandidate`/`ExecutionQueue`/
`ExecutionBatch`/`ExecutionDependency`/`ExecutionConflict`/
`ExecutionRecommendation`/`ExecutionForecast` records and their idempotent
merge logic. Mirrors `scenario_planning/scenario_memory.py`'s role: this is
NOT a second application-memory system, it only holds the OUTPUT of the
QA Strategy Engine.

Idempotency matters here exactly as it did for Scenario Planning: re-running
`generate()` against an UNCHANGED scenario set must leave every candidate's
`created_at`/`observation_count` untouched -- only genuinely changed content
bumps them, and only a genuine content change bumps `strategy_version`.
"""

from __future__ import annotations

from app.intelligence.qa_strategy.schemas import (
    ExecutionBatch,
    ExecutionCandidate,
    ExecutionConflict,
    ExecutionDependency,
    ExecutionForecast,
    ExecutionOrdering,
    ExecutionQueue,
    ExecutionRecommendation,
    ExecutionStrategy,
)

_CONTENT_EXCLUDE = {"created_at", "updated_at", "observation_count"}


class StrategyMemory:
    def __init__(self) -> None:
        self.candidates: dict[str, ExecutionCandidate] = {}
        self.queues: dict[str, ExecutionQueue] = {}
        self.batches: dict[str, ExecutionBatch] = {}
        self.dependencies: dict[str, ExecutionDependency] = {}
        self.conflicts: dict[str, ExecutionConflict] = {}
        self.recommendations: dict[str, ExecutionRecommendation] = {}
        self.forecasts: dict[str, ExecutionForecast] = {}
        self.ordering: ExecutionOrdering | None = None
        self.strategy: ExecutionStrategy | None = None
        self.strategy_version: int = 0
        self.last_graph_version: int = 0
        self.last_scenario_plan_version: int = 0

        self.dirty_this_pass: bool = False
        self.added_candidate_ids: list[str] = []
        self.updated_candidate_ids: list[str] = []
        self.stale_candidate_ids: list[str] = []

    def begin_pass(self) -> None:
        self.dirty_this_pass = False
        self.added_candidate_ids = []
        self.updated_candidate_ids = []
        self.stale_candidate_ids = []

    def end_pass(self) -> bool:
        if self.dirty_this_pass:
            self.strategy_version += 1
        return self.dirty_this_pass

    # -- candidates -----------------------------------------------------------

    def upsert_candidate(self, candidate: ExecutionCandidate) -> ExecutionCandidate:
        existing = self.candidates.get(candidate.candidate_id)
        if existing is None:
            self.candidates[candidate.candidate_id] = candidate
            self.added_candidate_ids.append(candidate.candidate_id)
            self.dirty_this_pass = True
            return candidate

        if _content_differs(existing, candidate):
            candidate.created_at = existing.created_at
            candidate.observation_count = existing.observation_count + 1
            self.candidates[candidate.candidate_id] = candidate
            self.updated_candidate_ids.append(candidate.candidate_id)
            self.dirty_this_pass = True
            return candidate
        return existing

    def remove_candidates_not_in(self, still_present_ids: set[str]) -> None:
        for candidate_id in list(self.candidates.keys()):
            if candidate_id not in still_present_ids:
                del self.candidates[candidate_id]
                self.stale_candidate_ids.append(candidate_id)
                self.dirty_this_pass = True

    # -- recomputed-each-pass collections (deterministic ids make wholesale
    #    replacement itself idempotent) --------------------------------------

    def replace_queues(self, queues: list[ExecutionQueue]) -> None:
        new_queues = {q.queue_id: q for q in queues}
        if set(new_queues) != set(self.queues) or any(
            new_queues[qid].candidate_ids != self.queues[qid].candidate_ids for qid in new_queues if qid in self.queues
        ):
            self.dirty_this_pass = True
        self.queues = new_queues

    def replace_batches(self, batches: list[ExecutionBatch]) -> None:
        new_batches = {b.batch_id: b for b in batches}
        if set(new_batches) != set(self.batches):
            self.dirty_this_pass = True
        self.batches = new_batches

    def replace_dependencies(self, dependencies: list[ExecutionDependency]) -> None:
        new_deps = {d.dependency_id: d for d in dependencies}
        if set(new_deps) != set(self.dependencies):
            self.dirty_this_pass = True
        self.dependencies = new_deps

    def replace_conflicts(self, conflicts: list[ExecutionConflict]) -> None:
        new_conflicts = {c.conflict_id: c for c in conflicts}
        if set(new_conflicts) != set(self.conflicts):
            self.dirty_this_pass = True
        self.conflicts = new_conflicts

    def replace_recommendations(self, recommendations: list[ExecutionRecommendation]) -> None:
        new_recs = {r.recommendation_id: r for r in recommendations}
        if set(new_recs) != set(self.recommendations):
            self.dirty_this_pass = True
        self.recommendations = new_recs

    def replace_forecasts(self, forecasts: list[ExecutionForecast]) -> None:
        new_forecasts = {f.forecast_id: f for f in forecasts}
        if set(new_forecasts) != set(self.forecasts):
            self.dirty_this_pass = True
        self.forecasts = new_forecasts

    def set_ordering(self, ordering: ExecutionOrdering) -> None:
        if self.ordering is None or self.ordering.ordered_candidate_ids != ordering.ordered_candidate_ids:
            self.dirty_this_pass = True
        self.ordering = ordering

    def set_strategy(self, strategy: ExecutionStrategy) -> None:
        self.strategy = strategy


def _content_differs(existing: ExecutionCandidate, incoming: ExecutionCandidate) -> bool:
    a = existing.model_dump(exclude=_CONTENT_EXCLUDE)
    b = incoming.model_dump(exclude=_CONTENT_EXCLUDE)
    return a != b
