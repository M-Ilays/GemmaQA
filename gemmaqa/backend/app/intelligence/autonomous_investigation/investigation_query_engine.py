"""Investigation query API -- the only way callers read investigation
memory directly. Mirrors `strategy_query_engine.py`'s role: simple,
bounded, deterministic lookups, never a place for new reasoning.
"""

from __future__ import annotations

from app.intelligence.autonomous_investigation.schemas import InvestigationStatistics


class InvestigationQueryEngine:
    def __init__(self, memory) -> None:
        self.memory = memory

    def current_investigation(self):
        return self.memory.active

    def all_results(self) -> list:
        return [self.memory.results[iid] for iid in self.memory.history_order]

    def completed(self) -> list:
        return [r for r in self.all_results() if r.outcome == "completed"]

    def failed(self) -> list:
        return [r for r in self.all_results() if r.outcome == "failed"]

    def blocked(self) -> list:
        return [r for r in self.all_results() if r.outcome == "blocked"]

    def paused(self) -> list:
        return [r for r in self.all_results() if r.outcome == "paused"]

    def cancelled(self) -> list:
        return [r for r in self.all_results() if r.outcome == "cancelled"]

    def history(self) -> list:
        return self.all_results()

    def result_by_id(self, investigation_id: str):
        return self.memory.results.get(investigation_id)

    def latest_evidence(self):
        for iid in reversed(self.memory.history_order):
            bundle = self.memory.results[iid].evidence_bundle
            if bundle is not None:
                return bundle
        return None

    def coverage(self):
        for iid in reversed(self.memory.history_order):
            updates = self.memory.results[iid].coverage_updates
            if updates is not None:
                return updates
        return None

    def confidence(self):
        for iid in reversed(self.memory.history_order):
            updates = self.memory.results[iid].confidence_updates
            if updates is not None:
                return updates
        return None

    def statistics(self, *, stop_reason: str = "none") -> InvestigationStatistics:
        results = self.all_results()
        by_outcome: dict[str, int] = {}
        steps_executed = 0
        supported = contradicted = inconclusive = 0
        for r in results:
            by_outcome[r.outcome] = by_outcome.get(r.outcome, 0) + 1
            steps_executed += r.steps_executed
            if r.verification is not None:
                supported += r.verification.supported_count
                contradicted += r.verification.contradicted_count
                inconclusive += r.verification.inconclusive_count
        return InvestigationStatistics(
            total_investigations=len(results), investigations_by_outcome=by_outcome,
            total_steps_executed=steps_executed, total_assertions_evaluated=supported + contradicted + inconclusive,
            supported_assertion_count=supported, contradicted_assertion_count=contradicted, inconclusive_assertion_count=inconclusive,
            total_recovery_attempts=self.memory.total_recovery_attempts, stop_reason=stop_reason,
        )
