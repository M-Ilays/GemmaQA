"""Investigation memory -- the store for finalized `InvestigationResult`
records plus the single in-flight `ActiveInvestigation`. Unlike the
discovery/reasoning engines' memories, an investigation is a one-off
execution event, not a continuously re-synced observation: there is no
content-diff idempotency to apply here, only deterministic ids (so
re-querying is always stable) and an append-only history.
"""

from __future__ import annotations

from app.intelligence.autonomous_investigation.schemas import ActiveInvestigation, InvestigationResult


class InvestigationMemory:
    def __init__(self) -> None:
        self.results: dict[str, InvestigationResult] = {}
        self.history_order: list[str] = []
        self.active: ActiveInvestigation | None = None
        self.attempt_counts: dict[str, int] = {}
        self.consecutive_failures: int = 0
        self.total_recovery_attempts: int = 0
        self.blocked_candidate_ids: set[str] = set()
        # Every candidate_id that has reached ANY terminal outcome at least
        # once -- never re-selected again automatically within the same
        # run. Without this, `_try_start_next` would keep re-picking
        # whatever candidate the strategy queue ranks highest forever,
        # since completing an investigation does not by itself change that
        # candidate's own `recommended_action`/priority in the strategy
        # snapshot already handed to this engine.
        self.investigated_candidate_ids: set[str] = set()
        # The most recent eligibility classification computed per candidate
        # (app.intelligence.autonomous_investigation.eligibility_classifier)
        # — a running record so "why is nothing executable" can always be
        # answered from the LAST scan, not just guessed from silence. Keyed
        # by candidate_id; value is a plain dict (status/reason) rather than
        # the dataclass itself so this stays trivially serializable for
        # reporting/tracing.
        self.eligibility_by_candidate_id: dict[str, dict[str, str]] = {}
        # Summary of the most recent `_try_start_next` scan — None until the
        # first scan runs. See AutonomousInvestigationEngine._try_start_next.
        self.last_scan_summary: dict[str, object] | None = None

    def next_attempt_number(self, candidate_id: str) -> int:
        return self.attempt_counts.get(candidate_id, 0) + 1

    def record_attempt(self, candidate_id: str) -> None:
        self.attempt_counts[candidate_id] = self.attempt_counts.get(candidate_id, 0) + 1

    def store_result(self, result: InvestigationResult) -> None:
        if result.investigation_id not in self.results:
            self.history_order.append(result.investigation_id)
        self.results[result.investigation_id] = result
        if result.candidate_id and result.outcome in {"completed", "failed", "blocked"}:
            self.investigated_candidate_ids.add(result.candidate_id)
        if result.outcome in {"failed", "blocked"}:
            self.consecutive_failures += 1
            if result.outcome == "blocked" and result.candidate_id:
                self.blocked_candidate_ids.add(result.candidate_id)
        elif result.outcome == "completed":
            self.consecutive_failures = 0
