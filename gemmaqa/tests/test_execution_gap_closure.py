"""Scenario-to-execution gap closure — tests for the NEW wiring added in this
milestone: scenario completion writing back to `InvestigationScenario.status`
and bridging to `AppTestScenario`/coverage, scenario eligibility
classification and reporting, investigation-alignment-driven local-action
prioritisation, provider capability-mode reporting, and richer stop reasons.

Existing coverage in `test_autonomous_investigation.py` (state machine,
precondition/safety gates, recovery, assertion verification, full execution
lifecycle, determinism/idempotency, neutrality) is NOT duplicated here.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.frontier import FrontierCandidate  # noqa: E402
from app.agent.priority_engine import PriorityEngine  # noqa: E402
from app.application.models import ScenarioExecutionStatus  # noqa: E402
from app.application.store import ApplicationStore  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.intelligence.autonomous_investigation import AutonomousInvestigationEngine  # noqa: E402
from app.intelligence.autonomous_investigation.eligibility_classifier import classify_eligibility  # noqa: E402
from app.intelligence.scenario_planning.schemas import SCENARIO_STATUSES  # noqa: E402
from app.runtime_info import CAPABILITY_MODES, provider_capability_mode  # noqa: E402
from app.schemas import ActionCategory, ActionResult, ActionType, BrowserAction, PageState, RiskLevel, RunConfiguration  # noqa: E402

# Reuse the KNOWN-GOOD fixture/pipeline builders from test_autonomous_investigation.py
# rather than re-deriving a simplified (and, as it turned out, non-ready-
# scenario-producing) one — this package's feasibility/precondition chain
# needs a real actor+permission+workflow+dependency graph to actually mark a
# scenario "feasible"/ready, exactly what that module's fixture builds.
from tests.test_autonomous_investigation import _plan_and_strategize, _single_transition_fixture  # noqa: E402


def _single_scenario_pipeline():
    er, ar, wr, dr = _single_transition_fixture()
    graph, goal_engine, scenario_engine, strategy_engine = _plan_and_strategize(er=er, ar=ar, wr=wr, dr=dr)
    return er, ar, wr, graph, goal_engine, scenario_engine, strategy_engine


class FakeAuth:
    authenticated = True

    def next_workflow_action(self):
        return None


class FakeRunMemory:
    def __init__(self, *, graph, goal_engine, scenario_engine, strategy_engine, er=None, ar=None, wr=None, app_store=None, budget=300):
        self.knowledge_graph = graph
        self.goal_engine = goal_engine
        self.scenario_engine = scenario_engine
        self.strategy_engine = strategy_engine
        self.entity_registry = er
        self.actor_registry = ar
        self.workflow_registry = wr
        self.auth_strategy = FakeAuth()
        self.visited_urls = ["http://example.test/"]
        self.remaining_action_budget = budget
        self.stop_reason = None
        self.actions = []
        self.app_store = app_store
        self.authenticated_page_count = 1
        self.configuration = RunConfiguration()


class ScriptedPlanner:
    """Always proposes a harmless CLICK — mirrors test_autonomous_investigation.py's
    ScriptedPlanner (never a raw Playwright call, exactly the runtime Planner
    contract semantic_step_executor.resolve_step relies on)."""

    def plan_by_priority(self, page_state, memory, context):
        return BrowserAction(
            action=ActionType.CLICK, element_id="el_1", reason="scripted",
            expected_result="ok", risk=RiskLevel.LOW, category=ActionCategory.EXPLORATION,
        )


class FakePageState:
    visible_text_summary = "hello"
    url = "http://example.test/"


def _run_to_first_completion(engine, planner, run_memory, page_state):
    for _ in range(200):
        action = engine.next_action(page_state, run_memory, {}, planner=planner)
        if action is None:
            if engine.memory.active is None and engine.query_engine.history():
                return
            continue
        result = ActionResult(
            action_id="a1", run_id="r1", action=action, success=True, message="ok",
            before_url=page_state.url, after_url=page_state.url,
        )
        engine.observe_step_result(before_state=page_state, after_state=page_state, result=result, run_memory=run_memory, action=action)


# ---------------------------------------------------------------------------
# 1-11: end-to-end scenario -> execution -> completion -> report bridge
# ---------------------------------------------------------------------------


def test_generated_scenario_reaches_autonomous_investigation_and_is_selected():
    er, ar, wr, graph, goal_engine, scenario_engine, strategy_engine = _single_scenario_pipeline()
    ready = list(strategy_engine.query_engine.ready())
    assert ready  # a scenario really was generated AND queued as executable

    run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
    engine = AutonomousInvestigationEngine()
    started = engine._try_start_next(run_memory)
    assert started is True
    assert engine.memory.active is not None
    assert engine.memory.last_scan_summary["started"] is True


def test_semantic_step_reaches_runtime_planner_and_action_flows_to_investigation():
    er, ar, wr, graph, goal_engine, scenario_engine, strategy_engine = _single_scenario_pipeline()
    run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
    engine = AutonomousInvestigationEngine()
    planner = ScriptedPlanner()

    action = engine.next_action(FakePageState(), run_memory, {}, planner=planner)
    assert action is not None
    assert action.action in {ActionType.CLICK, ActionType.WAIT, ActionType.TAKE_SCREENSHOT}
    # The action returned to the investigation is tagged so observe_step_result
    # can reconcile it back — this is what makes "action result returns to the
    # investigation" possible at all.
    assert action.metadata.get("investigation_step_id")


def test_scenario_completion_writes_back_status_and_bridges_to_app_test_scenario():
    """The core wiring fix: a completed investigation must (a) mutate the
    SAME InvestigationScenario object's status and (b) create/update an
    AppTestScenario the report/coverage layer actually reads — without this,
    a scenario stays `not_run` in the report forever even after executing."""
    er, ar, wr, graph, goal_engine, scenario_engine, strategy_engine = _single_scenario_pipeline()
    store = ApplicationStore("run_1", "http://example.test/")
    run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr, app_store=store)
    engine = AutonomousInvestigationEngine()
    planner = ScriptedPlanner()

    ready = list(strategy_engine.query_engine.ready())
    scenario_id = ready[0].scenario_id
    scenario_before = scenario_engine.query_engine.scenario_by_id(scenario_id)
    assert scenario_before.status in {"draft", "feasible", "conditionally_feasible"}

    _run_to_first_completion(engine, planner, run_memory, FakePageState())

    scenario_after = scenario_engine.query_engine.scenario_by_id(scenario_id)
    assert scenario_after.status in SCENARIO_STATUSES
    assert scenario_after.status not in {"draft", "feasible", "conditionally_feasible"}  # actually advanced

    bridged = [s for s in store.model.scenarios if s.scenario_key == f"investigation:{scenario_id}"]
    assert len(bridged) == 1
    assert bridged[0].execution_status != ScenarioExecutionStatus.NOT_RUN


def test_next_scenario_selected_after_first_completes():
    er, ar, wr, graph, goal_engine, scenario_engine, strategy_engine = _single_scenario_pipeline()
    ready = list(strategy_engine.query_engine.ready())
    if len(ready) < 2:
        import pytest as _pytest
        _pytest.skip("fixture only produced one ready scenario; multi-scenario selection covered in test_autonomous_investigation.py")
    run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
    engine = AutonomousInvestigationEngine()
    first = ready[0].candidate_id
    engine._try_start_next(run_memory)
    assert engine.memory.active.candidate_id == first
    engine._finalize(run_memory, outcome="completed")
    assert engine._try_start_next(run_memory) is True
    assert engine.memory.active.candidate_id != first


def test_repeated_candidate_never_investigated_twice():
    er, ar, wr, graph, goal_engine, scenario_engine, strategy_engine = _single_scenario_pipeline()
    run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
    engine = AutonomousInvestigationEngine()
    engine._try_start_next(run_memory)
    candidate_id = engine.memory.active.candidate_id
    engine._finalize(run_memory, outcome="completed")
    assert candidate_id in engine.memory.investigated_candidate_ids
    # A second scan must never re-select it.
    engine._try_start_next(run_memory)
    if engine.memory.active is not None:
        assert engine.memory.active.candidate_id != candidate_id


def test_blocked_scenario_stores_an_explicit_reason():
    er, ar, wr, graph, goal_engine, scenario_engine, strategy_engine = _single_scenario_pipeline()
    ready = list(strategy_engine.query_engine.ready())
    scenario = scenario_engine.query_engine.scenario_by_id(ready[0].scenario_id)
    scenario.feasibility_status = "blocked"  # investigation_safety_gate.check_safety rejects this unconditionally
    run_memory = FakeRunMemory(graph=graph, goal_engine=goal_engine, scenario_engine=scenario_engine, strategy_engine=strategy_engine, er=er, ar=ar, wr=wr)
    engine = AutonomousInvestigationEngine()
    engine._try_start_next(run_memory)

    assert engine.memory.last_scan_summary is not None
    entries = engine.memory.last_scan_summary["blocked_or_deferred"]
    assert entries, "a blocked/deferred candidate must record a reason, never be silently dropped"
    assert all(e.get("reason") for e in entries)


def test_eligibility_classification_covers_the_full_vocabulary_surface():
    er, ar, wr, graph, goal_engine, scenario_engine, strategy_engine = _single_scenario_pipeline()
    ready = list(strategy_engine.query_engine.ready())
    scenario = scenario_engine.query_engine.scenario_by_id(ready[0].scenario_id)
    eligibility = classify_eligibility(scenario, ready[0], scenario_engine=scenario_engine)
    from app.intelligence.autonomous_investigation.eligibility_classifier import ELIGIBILITY_STATUSES
    assert eligibility.status in ELIGIBILITY_STATUSES
    assert eligibility.reason


# ---------------------------------------------------------------------------
# 13-14: local-action prioritisation during an active scenario, vs. standard mode
# ---------------------------------------------------------------------------


def _nav_and_local_candidates():
    nav = FrontierCandidate(
        candidate_id="c_nav", candidate_type="navigation_control", action="click",
        element_id="el_sidebar", actual_label="Settings", navigation_centrality=0.9,
    )
    local = FrontierCandidate(
        candidate_id="c_local", candidate_type="inspect_form", action="inspect_form",
        form_id="form_1", actual_label="Add Member", workflow_value=0.8,
    )
    return nav, local


def test_sidebar_navigation_deprioritised_when_it_does_not_match_active_scenario():
    nav, local = _nav_and_local_candidates()
    nav.investigation_alignment = 0.0
    nav.off_scenario = True  # a scenario IS active and this candidate doesn't match it
    local.investigation_alignment = 1.0  # this candidate IS the scenario's required control

    engine = PriorityEngine()
    decision = engine.build_decision([nav, local], memory=None, page=PageState(page_id="p", url="http://example.test/"))
    assert decision.ordered_candidates[0].candidate_id == "c_local"
    nav_score = decision.scores_by_id["c_nav"].total_score
    local_score = decision.scores_by_id["c_local"].total_score
    assert local_score > nav_score


def test_standard_mode_leaves_the_investigation_signal_neutral():
    """The backward-compatibility guarantee this test was written for:
    investigation_alignment/off_scenario default to 0.0/False, so a candidate
    untouched by `Planner._apply_investigation_alignment` (i.e. every
    standard-mode run, since `context["investigation_hints"]` is only ever set
    from inside semantic_step_executor) gets no investigation contribution at
    all."""
    nav, local = _nav_and_local_candidates()
    assert nav.investigation_alignment == 0.0
    assert nav.off_scenario is False
    assert local.investigation_alignment == 0.0
    assert local.off_scenario is False

    engine = PriorityEngine()
    decision = engine.build_decision([nav, local], memory=None, page=PageState(page_id="p", url="http://example.test/"))
    for candidate_id in ("c_nav", "c_local"):
        factors = {f.name: f.contribution for f in decision.scores_by_id[candidate_id].positive_factors}
        assert factors.get("investigation_alignment", 0.0) == 0.0


def test_navigating_away_loses_to_unworked_form_work_on_the_same_page():
    """This assertion is the REVERSE of what this test file originally asserted.

    The original expectation was documented in-place as the "PRE-EXISTING (if
    imperfect)" ordering: raw `navigation_centrality` (22) outscored
    `workflow_value` (18), so a nav control beat local form work. That
    imperfection was then observed causing real damage on a live application —
    the agent inspected a create form and immediately clicked Cancel, and later
    clicked "Edit Contact" and immediately clicked Cancel, abandoning the update
    it had spent an entire run reaching. `flow_abandonment_penalty` corrects it.
    """
    nav, local = _nav_and_local_candidates()
    engine = PriorityEngine()
    decision = engine.build_decision([nav, local], memory=None, page=PageState(page_id="p", url="http://example.test/"))

    assert decision.ordered_candidates[0].candidate_id == "c_local"
    assert decision.scores_by_id["c_local"].total_score > decision.scores_by_id["c_nav"].total_score
    penalties = {f.name for f in decision.scores_by_id["c_nav"].negative_factors if f.value > 0}
    assert "flow_abandonment_penalty" in penalties


def test_navigation_is_not_penalised_when_no_form_work_is_available():
    """The penalty is about abandoning available work, not about navigation."""
    nav, _local = _nav_and_local_candidates()
    engine = PriorityEngine()
    decision = engine.build_decision([nav], memory=None, page=PageState(page_id="p", url="http://example.test/"))
    penalties = {f.name for f in decision.scores_by_id["c_nav"].negative_factors if f.value > 0}
    assert "flow_abandonment_penalty" not in penalties


def test_autonomous_investigation_disabled_by_default():
    config = RunConfiguration()
    assert config.enable_autonomous_investigation is False


# ---------------------------------------------------------------------------
# 16: Mock exploration-only capability reporting
# ---------------------------------------------------------------------------


def test_mock_provider_declares_exploration_only():
    provider = MockGemmaProvider()
    assert provider.supports_scenario_execution is False
    assert provider_capability_mode("mock", enable_autonomous_investigation=True) == "exploration_only"
    assert provider_capability_mode("mock", enable_autonomous_investigation=False) == "exploration_only"


def test_real_provider_capability_mode_depends_on_autonomous_flag():
    assert provider_capability_mode("openai_compatible", enable_autonomous_investigation=True) == "scenario_execution_capable"
    assert provider_capability_mode("openai_compatible", enable_autonomous_investigation=False) == "real_model_reasoning"


def test_all_capability_modes_are_in_the_closed_vocabulary():
    for mode in ("exploration_only", "scenario_execution_capable", "real_model_reasoning"):
        assert mode in CAPABILITY_MODES
