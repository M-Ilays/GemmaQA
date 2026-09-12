"""AutonomousInvestigationEngine -- the orchestrator.

This is the execution brain: it selects the next executable scenario from
the QA Strategy Engine's queues, drives it step-by-step through the
EXISTING runtime `Planner` (never a raw Playwright call, never a new
selector-matching heuristic), verifies assertions against genuinely
observed evidence, and closes the loop by re-running Goal Generation
against the Knowledge Graph the controller's own existing per-action hooks
already keep in sync.

Two entry points, matching the two places the controller's existing loop
already has a natural seam:

- `next_action()` -- called from the PLAN phase, BEFORE the normal
  frontier-based `Planner.next_action()` call, exactly like
  `self.presentation.next_action()` already is. Returns a `BrowserAction`
  to dispatch through the controller's UNCHANGED VALIDATE -> EXECUTE ->
  COMPARE pipeline, or `None` to fall through to normal exploration.
- `observe_step_result()` -- called from the post-action hook block
  (after `_run_qa_strategy()`), with the `ActionResult` for whatever action
  `next_action()` just produced. Reconciles the in-flight investigation:
  advances to the next step, retries, or finalizes it.

Never bypasses `ActionValidator`/`ActionExecutor`/`BrowserAdapter` -- those
run exactly as they do for any other action, on whatever `BrowserAction`
this engine returns. Never mutates the Knowledge Graph directly -- new
evidence flows through the SAME entity/actor/workflow/dependency registries
and `ApplicationKnowledgeGraph.synchronize()` every other action already
uses; this engine only re-runs Goal Generation against the result and
reports which goal ids are new (see `knowledge_feedback.py`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.intelligence.autonomous_investigation.assertion_verifier import evaluate_assertion, evaluate_comparison, summarize
from app.intelligence.autonomous_investigation.coverage_confidence_updater import (
    build_confidence_updates,
    build_coverage_updates,
    build_knowledge_updates,
)
from app.intelligence.autonomous_investigation.eligibility_classifier import classify_eligibility
from app.intelligence.autonomous_investigation.evidence_bundle_builder import build_evidence_bundle
from app.intelligence.autonomous_investigation.investigation_memory import InvestigationMemory
from app.intelligence.autonomous_investigation.investigation_query_engine import InvestigationQueryEngine
from app.intelligence.autonomous_investigation.investigation_safety_gate import check_safety
from app.intelligence.autonomous_investigation.knowledge_feedback import build_next_goals, goal_ids_snapshot, regenerate_goals
from app.intelligence.autonomous_investigation.precondition_validator import check_preconditions
from app.intelligence.autonomous_investigation.recovery import classify_failure, decide_recovery
from app.intelligence.autonomous_investigation.schemas import ActiveInvestigation, ExecutionTrace, InvestigationResult, StopReport
from app.intelligence.autonomous_investigation.semantic_step_executor import resolve_step
from app.intelligence.autonomous_investigation.state_machine import InvestigationStateMachine
from app.utils.exploration_trace import record as trace_record
from app.utils.logging import get_logger

logger = get_logger("intelligence.autonomous_investigation")

MAX_CANDIDATE_SCAN = 5
MAX_STEP_ADVANCE_PER_CALL = 40
MAX_RETRIES_PER_STEP = 2

# InvestigationResult.outcome (INVESTIGATION_OUTCOMES) -> InvestigationScenario.status
# (the new execution-lifecycle values added to SCENARIO_STATUSES). "paused"
# has no entry -- it is not a terminal disposition, so the scenario's
# existing status is left untouched. Refined further for "completed"/
# "failed" by the actual VerificationResult in `_sync_scenario_and_report`.
_OUTCOME_TO_SCENARIO_STATUS = {
    "completed": "passed",
    "failed": "failed",
    "blocked": "blocked",
    "cancelled": "skipped",
}


class AutonomousInvestigationEngine:
    def __init__(self, memory: InvestigationMemory | None = None) -> None:
        self.memory = memory or InvestigationMemory()
        self.query_engine = InvestigationQueryEngine(self.memory)
        self._fsm: InvestigationStateMachine | None = None
        self._before_graph_stats = None
        self._before_scenario_stats = None
        self._before_goal_ids: set[str] = set()
        self._last_before_state = None
        self._last_after_state = None
        self._last_result = None

    # -- stop conditions ------------------------------------------------------

    # `RunMemory.stop_reason` (set by the OUTER exploration loop —
    # `RunMemory.should_stop()` / `AgentController.run()`) uses its OWN,
    # older free-text vocabulary. It is NOT synonymous with "the user
    # cancelled the run" — conflating the two previously meant a run that
    # stopped on `action_budget_exhausted` was misreported here as
    # `user_cancellation`. Translated to this package's own STOP_REASONS
    # vocabulary; anything unrecognised (including a genuine cancellation)
    # falls back to `user_cancellation` as the least-wrong default.
    _OUTER_STOP_REASON_MAP = {
        "action_budget_exhausted": "budget_exceeded",
        "evidence storage budget exceeded": "budget_exceeded",
        "maximum runtime exceeded": "time_exceeded",
        "unresolved_authentication": "authentication_lost",
        "persistent_navigation_loop": "frontier_exhausted",
        "all_safe_candidates_exhausted": "frontier_exhausted",
        "exploration_complete": "all_executable_scenarios_completed",
    }

    def stop_reason(self, run_memory: Any) -> str:
        outer_reason = getattr(run_memory, "stop_reason", None)
        if outer_reason:
            return self._OUTER_STOP_REASON_MAP.get(str(outer_reason).strip().lower(), "user_cancellation")
        if getattr(run_memory, "remaining_action_budget", 1) <= 0:
            return "budget_exceeded"
        auth = getattr(run_memory, "auth_strategy", None)
        if auth is not None and not getattr(auth, "authenticated", True) and getattr(run_memory, "authenticated_page_count", 0) > 0:
            return "authentication_lost"
        if self.memory.consecutive_failures >= 3:
            return "repeated_failures"
        strategy_engine = getattr(run_memory, "strategy_engine", None)
        if self.memory.active is None:
            if strategy_engine is None:
                return "no_executable_scenarios"
            ready = list(strategy_engine.query_engine.ready())
            if not ready:
                return "no_executable_scenarios"
            remaining = [
                c for c in ready
                if c.candidate_id not in self.memory.investigated_candidate_ids
                and c.candidate_id not in self.memory.blocked_candidate_ids
            ]
            if not remaining:
                blocked = sum(1 for c in ready if c.candidate_id in self.memory.blocked_candidate_ids)
                if blocked and blocked == len(ready):
                    return "all_remaining_scenarios_blocked"
                if blocked:
                    return "all_remaining_scenarios_blocked"
                return "all_executable_scenarios_completed"
        return "none"

    def stop_report(self, run_memory: Any) -> StopReport:
        """The full "why did we stop, and what should happen next" answer —
        see `StopReport`. Always computable, even when `stop_reason() ==
        "none"` (the run just hasn't stopped yet)."""
        reason = self.stop_reason(run_memory)
        active = self.memory.active
        history = self.query_engine.history()
        last_result = history[-1] if history else None
        last_step_id = None
        if last_result is not None and last_result.execution_trace:
            last_step_id = last_result.execution_trace[-1].step_id

        blocked_counts: dict[str, int] = {}
        for eligibility in self.memory.eligibility_by_candidate_id.values():
            status = eligibility.get("status", "unknown")
            if status not in {"executable", "executable_with_controlled_writes", "read_only_executable"}:
                blocked_counts[status] = blocked_counts.get(status, 0) + 1

        strategy_engine = getattr(run_memory, "strategy_engine", None)
        unexecuted = 0
        if strategy_engine is not None:
            unexecuted = sum(
                1 for c in strategy_engine.query_engine.ready()
                if c.candidate_id not in self.memory.investigated_candidate_ids
            )

        recommendations = {
            "no_executable_scenarios": "No scenario has reached QA Strategy's ready() queue yet — check Goal Generation/Scenario Planning coverage against the Knowledge Graph.",
            "all_remaining_scenarios_blocked": "Review blocked_candidate_counts — resolve the most common blocking reason (e.g. enable allow_controlled_writes, or provide the missing actor/test data) and re-run.",
            "all_executable_scenarios_completed": "All executable scenarios ran to a terminal outcome; generate more scenarios (broaden goals) or enable controlled writes to unlock currently-configuration-blocked ones.",
            "budget_exceeded": "Increase max_actions or narrow the investigation's scope to fewer scenarios per run.",
            "repeated_failures": "Investigate the most recent failure_reason values — a persistent step/environment issue is likely, not scenario coverage.",
            "authentication_lost": "Session was lost mid-run; re-authenticate and re-run rather than continuing to burn action budget unauthenticated.",
            "user_cancellation": "Run was cancelled by the caller; no action needed unless cancellation was unintended.",
        }

        return StopReport(
            stop_reason=reason if reason != "none" else "none",
            active_scenario_id=active.scenario_id if active is not None else None,
            last_completed_investigation_id=last_result.investigation_id if last_result is not None else None,
            last_completed_step_id=last_step_id,
            blocked_candidate_counts=blocked_counts,
            unexecuted_scenario_count=unexecuted,
            recommended_next_action=recommendations.get(reason, ""),
        )

    def statistics(self):
        return self.query_engine.statistics()

    # -- PLAN-phase hook --------------------------------------------------------

    def next_action(self, page_state, run_memory: Any, context: dict, *, planner):
        if self.memory.active is None and self.stop_reason(run_memory) != "none":
            return None

        for _ in range(MAX_STEP_ADVANCE_PER_CALL):
            if self.memory.active is None:
                if not self._try_start_next(run_memory):
                    return None

            active = self.memory.active
            if active is None:
                return None
            scenario_engine = getattr(run_memory, "scenario_engine", None)
            scenario = scenario_engine.query_engine.scenario_by_id(active.scenario_id) if scenario_engine else None
            if scenario is None:
                self._finalize(run_memory, outcome="failed", failure_reason="scenario disappeared mid-investigation")
                continue

            if active.current_step_index >= len(scenario.steps):
                self._finalize_success_path(run_memory, scenario)
                continue

            step = scenario.steps[active.current_step_index]
            resolution = resolve_step(step, scenario, page_state, run_memory, planner, context)

            if resolution.kind == "instant":
                self._evaluate_instant_step(active, scenario)
                self._walk("collecting_evidence", "verifying", "planning_next", "ready")
                active.current_step_index += 1
                continue

            if resolution.kind == "skip":
                outcome = self._handle_unresolvable_step(run_memory, active, step, resolution.reason)
                if outcome == "defer":
                    return None
                continue

            active.pending_step_id = step.step_id
            self._walk("executing")
            return resolution.action

        return None

    # -- post-action hook ---------------------------------------------------

    def observe_step_result(self, *, before_state, after_state, result, run_memory: Any, action) -> None:
        active = self.memory.active
        if active is None:
            return
        step_id = (action.metadata or {}).get("investigation_step_id")
        if not step_id or step_id != active.pending_step_id:
            return

        self._last_before_state = before_state
        self._last_after_state = after_state
        self._last_result = result

        scenario_engine = getattr(run_memory, "scenario_engine", None)
        scenario = scenario_engine.query_engine.scenario_by_id(active.scenario_id) if scenario_engine else None
        step = next((s for s in scenario.steps if s.step_id == step_id), None) if scenario is not None else None

        self._walk("waiting", "observing")
        trace = ExecutionTrace(
            trace_id=f"trace:{active.investigation_id}:{step_id}:{len(active.execution_trace)}",
            investigation_id=active.investigation_id, scenario_id=active.scenario_id, step_id=step_id,
            sequence_index=active.current_step_index, state=self._fsm.state if self._fsm else "observing",
            action_type=action.action.value, action_summary=action.reason or "", success=bool(result.success),
            duration_ms=result.duration_ms, evidence_ids=list(result.evidence_ids), error=result.error or "",
        )
        active.pending_step_id = ""

        if not result.success:
            failure_class = classify_failure(result)
            retries = active.retry_counts.get(step_id, 0)
            decision = decide_recovery(failure_class, attempt=retries, max_retries=MAX_RETRIES_PER_STEP)
            trace.recovery_action = decision
            active.execution_trace.append(trace)
            active.retry_counts[step_id] = retries + 1
            active.consecutive_step_failures += 1
            self.memory.total_recovery_attempts += 1
            self._walk("recovery")

            if decision in {"retry", "retry_with_backoff"}:
                active.blockers.append(f"step '{step_id}' failed ({failure_class}); retrying")
                self._walk("ready")
                return
            if decision == "skip_step" and not (step.blocking if step is not None else True):
                active.current_step_index += 1
                active.blockers.append(f"step '{step_id}' failed ({failure_class}); skipped (non-blocking)")
                self._walk("ready")
                return
            self._finalize(run_memory, outcome="failed", failure_reason=f"step '{step_id}' failed ({failure_class}) and could not recover")
            return

        active.consecutive_step_failures = 0
        active.execution_trace.append(trace)
        active.evidence_ids.extend(result.evidence_ids)
        self._walk("collecting_evidence", "verifying", "planning_next", "ready")
        active.current_step_index += 1

    # -- internals ------------------------------------------------------------

    def _walk(self, *states: str) -> None:
        if self._fsm is None:
            return
        for state in states:
            self._fsm.transition(state)
        if self.memory.active is not None:
            self.memory.active.state = self._fsm.state

    def _try_start_next(self, run_memory: Any) -> bool:
        strategy_engine = getattr(run_memory, "strategy_engine", None)
        scenario_engine = getattr(run_memory, "scenario_engine", None)
        if strategy_engine is None or scenario_engine is None:
            self.memory.last_scan_summary = {
                "scanned": 0, "started": False,
                "aggregate_reason": "strategy_engine or scenario_engine not available on run memory",
                "blocked_or_deferred": [],
            }
            return False

        allow_controlled_writes = bool(getattr(run_memory, "configuration", None) and run_memory.configuration.allow_controlled_writes)
        allow_safe_test_data = bool(
            getattr(getattr(run_memory, "configuration", None), "allow_safe_test_data_creation", False)
        )

        scanned = 0
        blocked_or_deferred: list[dict[str, Any]] = []
        ready = list(strategy_engine.query_engine.ready())
        already_done = [
            c for c in ready
            if c.candidate_id in self.memory.investigated_candidate_ids or c.candidate_id in self.memory.blocked_candidate_ids
        ]
        for candidate in ready:
            if candidate.candidate_id in self.memory.blocked_candidate_ids:
                continue
            if candidate.candidate_id in self.memory.investigated_candidate_ids:
                continue
            scanned += 1
            if scanned > MAX_CANDIDATE_SCAN:
                blocked_or_deferred.append(
                    {"candidate_id": candidate.candidate_id, "eligibility": "blocked_by_configuration", "reason": f"MAX_CANDIDATE_SCAN ({MAX_CANDIDATE_SCAN}) reached this call; retried next iteration"}
                )
                break
            scenario = scenario_engine.query_engine.scenario_by_id(candidate.scenario_id)
            if scenario is None:
                continue

            eligibility = classify_eligibility(
                scenario, candidate, scenario_engine=scenario_engine,
                allow_controlled_writes=allow_controlled_writes, allow_safe_test_data_creation=allow_safe_test_data,
            )
            self.memory.eligibility_by_candidate_id[candidate.candidate_id] = eligibility.to_dict()

            safety = check_safety(scenario, candidate)
            if not safety.allowed:
                self._record_immediate_outcome(run_memory, candidate, scenario, outcome="blocked", reasons=[safety.reason])
                blocked_or_deferred.append({"candidate_id": candidate.candidate_id, "eligibility": "blocked_by_safety_policy", "reason": safety.reason})
                continue

            precondition = check_preconditions(scenario, run_memory)
            if precondition.blocking_reasons:
                self._record_immediate_outcome(run_memory, candidate, scenario, outcome="blocked", reasons=precondition.blocking_reasons)
                blocked_or_deferred.append({"candidate_id": candidate.candidate_id, "eligibility": eligibility.status, "reason": "; ".join(precondition.blocking_reasons)})
                continue
            if not precondition.satisfied:
                blocked_or_deferred.append({"candidate_id": candidate.candidate_id, "eligibility": eligibility.status, "reason": "; ".join(precondition.deferred_reasons) or "preconditions not yet satisfiable"})
                continue  # deferred -- preconditions not yet satisfiable; try the next candidate this call

            # Eligibility is recorded for REPORTING (self.memory.
            # eligibility_by_candidate_id, above) but does not itself gate
            # execution beyond safety/preconditions: write-permission
            # enforcement belongs to ActionValidator at the per-action level
            # (the same policy every other action already goes through),
            # not a second, scenario-level duplicate of it here. A
            # "blocked_by_configuration"/"executable_with_controlled_writes"
            # scenario still gets a chance to run; if a step's action really
            # would need a write policy that's off, ActionValidator blocks
            # THAT action exactly as it would for any other candidate.

            self._start_investigation(run_memory, candidate, scenario)
            self.memory.last_scan_summary = {
                "scanned": scanned, "started": True, "started_candidate_id": candidate.candidate_id,
                "aggregate_reason": "", "blocked_or_deferred": blocked_or_deferred,
            }
            return True

        if not ready:
            aggregate = "no scenario reached QA Strategy's ready() queue this pass"
        elif already_done and not blocked_or_deferred and scanned == 0:
            aggregate = f"all {len(already_done)} ready candidate(s) already investigated or blocked in a prior pass"
        elif blocked_or_deferred:
            aggregate = f"scanned {scanned} candidate(s); none currently executable — see blocked_or_deferred"
        else:
            aggregate = "no executable candidate found this pass"
        self.memory.last_scan_summary = {
            "scanned": scanned, "started": False, "aggregate_reason": aggregate, "blocked_or_deferred": blocked_or_deferred,
        }
        return False

    def _record_immediate_outcome(self, run_memory: Any, candidate, scenario, *, outcome: str, reasons: list[str]) -> None:
        attempt = self.memory.next_attempt_number(candidate.candidate_id)
        self.memory.record_attempt(candidate.candidate_id)
        investigation_id = f"investigation:{candidate.candidate_id}:{attempt}"
        result = InvestigationResult(
            investigation_id=investigation_id, candidate_id=candidate.candidate_id, scenario_id=scenario.scenario_id,
            goal_id=scenario.goal_id, outcome=outcome, state=outcome, steps_total=len(scenario.steps), steps_executed=0,
            blockers=list(reasons), failure_reason="; ".join(reasons), ended_at=datetime.utcnow(),
        )
        self.memory.store_result(result)
        try:
            scenario.status = "blocked" if outcome == "blocked" else outcome
        except Exception:
            pass
        app_store = getattr(run_memory, "app_store", None)
        if app_store is not None:
            try:
                app_store.record_investigation_scenario_result(
                    scenario_id=scenario.scenario_id, candidate_id=candidate.candidate_id,
                    title=scenario.title or scenario.objective, category=scenario.scenario_type,
                    steps=[s.description or s.semantic_action or s.step_type for s in scenario.steps],
                    scenario_status="blocked" if outcome == "blocked" else outcome, notes="; ".join(reasons),
                )
            except Exception as exc:
                logger.warning("Failed to record immediate AppTestScenario outcome for %s (%s)", scenario.scenario_id, exc)
        trace_record("autonomous_investigation.immediate_outcome", investigation_id=investigation_id, outcome=outcome, reasons=reasons)

    def _start_investigation(self, run_memory: Any, candidate, scenario) -> None:
        attempt = self.memory.next_attempt_number(candidate.candidate_id)
        self.memory.record_attempt(candidate.candidate_id)
        investigation_id = f"investigation:{candidate.candidate_id}:{attempt}"

        graph = getattr(run_memory, "knowledge_graph", None)
        scenario_engine = getattr(run_memory, "scenario_engine", None)
        self._before_graph_stats = graph.statistics() if graph is not None else None
        self._before_scenario_stats = scenario_engine.statistics() if scenario_engine is not None else None
        self._before_goal_ids = goal_ids_snapshot(getattr(run_memory, "goal_engine", None))
        self._last_before_state = None
        self._last_after_state = None
        self._last_result = None

        self._fsm = InvestigationStateMachine("idle")
        self._fsm.transition("preparing")
        self._fsm.transition("ready")

        self.memory.active = ActiveInvestigation(
            investigation_id=investigation_id, candidate_id=candidate.candidate_id, scenario_id=scenario.scenario_id,
            goal_id=scenario.goal_id, attempt=attempt, state=self._fsm.state, total_steps=len(scenario.steps),
            graph_version_at_start=self._before_graph_stats.graph_version if self._before_graph_stats else 0,
        )
        trace_record(
            "autonomous_investigation.started", investigation_id=investigation_id, candidate_id=candidate.candidate_id,
            scenario_id=scenario.scenario_id, total_steps=len(scenario.steps),
        )

    def _evaluate_instant_step(self, active: ActiveInvestigation, scenario) -> None:
        if active.assertions_evaluated:
            return
        for assertion in scenario.assertions:
            active.assertion_results.append(
                evaluate_assertion(
                    assertion, investigation_id=active.investigation_id,
                    before_state=self._last_before_state, after_state=self._last_after_state, last_result=self._last_result,
                )
            )
        for comparison in scenario.comparisons:
            active.assertion_results.append(
                evaluate_comparison(
                    comparison, investigation_id=active.investigation_id,
                    before_state=self._last_before_state, after_state=self._last_after_state, last_result=self._last_result,
                )
            )
        active.assertions_evaluated = True

    def _handle_unresolvable_step(self, run_memory: Any, active: ActiveInvestigation, step, reason: str) -> str:
        if not step.blocking:
            active.current_step_index += 1
            active.blockers.append(f"step '{step.step_id}' skipped (optional): {reason}")
            return "continue"
        retries = active.retry_counts.get(step.step_id, 0)
        if retries < MAX_RETRIES_PER_STEP:
            active.retry_counts[step.step_id] = retries + 1
            active.blockers.append(f"step '{step.step_id}' deferred (attempt {retries + 1}): {reason}")
            return "defer"
        active.blockers.append(f"step '{step.step_id}' could not be resolved after {retries} attempts: {reason}")
        self._finalize(run_memory, outcome="blocked", failure_reason=reason)
        return "continue"

    def _finalize_success_path(self, run_memory: Any, scenario) -> None:
        active = self.memory.active
        if active is None:
            return
        verification = summarize(active.investigation_id, scenario.scenario_id, active.assertion_results)
        if verification.overall_outcome == "contradicted":
            outcome = "failed"
            failure_reason = "one or more assertions were contradicted by observed evidence"
        elif any("could not be resolved" in b for b in active.blockers):
            outcome = "blocked"
            failure_reason = active.blockers[-1]
        else:
            outcome = "completed"
            failure_reason = ""
        self._walk("updating_knowledge")
        self._finalize(run_memory, outcome=outcome, failure_reason=failure_reason, verification=verification)

    def _finalize(self, run_memory: Any, *, outcome: str, failure_reason: str = "", verification=None) -> None:
        active = self.memory.active
        if active is None:
            return

        graph = getattr(run_memory, "knowledge_graph", None)
        scenario_engine = getattr(run_memory, "scenario_engine", None)
        goal_engine = getattr(run_memory, "goal_engine", None)

        evidence_bundle = build_evidence_bundle(
            active.investigation_id, active.scenario_id, active.execution_trace,
            urls_visited=list(getattr(run_memory, "visited_urls", None) or []),
        )

        knowledge_updates = coverage_updates = confidence_updates = next_goals = None
        if graph is not None and self._before_graph_stats is not None:
            try:
                after_graph_stats = graph.statistics()
                after_scenario_stats = scenario_engine.statistics() if scenario_engine is not None else None
                knowledge_updates = build_knowledge_updates(
                    active.investigation_id, before_graph_stats=self._before_graph_stats, after_graph_stats=after_graph_stats,
                )
                coverage_updates = build_coverage_updates(
                    active.investigation_id, before_graph_stats=self._before_graph_stats, after_graph_stats=after_graph_stats,
                    before_scenario_stats=self._before_scenario_stats, after_scenario_stats=after_scenario_stats,
                )
                confidence_updates = build_confidence_updates(
                    active.investigation_id, before_graph_stats=self._before_graph_stats, after_graph_stats=after_graph_stats,
                    before_scenario_stats=self._before_scenario_stats, after_scenario_stats=after_scenario_stats,
                )
                regenerate_goals(goal_engine, graph, iteration=len(getattr(run_memory, "actions", None) or []))
                after_goal_ids = goal_ids_snapshot(goal_engine)
                next_goals = build_next_goals(
                    active.investigation_id, self._before_goal_ids, after_goal_ids, triggered_by="investigation_completed",
                )
            except Exception as exc:
                logger.warning("Knowledge/coverage/confidence feedback failed (%s); continuing", exc)

        result = InvestigationResult(
            investigation_id=active.investigation_id, candidate_id=active.candidate_id, scenario_id=active.scenario_id,
            goal_id=active.goal_id, outcome=outcome, state=outcome, steps_total=active.total_steps,
            steps_executed=active.current_step_index, execution_trace=list(active.execution_trace), verification=verification,
            evidence_bundle=evidence_bundle, knowledge_updates=knowledge_updates, coverage_updates=coverage_updates,
            confidence_updates=confidence_updates, next_goals=next_goals, blockers=list(active.blockers),
            failure_reason=failure_reason, started_at=active.started_at, ended_at=datetime.utcnow(),
            duration_ms=int((datetime.utcnow() - active.started_at).total_seconds() * 1000),
        )
        self.memory.store_result(result)
        self._sync_scenario_and_report(run_memory, scenario_engine, active, outcome, verification)
        trace_record(
            "autonomous_investigation.finalized", investigation_id=active.investigation_id, outcome=outcome,
            steps_executed=active.current_step_index, steps_total=active.total_steps,
        )
        self.memory.active = None
        self._fsm = None
        self._before_graph_stats = None
        self._before_scenario_stats = None
        self._before_goal_ids = set()

    def _sync_scenario_and_report(
        self, run_memory: Any, scenario_engine: Any, active: ActiveInvestigation, outcome: str, verification: Any,
    ) -> None:
        """Writes the terminal outcome back onto the SAME `InvestigationScenario`
        object `scenario_engine`'s own registry holds (in-place mutation --
        `scenario_by_id` returns a direct reference, never a copy), then
        bridges the result into `AppTestScenario` via `ApplicationStore` so
        report/coverage reflects it. Best-effort and non-fatal, matching
        every other post-investigation feedback step in this method."""
        scenario_status = _OUTCOME_TO_SCENARIO_STATUS.get(outcome)
        if scenario_status is None:
            return  # e.g. "paused" -- not a terminal disposition
        if verification is not None:
            if outcome == "completed" and verification.overall_outcome in {"inconclusive", "mixed"}:
                scenario_status = "inconclusive"
            elif outcome == "failed" and verification.overall_outcome == "contradicted":
                scenario_status = "contradicted"

        scenario = scenario_engine.query_engine.scenario_by_id(active.scenario_id) if scenario_engine is not None else None
        if scenario is not None:
            try:
                scenario.status = scenario_status
            except Exception as exc:
                logger.warning("Failed to write back scenario.status=%s for %s (%s)", scenario_status, active.scenario_id, exc)

        app_store = getattr(run_memory, "app_store", None)
        if app_store is None:
            return
        try:
            app_store.record_investigation_scenario_result(
                scenario_id=active.scenario_id,
                candidate_id=active.candidate_id,
                title=(getattr(scenario, "title", "") or getattr(scenario, "objective", "")) if scenario is not None else "",
                category=getattr(scenario, "scenario_type", "") if scenario is not None else "investigation",
                steps=[s.description or s.semantic_action or s.step_type for s in getattr(scenario, "steps", [])] if scenario is not None else [],
                scenario_status=scenario_status,
                notes="; ".join(active.blockers[-3:]) if active.blockers else "",
            )
        except Exception as exc:
            logger.warning("Failed to record AppTestScenario result for %s (%s)", active.scenario_id, exc)
