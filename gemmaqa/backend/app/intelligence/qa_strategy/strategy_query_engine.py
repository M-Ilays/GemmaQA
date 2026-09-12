"""Strategy query API -- the only way callers read strategy memory
directly. Mirrors `scenario_query_engine.py`'s role: simple, bounded,
deterministic lookups, never a place for new reasoning.
"""

from __future__ import annotations

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term
from app.intelligence.qa_strategy.schemas import ExecutionStatistics

_RISK_ORDER = {"read_only": 0, "low": 1, "moderate": 2, "high": 3, "prohibited": 4, "unknown": 5}


class StrategyQueryEngine:
    def __init__(self, memory) -> None:
        self.memory = memory

    # -- basic lookups ----------------------------------------------------

    def candidate_by_id(self, candidate_id: str):
        return self.memory.candidates.get(candidate_id)

    def all_candidates(self) -> list:
        return sorted(self.memory.candidates.values(), key=lambda c: c.candidate_id)

    def candidates_by_queue(self, queue_type: str) -> list:
        queue = self.memory.queues.get(f"queue:{queue_type}")
        if queue is None:
            return []
        return [self.memory.candidates[cid] for cid in queue.candidate_ids if cid in self.memory.candidates]

    def candidates_by_action(self, action: str) -> list:
        return sorted((c for c in self.memory.candidates.values() if c.recommended_action == action), key=lambda c: c.candidate_id)

    def candidates_for_goal(self, goal_id: str) -> list:
        return sorted((c for c in self.memory.candidates.values() if c.goal_id == goal_id), key=lambda c: c.candidate_id)

    def batch_by_id(self, batch_id: str):
        return self.memory.batches.get(batch_id)

    def all_batches(self) -> list:
        return sorted(self.memory.batches.values(), key=lambda b: b.batch_id)

    def all_queues(self) -> list:
        return sorted(self.memory.queues.values(), key=lambda q: q.queue_id)

    def dependencies_for_candidate(self, candidate_id: str) -> list:
        return sorted((d for d in self.memory.dependencies.values() if d.candidate_id == candidate_id), key=lambda d: d.dependency_id)

    def conflicts_for_candidate(self, candidate_id: str) -> list:
        return sorted((c for c in self.memory.conflicts.values() if candidate_id in c.candidate_ids), key=lambda c: c.conflict_id)

    def recommendation_for_candidate(self, candidate_id: str):
        return self.memory.recommendations.get(f"recommendation:{candidate_id}")

    # -- required query API -------------------------------------------------

    def next_execution(self):
        ready_candidates = self.ready()
        return ready_candidates[0] if ready_candidates else None

    def highest_value(self, limit: int = 20) -> list:
        return sorted(self.memory.candidates.values(), key=lambda c: (-c.business_value, c.candidate_id))[:limit]

    def lowest_risk(self, limit: int = 20) -> list:
        return sorted(
            self.memory.candidates.values(),
            key=lambda c: (_RISK_ORDER.get(c.risk_class, 5), c.candidate_id),
        )[:limit]

    def blocked(self) -> list:
        return self.candidates_by_queue("blocked")

    def deferred(self) -> list:
        return self.candidates_by_queue("deferred")

    def ready(self) -> list:
        """Candidates recommended for execution, ordered by descending
        priority -- the same order they were placed into their queues."""
        return sorted(
            (c for c in self.memory.candidates.values() if c.recommended_action == "execute"),
            key=lambda c: (-c.priority_score, c.candidate_id),
        )

    def batch_for_actor(self, actor_term: str) -> list:
        target = normalize_term(actor_term)
        return sorted(
            (b for b in self.memory.batches.values() if normalize_term(b.primary_actor) == target),
            key=lambda b: b.batch_id,
        )

    def batch_for_workflow(self, workflow_term: str) -> list:
        target = normalize_term(workflow_term)
        return sorted(
            (b for b in self.memory.batches.values() if b.batch_type == "actor_workflow" and target in normalize_term(b.batch_key)),
            key=lambda b: b.batch_id,
        )

    def recommended_sequence(self) -> list[str]:
        if self.memory.ordering is not None:
            return list(self.memory.ordering.ordered_candidate_ids)
        return [c.candidate_id for c in self.ready()]

    def coverage_forecast(self):
        return self.memory.forecasts.get("forecast:coverage")

    def confidence_forecast(self):
        return self.memory.forecasts.get("forecast:confidence")

    def risk_forecast(self):
        return self.memory.forecasts.get("forecast:risk")

    # -- statistics -----------------------------------------------------------

    def strategy_statistics(self) -> ExecutionStatistics:
        candidates = list(self.memory.candidates.values())
        by_queue: dict[str, int] = {}
        by_action: dict[str, int] = {}
        by_batch_type: dict[str, int] = {}
        priority_sum = risk_sum = 0.0

        for c in candidates:
            by_queue[c.queue_type] = by_queue.get(c.queue_type, 0) + 1
            by_action[c.recommended_action] = by_action.get(c.recommended_action, 0) + 1
            priority_sum += c.priority_score
            risk_sum += c.risk_score

        for b in self.memory.batches.values():
            by_batch_type[b.batch_type] = by_batch_type.get(b.batch_type, 0) + 1

        count = len(candidates) or 1
        policy_used = self.memory.strategy.policy.policy_id if self.memory.strategy else ""
        return ExecutionStatistics(
            total_candidates=len(candidates), candidates_by_queue=by_queue, candidates_by_action=by_action,
            candidates_by_batch_type=by_batch_type, batch_count=len(self.memory.batches),
            dependency_count=len(self.memory.dependencies), conflict_count=len(self.memory.conflicts),
            average_priority=priority_sum / count, average_risk=risk_sum / count, policy_used=policy_used,
            graph_version=self.memory.last_graph_version, strategy_version=self.memory.strategy_version,
        )
