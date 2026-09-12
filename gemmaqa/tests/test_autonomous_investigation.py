"""Autonomous Investigation Engine (app/intelligence/autonomous_investigation/).

Covers: state machine (A), precondition validation (B), safety gate (C),
semantic step execution (D), recovery/retries (E), assertion verification
(F), full execution lifecycle (G), evidence collection (H), knowledge/
coverage/confidence updates (I), stop conditions (J), memory/controller
integration (K), query API (L), determinism/idempotency (M), neutrality
(N) -- built on the real Scenario Planning -> Goal Generation -> Knowledge
Graph -> QA Strategy pipeline, exactly like `test_qa_strategy.py`.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest

from app.intelligence.actor_discovery import ActorRegistry
from app.intelligence.actor_discovery.schemas import ActorEvidence, ActorRecord, PermissionCandidate
from app.intelligence.dependency_discovery import DependencyRegistry
from app.intelligence.dependency_discovery.schemas import DependencyDescriptor, DependencyEvidence, DerivedOutputDescriptor
from app.intelligence.entity_discovery import EntityRegistry
from app.intelligence.entity_discovery.schemas import EntityEvidence, EntityRecord
from app.intelligence.goal_generation import GoalGenerationEngine
from app.intelligence.knowledge_graph import ApplicationKnowledgeGraph
from app.intelligence.qa_strategy import QAStrategyEngine
from app.intelligence.autonomous_investigation import AutonomousInvestigationEngine
from app.intelligence.autonomous_investigation.investigation_safety_gate import check_safety
from app.intelligence.autonomous_investigation.precondition_validator import check_preconditions
from app.intelligence.autonomous_investigation.recovery import classify_failure, decide_recovery
from app.intelligence.autonomous_investigation.assertion_verifier import evaluate_assertion, evaluate_comparison
from app.intelligence.autonomous_investigation.semantic_step_executor import resolve_step
from app.intelligence.autonomous_investigation.state_machine import InvestigationStateMachine
from app.intelligence.autonomous_investigation.schemas import ASSERTION_OUTCOMES, EXECUTION_STATES
from app.intelligence.scenario_planning import ScenarioPlanningEngine
from app.intelligence.workflow_discovery import WorkflowRegistry
from app.intelligence.workflow_discovery.schemas import WorkflowActorParticipation, WorkflowEntityParticipation, WorkflowStep
from app.schemas import ActionCategory, ActionResult, ActionType, BrowserAction, RiskLevel
from app.utils.ids import new_id

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(term, *, states=None, confidence=0.8) -> EntityRecord:
    return EntityRecord(
        canonical_name=term, status="confirmed", confidence=confidence, known_states=states or [],
        evidence=[EntityEvidence(source_kind="heading", observed_text=term)],
    )


def _actor(term, *, permissions=None, confidence=0.7) -> ActorRecord:
    return ActorRecord(
        canonical_name=term, status="confirmed", confidence=confidence, known_permissions=permissions or [],
        supporting_evidence=[ActorEvidence(source_kind="session_info", observed_text=term)],
    )


def _permission(permission_id, *, confidence=0.6) -> PermissionCandidate:
    ev = ActorEvidence(source_kind="visible_control", observed_text=permission_id)
    return PermissionCandidate(permission_id=permission_id, confidence=confidence, positive_evidence=[ev])


def _step(verb, *, entity_id=None, actor_id="current session", sequence_hint=0, source_state=None, target_state=None) -> WorkflowStep:
    return WorkflowStep(
        semantic_action=verb, entity_id=entity_id, actor_id=actor_id, status="observed", sequence_hint=sequence_hint,
        source_state=source_state, target_state=target_state, page_id="p", page_state_fingerprint="f", confidence=0.6,
    )


def _output(label, *, output_type="kpi_card", confidence=0.6, output_id=None) -> DerivedOutputDescriptor:
    kwargs = {"output_id": output_id} if output_id else {}
    return DerivedOutputDescriptor(canonical_label=label, output_type=output_type, raw_value="5", confidence=confidence, evidence=[DependencyEvidence(source_kind="heading", observed_text=label)], **kwargs)


def _dependency(*, workflow_ids=None, output_id, confidence=0.6, effect_direction="unknown", dependency_id=None) -> DependencyDescriptor:
    kwargs = {"dependency_id": dependency_id} if dependency_id else {}
    return DependencyDescriptor(
        canonical_name=f"dep -> {output_id}", dependency_type="entity_dependency", relationship_type="counts",
        source_workflow_ids=workflow_ids or [], target_output_ids=[output_id], status="observed", confidence=confidence,
        effect_direction=effect_direction, supporting_evidence=[DependencyEvidence(source_kind="entity_registry", observed_text="matched")],
        **kwargs,
    )


def _registries():
    return EntityRegistry(), ActorRegistry(), WorkflowRegistry(), DependencyRegistry()


def _single_transition_fixture():
    er, ar, wr, dr = _registries()
    er.memory.records["job"] = _entity("job", states=["open", "closed"])
    ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_create_job", confidence=0.5)])
    wf = wr.memory.get_or_create("job", canonical_name="job workflow")
    wf.steps.append(_step("create", entity_id="job", sequence_hint=0, source_state="open", target_state="closed"))
    wf.actors.append(WorkflowActorParticipation(actor_id="current session", role_in_workflow="initiator", step_ids=[]))
    wf.entities.append(WorkflowEntityParticipation(entity_id="job", role_in_workflow="subject", step_ids=[]))
    output = _output("open jobs", output_type="kpi_card", confidence=0.5, output_id="out1")
    dr.memory.outputs[output.output_id] = output
    dep = _dependency(workflow_ids=["job workflow"], output_id=output.output_id, effect_direction="increase", dependency_id="dep1")
    dr.memory.records["dep1"] = dep
    return er, ar, wr, dr


def _plan_and_strategize(*, er=None, ar=None, wr=None, dr=None, iteration=1):
    graph = ApplicationKnowledgeGraph()
    graph.synchronize(entity_registry=er, actor_registry=ar, workflow_registry=wr, dependency_registry=dr, iteration=iteration)
    goal_engine = GoalGenerationEngine()
    goal_engine.generate(graph, iteration=iteration)
    scenario_engine = ScenarioPlanningEngine()
    scenario_engine.generate(goal_engine, graph, iteration=iteration)
    strategy_engine = QAStrategyEngine()
    strategy_engine.generate(scenario_engine, goal_engine, graph, policy_id="balanced")
    return graph, goal_engine, scenario_engine, strategy_engine


class FakeAuth:
    authenticated = True

    def next_workflow_action(self):
        return None


class FakeRunMemory:
    def __init__(self, *, graph, goal_engine, scenario_engine, strategy_engine, er, ar, wr, budget=300):
        self.knowledge_graph = graph
        self.goal_engine = goal_engine
        self.scenario_engine = scenario_engine
        self.strategy_engine = strategy_engine
        self.actor_registry = ar
        self.entity_registry = er
        self.workflow_registry = wr
        self.auth_strategy = FakeAuth()
        self.visited_urls = ["http://example.test/"]
        self.remaining_action_budget = budget
        self.stop_reason = None
        self.actions = []


class FakePageState:
    visible_text_summary = "hello"
    url = "http://example.test/"


class ScriptedPlanner:
    """A minimal stand-in for `Planner` -- `plan_by_priority` always
    returns a benign CLICK action unless told to fail/finish."""

    def __init__(self, *, fail_first_n: int = 0, always_finish: bool = False):
        self.call_count = 0
        self.fail_first_n = fail_first_n
        self.always_finish = always_finish

    def plan_by_priority(self, page_state, memory, context):
        self.call_count += 1
        if self.always_finish:
            return BrowserAction(action=ActionType.FINISH, reason="nothing left", risk=RiskLevel.LOW, category=ActionCategory.COMPLETION)
        return BrowserAction(action=ActionType.CLICK, element_id="el1", reason="fake click", risk=RiskLevel.LOW, category=ActionCategory.EXPLORATION)


def _run_to_exhaustion(engine, planner, run_memory, *, max_iterations=500, force_failure_step_ids=None):
    """Drives next_action()/observe_step_result() until the engine stops
    producing actions, mirroring the controller's PLAN -> EXECUTE -> COMPARE
    -> post-action-hook cycle exactly (one action per iteration)."""
    force_failure_step_ids = force_failure_step_ids or set()
    page_state = FakePageState()
    iterations = 0
    while iterations < max_iterations:
        iterations += 1
        action = engine.next_action(page_state, run_memory, {}, planner=planner)
        if action is None:
            if engine.stop_reason(run_memory) != "none":
                break
            continue
        step_id = (action.metadata or {}).get("investigation_step_id", "")
        succeed = step_id not in force_failure_step_ids
        result = ActionResult(
            action_id=new_id(), run_id="r1", action=action, success=succeed,
            message="ok" if succeed else "failed", error=None if succeed else "Timeout 3000ms exceeded",
            before_url=page_state.url, after_url=page_state.url, evidence_ids=[f"ev{iterations}"], duration_ms=50,
        )
        engine.observe_step_result(before_state=page_state, after_state=page_state, result=result, run_memory=run_memory, action=action)
        run_memory.actions.append(action)
        run_memory.remaining_action_budget -= 1
    return iterations


# ---------------------------------------------------------------------------
# A. State machine
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_valid_transition_sequence(self):
        fsm = InvestigationStateMachine()
        assert fsm.transition("preparing")
        assert fsm.transition("ready")
        assert fsm.transition("executing")
        assert fsm.transition("waiting")
        assert fsm.transition("observing")
        assert fsm.transition("collecting_evidence")
        assert fsm.transition("verifying")
        assert fsm.transition("planning_next")
        assert fsm.transition("updating_knowledge")
        assert fsm.transition("completed")
        assert fsm.is_terminal

    def test_invalid_transition_rejected(self):
        fsm = InvestigationStateMachine()
        assert not fsm.transition("completed")  # idle -> completed is not allowed
        assert fsm.state == "idle"

    def test_terminal_states_have_no_outgoing_transitions(self):
        for terminal in {"completed", "blocked", "failed", "cancelled"}:
            fsm = InvestigationStateMachine(terminal)
            assert fsm.is_terminal
            assert not fsm.can_transition("preparing")

    def test_all_states_covered_by_vocabulary(self):
        from app.intelligence.autonomous_investigation.state_machine import ALLOWED_TRANSITIONS

        assert set(ALLOWED_TRANSITIONS) == EXECUTION_STATES


# ---------------------------------------------------------------------------
# B. Precondition validation
# ---------------------------------------------------------------------------


class TestPreconditionValidator:
    def test_satisfied_when_actor_known_and_authenticated(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        ready = strategy_engine.query_engine.ready()
        assert ready
        scenario = scenario_engine.query_engine.scenario_by_id(ready[0].scenario_id)
        result = check_preconditions(scenario, run_memory)
        assert result.satisfied or result.deferred  # never silently blocking a scenario Scenario Planning already accepted

    def test_deferred_when_actor_not_yet_authenticated(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        run_memory.auth_strategy = FakeAuth()
        run_memory.auth_strategy.authenticated = False
        ready = strategy_engine.query_engine.ready()
        scenario = next((scenario_engine.query_engine.scenario_by_id(c.scenario_id) for c in ready if scenario_engine.query_engine.scenario_by_id(c.scenario_id).actor_requirements), None)
        if scenario is not None:
            result = check_preconditions(scenario, run_memory)
            assert not result.satisfied

    def test_blocking_requirement_status_is_blocking(self):
        class Obj:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        scenario = Obj(
            actor_requirements=[Obj(canonical_name="admin", status="contradicted", session_required=False)],
            entity_requirements=[], workflow_requirements=[], permission_requirements=[], data_requirements=[], state_requirements=[],
            feasibility_assessment=None,
        )
        result = check_preconditions(scenario, FakeRunMemory(graph=None, goal_engine=None, scenario_engine=None, strategy_engine=None, er=None, ar=None, wr=None))
        assert not result.satisfied
        assert result.blocking_reasons


# ---------------------------------------------------------------------------
# C. Safety gate
# ---------------------------------------------------------------------------


class TestSafetyGate:
    def test_prohibited_scenario_rejected(self):
        class Obj:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        scenario = Obj(risk_assessment=Obj(risk_class="prohibited"), feasibility_status="feasible", steps=[])
        result = check_safety(scenario)
        assert not result.allowed

    def test_blocked_feasibility_rejected(self):
        class Obj:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        scenario = Obj(risk_assessment=Obj(risk_class="moderate"), feasibility_status="blocked", steps=[])
        result = check_safety(scenario)
        assert not result.allowed

    def test_moderate_feasible_scenario_allowed(self):
        class Obj:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        scenario = Obj(risk_assessment=Obj(risk_class="moderate"), feasibility_status="feasible", steps=[Obj(safety_class="moderate")])
        result = check_safety(scenario)
        assert result.allowed

    def test_no_scenario_in_real_pipeline_is_falsely_rejected_for_permission_wording(self):
        # Regression: `_safety_class` in scenario_step_builder.py used to
        # scan the goal's free-text TITLE for prohibited intent patterns,
        # so any permission-related goal (title containing the word
        # "permission") had every one of its steps marked safety_class=
        # "prohibited" even though the scenario's own overall risk_class
        # was "moderate" and feasibility "feasible" -- Scenario Planning
        # considered it perfectly fine to execute, but this engine's own
        # safety gate rejected the whole scenario outright.
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        permission_scenarios = [s for s in scenario_engine.query_engine.all_scenarios() if s.scenario_type.startswith("permission_")]
        assert permission_scenarios
        for scenario in permission_scenarios:
            if scenario.risk_assessment and scenario.risk_assessment.risk_class != "prohibited" and scenario.feasibility_status == "feasible":
                result = check_safety(scenario)
                assert result.allowed, [s.safety_class for s in scenario.steps]


# ---------------------------------------------------------------------------
# D. Semantic step execution
# ---------------------------------------------------------------------------


class TestSemanticStepExecutor:
    def _step(self, **kw):
        class Obj:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        defaults = dict(step_id="s1", description="", semantic_action="", expected_state_change="", expected_output_change="")
        defaults.update(kw)
        return Obj(**defaults)

    def test_wait_step_produces_wait_action_without_planner(self):
        step = self._step(step_type="wait_for_effect")
        res = resolve_step(step, None, None, None, None, {})
        assert res.kind == "action"
        assert res.action.action == ActionType.WAIT

    def test_evidence_step_produces_screenshot_action_without_planner(self):
        step = self._step(step_type="capture_evidence")
        res = resolve_step(step, None, None, None, None, {})
        assert res.kind == "action"
        assert res.action.action == ActionType.TAKE_SCREENSHOT

    def test_evidence_step_skips_screenshot_when_budget_exhausted(self):
        step = self._step(step_type="capture_evidence")
        res = resolve_step(
            step,
            None,
            None,
            None,
            None,
            {"screenshots_taken": 5, "max_screenshots": 5},
        )
        assert res.kind == "instant"
        assert res.action is None
        assert "screenshot budget" in res.reason

    def test_observation_step_resolves_instantly_without_planner(self):
        step = self._step(step_type="verify_state")
        res = resolve_step(step, None, None, None, None, {})
        assert res.kind == "instant"

    def test_browser_driving_step_delegates_to_planner(self):
        step = self._step(step_type="perform_operation")
        planner = ScriptedPlanner()
        res = resolve_step(step, type("S", (), {"title": "t", "objective": ""})(), None, None, planner, {})
        assert res.kind == "action"
        assert planner.call_count == 1
        assert res.action.metadata["investigation_step_id"] == "s1"

    def test_browser_driving_step_finish_becomes_skip(self):
        step = self._step(step_type="navigate")
        planner = ScriptedPlanner(always_finish=True)
        res = resolve_step(step, type("S", (), {"title": "t", "objective": ""})(), None, None, planner, {})
        assert res.kind == "skip"


# ---------------------------------------------------------------------------
# E. Recovery / retries
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_classifies_timeout(self):
        class R:
            success = False
            error = "Timeout 3000ms exceeded"
            message = ""

        assert classify_failure(R()) == "timeout"

    def test_classifies_unknown_on_success(self):
        class R:
            success = True
            error = None
            message = ""

        assert classify_failure(R()) == "unknown"

    def test_retry_bounded_by_max_retries(self):
        assert decide_recovery("timeout", attempt=0, max_retries=2) in {"retry", "retry_with_backoff"}
        assert decide_recovery("timeout", attempt=1, max_retries=2) in {"retry", "retry_with_backoff"}
        assert decide_recovery("timeout", attempt=2, max_retries=2) == "abort_scenario"

    def test_session_expiry_never_retried(self):
        assert decide_recovery("session_expiry", attempt=0, max_retries=2) == "abort_scenario"


# ---------------------------------------------------------------------------
# F. Assertion verification
# ---------------------------------------------------------------------------


class TestAssertionVerifier:
    def _assertion(self, **kw):
        class Obj:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        defaults = dict(assertion_id="a1", subject_id="x", operator="unknown", expected_value="", confidence=0.5)
        defaults.update(kw)
        return Obj(**defaults)

    def test_permission_denied_supported_on_failed_action(self):
        assertion = self._assertion(operator="permission_denied")

        class Result:
            success = False
            error = "blocked"

        ar = evaluate_assertion(assertion, investigation_id="inv:1", last_result=Result())
        assert ar.outcome == "supported"
        assert ar.outcome in ASSERTION_OUTCOMES

    def test_permission_allowed_contradicted_on_failed_action(self):
        assertion = self._assertion(operator="permission_allowed")

        class Result:
            success = False
            error = "blocked"

        ar = evaluate_assertion(assertion, investigation_id="inv:1", last_result=Result())
        assert ar.outcome == "contradicted"

    def test_no_signal_is_honestly_inconclusive_never_supported(self):
        assertion = self._assertion(operator="equals", expected_value="5")
        ar = evaluate_assertion(assertion, investigation_id="inv:1")
        assert ar.outcome == "inconclusive"

    def test_remains_unchanged_supported_when_text_identical(self):
        assertion = self._assertion(operator="remains_unchanged")

        class State:
            visible_text_summary = "same text"

        ar = evaluate_assertion(assertion, investigation_id="inv:1", before_state=State(), after_state=State())
        assert ar.outcome == "supported"

    def test_comparison_uses_same_operator_vocabulary(self):
        class Obj:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        comparison = Obj(comparison_id="c1", subject_id="x", comparison_operator="unknown", expected_delta="")
        ar = evaluate_comparison(comparison, investigation_id="inv:1")
        assert ar.outcome == "inconclusive"


# ---------------------------------------------------------------------------
# G. Full execution lifecycle / no duplicate execution
# ---------------------------------------------------------------------------


class TestExecutionLifecycle:
    def test_every_ready_candidate_investigated_exactly_once(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        ready_ids = {c.candidate_id for c in strategy_engine.query_engine.ready()}
        assert ready_ids

        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        engine = AutonomousInvestigationEngine()
        planner = ScriptedPlanner()
        _run_to_exhaustion(engine, planner, run_memory)

        terminal = engine.query_engine.completed() + engine.query_engine.failed() + engine.query_engine.blocked()
        investigated_ids = [r.candidate_id for r in terminal]
        assert len(investigated_ids) == len(set(investigated_ids))  # no duplicate execution
        assert set(investigated_ids) == ready_ids
        # Refined stop-reason vocabulary: "all executable scenarios ran to a
        # terminal outcome" is now distinguished from the generic
        # "no_executable_scenarios" (which stays reserved for "nothing was
        # ever executable at all" — see test_no_executable_scenarios_when_strategy_engine_missing).
        assert engine.stop_reason(run_memory) == "all_executable_scenarios_completed"

    def test_investigation_completes_with_full_step_count(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        engine = AutonomousInvestigationEngine()
        planner = ScriptedPlanner()
        _run_to_exhaustion(engine, planner, run_memory)
        for r in engine.query_engine.completed():
            assert r.steps_executed == r.steps_total


# ---------------------------------------------------------------------------
# H. Evidence collection
# ---------------------------------------------------------------------------


class TestEvidenceCollection:
    def test_evidence_bundle_accumulates_ids_across_steps(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        engine = AutonomousInvestigationEngine()
        planner = ScriptedPlanner()
        _run_to_exhaustion(engine, planner, run_memory)
        completed = engine.query_engine.completed()
        assert completed
        for r in completed:
            assert r.evidence_bundle is not None
            # Only real, distinct evidence ids -- never fabricated duplicates
            assert len(r.evidence_bundle.evidence_ids) == len(set(r.evidence_bundle.evidence_ids))

    def test_latest_evidence_reflects_most_recent_investigation(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        engine = AutonomousInvestigationEngine()
        planner = ScriptedPlanner()
        _run_to_exhaustion(engine, planner, run_memory)
        latest = engine.query_engine.latest_evidence()
        history = engine.query_engine.history()
        assert latest is not None
        assert latest.investigation_id == history[-1].investigation_id


# ---------------------------------------------------------------------------
# I. Knowledge / coverage / confidence updates
# ---------------------------------------------------------------------------


class TestKnowledgeCoverageConfidenceUpdates:
    def test_finalized_investigation_carries_update_records(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        engine = AutonomousInvestigationEngine()
        planner = ScriptedPlanner()
        _run_to_exhaustion(engine, planner, run_memory)
        completed = engine.query_engine.completed()
        assert completed
        for r in completed:
            assert r.knowledge_updates is not None
            assert r.coverage_updates is not None
            assert r.confidence_updates is not None
            assert r.next_goals is not None


# ---------------------------------------------------------------------------
# J. Stop conditions
# ---------------------------------------------------------------------------


class TestStopConditions:
    def test_budget_exceeded(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr, budget=0)
        engine = AutonomousInvestigationEngine()
        assert engine.stop_reason(run_memory) == "budget_exceeded"
        assert engine.next_action(FakePageState(), run_memory, {}, planner=ScriptedPlanner()) is None

    def test_user_cancellation(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        run_memory.stop_reason = "cancelled"
        engine = AutonomousInvestigationEngine()
        assert engine.stop_reason(run_memory) == "user_cancellation"

    def test_no_executable_scenarios_when_strategy_engine_missing(self):
        run_memory = FakeRunMemory(graph=None, goal_engine=None, scenario_engine=None, strategy_engine=None, er=None, ar=None, wr=None)
        engine = AutonomousInvestigationEngine()
        assert engine.stop_reason(run_memory) == "no_executable_scenarios"

    def test_repeated_failures_stops_new_investigations(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        engine = AutonomousInvestigationEngine()
        engine.memory.consecutive_failures = 3
        assert engine.stop_reason(run_memory) == "repeated_failures"


# ---------------------------------------------------------------------------
# K. Memory / controller integration
# ---------------------------------------------------------------------------


class TestMemoryAndController:
    def _run_memory(self):
        from app.agent.memory import RunMemory
        return RunMemory(run_id="r1", start_url="https://example.test")

    def test_degrades_gracefully_with_no_investigation_engine(self):
        mem = self._run_memory()
        assert mem.current_investigation() is None
        assert mem.completed_investigations() == []
        assert mem.failed_investigations() == []
        assert mem.blocked_investigations() == []
        assert mem.paused_investigations() == []
        assert mem.investigation_history() == []
        assert mem.latest_investigation_evidence() is None
        assert mem.investigation_coverage() is None
        assert mem.investigation_confidence() is None
        assert mem.investigation_statistics() is None

    def test_memory_snapshot_includes_investigation_block_when_attached(self):
        mem = self._run_memory()
        mem.investigation_engine = AutonomousInvestigationEngine()
        snap = mem.memory_snapshot()
        assert "autonomous_investigation" in snap
        assert snap["autonomous_investigation"]["total_investigations"] == 0

    def test_run_configuration_defaults_to_disabled(self):
        from app.schemas import RunConfiguration
        assert RunConfiguration().enable_autonomous_investigation is False

    def test_controller_never_invokes_browser_execution_on_hook_failure(self):
        import types
        from app.agent.controller import AgentController

        fake_self = types.SimpleNamespace(memory=types.SimpleNamespace(investigation_engine=object()))
        AgentController._run_autonomous_investigation(
            fake_self, before_state=object(), after_state=object(), action=object(), result=object(),
        )  # must swallow AttributeError, never raise

    def test_controller_wires_investigation_after_qa_strategy(self):
        import inspect
        from app.agent import controller as controller_module

        source = inspect.getsource(controller_module)
        assert "self._run_qa_strategy()" in source
        assert "self._run_autonomous_investigation(" in source
        assert source.index("self._run_qa_strategy()") < source.index("self._run_autonomous_investigation(")

    def test_no_action_executor_or_browser_adapter_reference_in_package(self):
        package_dir = BACKEND / "app" / "intelligence" / "autonomous_investigation"
        for path in package_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "ActionExecutor(" not in text
            assert "import ActionExecutor" not in text
            assert "BrowserAdapter(" not in text
            assert "import BrowserAdapter" not in text
            assert "import playwright" not in text.lower()
            assert "playwright.sync_api" not in text.lower()
            assert "page.locator" not in text.lower()


# ---------------------------------------------------------------------------
# L. Query API
# ---------------------------------------------------------------------------


class TestQueryAPI:
    def test_query_methods_partition_by_outcome(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        engine = AutonomousInvestigationEngine()
        planner = ScriptedPlanner()
        _run_to_exhaustion(engine, planner, run_memory)

        completed_ids = {r.investigation_id for r in engine.query_engine.completed()}
        failed_ids = {r.investigation_id for r in engine.query_engine.failed()}
        blocked_ids = {r.investigation_id for r in engine.query_engine.blocked()}
        assert not (completed_ids & failed_ids)
        assert not (completed_ids & blocked_ids)
        history_ids = {r.investigation_id for r in engine.query_engine.history()}
        assert completed_ids | failed_ids | blocked_ids <= history_ids

    def test_current_investigation_none_when_idle(self):
        engine = AutonomousInvestigationEngine()
        assert engine.query_engine.current_investigation() is None

    def test_statistics_totals_match_history_length(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        engine = AutonomousInvestigationEngine()
        planner = ScriptedPlanner()
        _run_to_exhaustion(engine, planner, run_memory)
        stats = engine.statistics()
        assert stats.total_investigations == len(engine.query_engine.history())


# ---------------------------------------------------------------------------
# M. Determinism / idempotency
# ---------------------------------------------------------------------------


class TestDeterminismAndIdempotency:
    def test_investigation_id_deterministic_from_candidate_and_attempt(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        engine = AutonomousInvestigationEngine()
        planner = ScriptedPlanner()
        _run_to_exhaustion(engine, planner, run_memory)
        for r in engine.query_engine.history():
            assert r.investigation_id.startswith(f"investigation:{r.candidate_id}:")

    def test_repeated_query_calls_are_stable(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
        run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
        engine = AutonomousInvestigationEngine()
        planner = ScriptedPlanner()
        _run_to_exhaustion(engine, planner, run_memory)
        first = [r.investigation_id for r in engine.query_engine.history()]
        second = [r.investigation_id for r in engine.query_engine.history()]
        assert first == second

    def test_two_independent_engines_produce_identical_investigation_sets(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)

        def _run_once():
            rm = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
            eng = AutonomousInvestigationEngine()
            _run_to_exhaustion(eng, ScriptedPlanner(), rm)
            return {r.investigation_id for r in eng.query_engine.history()}

        assert _run_once() == _run_once()


# ---------------------------------------------------------------------------
# N. Neutrality
# ---------------------------------------------------------------------------

FORBIDDEN_WORDS = {
    "invoice", "customer", "job", "ticket", "order", "product", "cart", "checkout",
    "employee", "vehicle", "haulvana", "serviceflow", "saucedemo", "insightboard",
}


class TestNeutrality:
    def test_no_hardcoded_business_vocabulary(self):
        package_dir = BACKEND / "app" / "intelligence" / "autonomous_investigation"
        offenders = []
        for path in sorted(package_dir.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            docstring_nodes = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                    body = getattr(node, "body", [])
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                        docstring_nodes.add(id(body[0].value))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_nodes:
                    words = set(re.findall(r"[a-z]+", node.value.lower()))
                    hit = words & FORBIDDEN_WORDS
                    if hit:
                        offenders.append((path.name, node.value, hit))
        assert not offenders, f"Hardcoded business vocabulary found: {offenders}"

    def test_execution_states_have_no_application_specific_members(self):
        for state in EXECUTION_STATES:
            for word in FORBIDDEN_WORDS:
                assert word not in state
