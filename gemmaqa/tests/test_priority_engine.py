"""Evidence-first exploration priority engine (app/agent/priority_engine.py).

Covers: sidebar vs footer, header vs social link, active workflow vs
unrelated navigation, current-module tabs vs global navigation, external-link
penalties, repetition penalties, unknown-component investigation, stale
candidate rejection, safety override, deterministic tie-breaking — plus the
explainable decision trace, the Gemma advisory tie-break, and the
Executor's one-action-per-call invariant.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest

from app.agent.auth_strategy import AuthenticationStrategy  # noqa: E402
from app.agent.frontier import FrontierCandidate  # noqa: E402
from app.agent.goals import ExplorationGoal  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.agent.planner import Planner  # noqa: E402
from app.agent.priority_engine import PriorityEngine, apply_advisory_order  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.schemas import PageState  # noqa: E402
from app.utils import exploration_trace  # noqa: E402
from app.utils.ids import new_id  # noqa: E402

URL = "https://example.com/app"


def _memory(**overrides) -> RunMemory:
    memory = RunMemory(run_id=new_id(), start_url=URL)
    memory.remaining_action_budget = 30
    for key, value in overrides.items():
        setattr(memory, key, value)
    return memory


def _page(state_fingerprint: str = "fp1") -> PageState:
    return PageState(page_id=new_id(), url=URL, title="App", state_fingerprint=state_fingerprint)


def _cand(candidate_id: str, candidate_type: str = "navigation_control", **overrides) -> FrontierCandidate:
    base = dict(
        candidate_id=candidate_id,
        candidate_type=candidate_type,
        action="click",
        element_id=candidate_id,
        state_fingerprint="fp1",
        source_state_fingerprint="fp1",
    )
    base.update(overrides)
    return FrontierCandidate(**base)


def _decision(candidates, *, memory=None, page=None, active_goal=None):
    return PriorityEngine().build_decision(
        candidates, memory=memory or _memory(), page=page or _page(), active_goal=active_goal
    )


def _score_of(decision, candidate_id: str) -> float:
    return decision.scores_by_id[candidate_id].total_score


# ---------------------------------------------------------------------------
# Sidebar vs footer
# ---------------------------------------------------------------------------


def test_sidebar_outranks_footer():
    sidebar = _cand("el_sidebar", "open_navigation_item", navigation_centrality=0.75, semantic_type="sidebar")
    footer = _cand("el_footer", "verify_internal_link", navigation_centrality=0.0, semantic_type="internal_link")
    decision = _decision([sidebar, footer])
    assert decision.ordered_candidates[0].candidate_id == "el_sidebar"
    assert _score_of(decision, "el_sidebar") > _score_of(decision, "el_footer")


# ---------------------------------------------------------------------------
# Header vs social link
# ---------------------------------------------------------------------------


def test_header_nav_outranks_social_link():
    header = _cand(
        "el_header_home", "open_navigation_item", navigation_centrality=0.9, actual_label="Home", semantic_type="primary_navigation"
    )
    social = _cand(
        "el_facebook",
        "verify_external_link",
        actual_label="Facebook",
        url="https://facebook.com/example",
        target_url="https://facebook.com/example",
        external_navigation_cost=0.8,
    )
    decision = _decision([header, social])
    assert decision.ordered_candidates[0].candidate_id == "el_header_home"
    social_score = decision.scores_by_id["el_facebook"]
    social_factor_names = {f.name for f in social_score.negative_factors if f.contribution < 0}
    assert "social_link_penalty" in social_factor_names
    assert "external_domain_penalty" in social_factor_names


# ---------------------------------------------------------------------------
# Active workflow vs unrelated navigation
# ---------------------------------------------------------------------------


def test_active_workflow_continuity_outranks_unrelated_navigation():
    checkout = _cand("el_checkout", "continue_form_workflow", business_relevance=0.5)
    settings = _cand("el_settings", "navigation_control", actual_label="Settings", business_relevance=0.9, novelty=1.0)
    decision = _decision([checkout, settings])
    assert decision.ordered_candidates[0].candidate_id == "el_checkout"
    top_factor = decision.scores_by_id["el_checkout"].top_positive_factor()
    assert top_factor.name == "active_workflow_continuity"


# ---------------------------------------------------------------------------
# Current-module tabs vs global navigation
# ---------------------------------------------------------------------------


def test_current_module_tab_outranks_a_different_modules_global_nav_link():
    memory = _memory()
    tab = _cand("el_customers_tab", "select_tab", module_id="mod_customers", novelty=1.0, confidence=0.9)
    other_module_link = _cand(
        "el_jobs_link", "open_navigation_item", module_id="mod_jobs", novelty=1.0, confidence=0.9, coverage_value=0.3
    )

    class _FakeAppPage:
        module_id = "mod_customers"

    class _FakeModel:
        @staticmethod
        def page_by_url(url):
            return _FakeAppPage()

    class _FakeAppStore:
        model = _FakeModel()

    memory.app_store = _FakeAppStore()
    decision = _decision([tab, other_module_link], memory=memory)
    assert decision.ordered_candidates[0].candidate_id == "el_customers_tab"
    assert decision.scores_by_id["el_customers_tab"].total_score > decision.scores_by_id["el_jobs_link"].total_score


# ---------------------------------------------------------------------------
# External-link penalties
# ---------------------------------------------------------------------------


def test_external_link_penalty_applied_and_lowers_score_below_internal_equivalent():
    internal = _cand("el_internal", "verify_internal_link", actual_label="About us")
    external = _cand("el_external", "verify_external_link", actual_label="Partner site", external_navigation_cost=0.8)
    decision = _decision([internal, external])
    assert _score_of(decision, "el_internal") > _score_of(decision, "el_external")
    ext_negatives = {f.name for f in decision.scores_by_id["el_external"].negative_factors if f.contribution < 0}
    assert "external_domain_penalty" in ext_negatives


# ---------------------------------------------------------------------------
# Repetition penalties
# ---------------------------------------------------------------------------


def test_repeated_low_information_row_eventually_loses_to_unexplored_candidate():
    fresh_row = _cand(
        "el_row_1", "open_table_row", already_attempted_count=0, novelty=1.0, repetition_penalty=0.0,
        expected_information_gain=0.6,
    )
    decision_fresh = _decision([fresh_row])
    fresh_score = _score_of(decision_fresh, "el_row_1")

    repeated_row = _cand(
        "el_row_1", "open_table_row", already_attempted_count=4, novelty=0.0, repetition_penalty=1.0,
        expected_information_gain=0.3,
    )
    unexplored_module = _cand(
        "el_new_module", "open_navigation_item", coverage_value=1.0, novelty=1.0, confidence=0.9
    )
    decision_later = _decision([repeated_row, unexplored_module])
    repeated_score = _score_of(decision_later, "el_row_1")

    assert repeated_score < fresh_score, "repeated attempts must measurably reduce the score"
    assert decision_later.ordered_candidates[0].candidate_id == "el_new_module"
    repetition_factor = next(
        f for f in decision_later.scores_by_id["el_row_1"].negative_factors if f.name == "repetition_penalty"
    )
    assert repetition_factor.contribution < 0


# ---------------------------------------------------------------------------
# Unknown-component investigation
# ---------------------------------------------------------------------------


def test_unknown_component_is_scored_and_survives_as_a_safe_candidate():
    unknown = _cand("el_mystery", "inspect_unknown_component", confidence=0.3, reversibility="reversible")
    decision = _decision([unknown])
    assert decision.ordered_candidates == [unknown]
    score = decision.scores_by_id["el_mystery"]
    assert score.safety_rejected is False
    assert score.total_score > 0


# ---------------------------------------------------------------------------
# Stale candidate rejection
# ---------------------------------------------------------------------------


def test_stale_candidate_is_removed_before_scoring():
    fresh = _cand("el_fresh", state_fingerprint="fp_current", source_state_fingerprint="fp_current")
    stale = _cand("el_stale", state_fingerprint="fp_old", source_state_fingerprint="fp_old")
    decision = _decision([fresh, stale], page=_page("fp_current"))
    assert [c.candidate_id for c in decision.ordered_candidates] == ["el_fresh"]
    assert "el_stale" not in decision.scores_by_id
    reasons = {r["candidate_id"]: r["reason"] for r in decision.rejected}
    assert "stale" in reasons["el_stale"]


# ---------------------------------------------------------------------------
# Safety override
# ---------------------------------------------------------------------------


def test_destructive_candidate_is_rejected_regardless_of_score():
    destructive = _cand(
        "el_delete_all",
        "navigation_control",
        safety_class="destructive",
        destructive_risk=True,
        novelty=1.0,
        confidence=1.0,
        business_relevance=1.0,
        expected_information_gain=1.0,
    )
    safe = _cand("el_safe", "navigation_control", novelty=0.2, confidence=0.5)
    decision = _decision([destructive, safe])
    assert [c.candidate_id for c in decision.ordered_candidates] == ["el_safe"]
    rejected_ids = {r["candidate_id"] for r in decision.rejected}
    assert "el_delete_all" in rejected_ids
    assert decision.scores_by_id["el_delete_all"].safety_rejected is True


def test_financial_candidate_is_rejected_regardless_of_score():
    financial = _cand("el_pay_now", "navigation_control", safety_class="financial", novelty=1.0, confidence=1.0)
    safe = _cand("el_safe", "navigation_control")
    decision = _decision([financial, safe])
    assert all(c.candidate_id != "el_pay_now" for c in decision.ordered_candidates)


# ---------------------------------------------------------------------------
# Deterministic tie-breaking
# ---------------------------------------------------------------------------


def test_equal_score_candidates_break_ties_by_candidate_id_ascending():
    a = _cand("el_b_candidate")
    b = _cand("el_a_candidate")
    c = _cand("el_c_candidate")
    decision = _decision([a, b, c])
    scores = {decision.scores_by_id[c.candidate_id].total_score for c in [a, b, c]}
    assert len(scores) == 1, "fixture must actually produce equal scores to test the tie-break"
    assert [c.candidate_id for c in decision.ordered_candidates] == [
        "el_a_candidate",
        "el_b_candidate",
        "el_c_candidate",
    ]


def test_tie_break_is_reproducible_across_repeated_calls():
    candidates = [_cand(f"el_{i}") for i in (3, 1, 2)]
    first = [c.candidate_id for c in _decision(candidates).ordered_candidates]
    second = [c.candidate_id for c in _decision(candidates).ordered_candidates]
    assert first == second == ["el_1", "el_2", "el_3"]


# ---------------------------------------------------------------------------
# Near-tie detection + advisory Gemma re-order (Planner-level)
# ---------------------------------------------------------------------------


def test_near_tied_top_group_detected_for_close_scores():
    a = _cand("el_a")
    b = _cand("el_b")
    far = _cand("el_far", coverage_value=0.0, novelty=0.0, confidence=0.0, expected_information_gain=0.0, business_relevance=0.0)
    decision = _decision([a, b, far])
    near_ids = {c.candidate_id for c in decision.near_tied_top}
    assert {"el_a", "el_b"} <= near_ids
    assert "el_far" not in near_ids


def test_apply_advisory_order_reorders_only_the_near_tied_prefix():
    a = _cand("el_a")
    b = _cand("el_b")
    far = _cand("el_far", coverage_value=0.0, novelty=0.0, confidence=0.0, expected_information_gain=0.0, business_relevance=0.0)
    decision = _decision([a, b, far])
    assert [c.candidate_id for c in decision.ordered_candidates][:2] == ["el_a", "el_b"]

    reordered = apply_advisory_order(decision, ["el_b", "el_a"])
    assert [c.candidate_id for c in reordered.ordered_candidates] == ["el_b", "el_a", "el_far"]
    assert reordered.gemma_advisory_applied is True


def test_apply_advisory_order_cannot_invent_or_elevate_an_outside_candidate():
    a = _cand("el_a")
    b = _cand("el_b")
    far = _cand("el_far", coverage_value=0.0, novelty=0.0, confidence=0.0, expected_information_gain=0.0, business_relevance=0.0)
    decision = _decision([a, b, far])
    # "el_far" is not in the near-tied group and "el_ghost" doesn't exist at all.
    reordered = apply_advisory_order(decision, ["el_far", "el_ghost", "el_b", "el_a"])
    ids = [c.candidate_id for c in reordered.ordered_candidates]
    assert ids[-1] == "el_far", "a candidate outside the tie must never be pulled ahead of the tied group"
    assert "el_ghost" not in ids


async def test_planner_gemma_tie_break_reorders_a_genuine_near_tie():
    """A scripted advisory response that reverses the near-tied pair must be
    applied — but only to that pair; the clearly-worse "el_far" candidate
    must stay last regardless of what the advisory response claims."""
    a = _cand("el_a")
    b = _cand("el_b")
    far = _cand(
        "el_far", coverage_value=0.0, novelty=0.0, confidence=0.0, expected_information_gain=0.0, business_relevance=0.0
    )
    decision = _decision([a, b, far])
    assert [c.candidate_id for c in decision.ordered_candidates][:2] == ["el_a", "el_b"]

    class ScriptedGemma(MockGemmaProvider):
        async def rank_candidates(self, candidates, *, context=None):
            return list(reversed([c["candidate_id"] for c in candidates]))

    planner = Planner(ScriptedGemma())
    adjusted = await planner._apply_gemma_tie_break(decision, {})
    assert [c.candidate_id for c in adjusted.ordered_candidates] == ["el_b", "el_a", "el_far"]
    assert adjusted.gemma_advisory_applied is True


async def test_planner_gemma_tie_break_is_a_noop_when_nothing_is_tied():
    a = _cand("el_a")
    far = _cand(
        "el_far", coverage_value=0.0, novelty=0.0, confidence=0.0, expected_information_gain=0.0, business_relevance=0.0
    )
    decision = _decision([a, far])
    assert len(decision.near_tied_top) < 2

    planner = Planner(MockGemmaProvider())
    unchanged = await planner._apply_gemma_tie_break(decision, {})
    assert unchanged is decision


# ---------------------------------------------------------------------------
# Explainable decision trace
# ---------------------------------------------------------------------------


def test_decision_trace_contains_scores_goal_rejections_and_selection_reason(monkeypatch):
    monkeypatch.setenv("GEMMAQA_EXPLORATION_TRACE", "true")
    memory = _memory()
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = True
    page = _page("fp1")

    planner = Planner(MockGemmaProvider())
    with patch.object(exploration_trace, "logger") as mock_logger:
        planner._select_auth_candidate(page, memory, {})
        assert mock_logger.info.called
        call = next(c for c in mock_logger.info.call_args_list if c.args[1] == "iteration.plan")
        fields = call.args[2]

    for key in (
        "candidate_scores",
        "rejected_candidates",
        "active_goal",
        "selected_candidate_id",
        "selected_action",
        "selection_reason",
        "gemma_advisory_applied",
    ):
        assert key in fields, f"missing trace field: {key}"


# ---------------------------------------------------------------------------
# Executor: exactly one action per call
# ---------------------------------------------------------------------------


async def test_executor_performs_exactly_one_adapter_action_per_execute_call():
    from app.browser.executor import ActionExecutor
    from app.browser.locators import LocatorRegistry
    from app.schemas import ActionType, BrowserAction, RiskLevel, ActionCategory

    click_calls = []

    class FakeCapabilities:
        click = True
        screenshots = False

        def __getattr__(self, name):
            return True

    class FakeAdapter:
        name = "fake"

        async def get_current_url(self):
            return URL

        async def get_console_events(self):
            return []

        async def get_network_events(self):
            return []

        def capabilities(self):
            return FakeCapabilities()

        async def resolve_target(self, target):
            return None

        async def click_target(self, target):
            click_calls.append(target)

        async def wait(self, ms):
            return None

        async def wait_for_page_stable(self):
            return None

    executor = ActionExecutor("run1", registry=LocatorRegistry(), adapter=FakeAdapter())
    action = BrowserAction(
        action=ActionType.CLICK,
        element_id="el_1",
        reason="test",
        expected_result="test",
        risk=RiskLevel.LOW,
        category=ActionCategory.EXPLORATION,
    )
    result = await executor.execute(action=action, page_state=_page("fp1"), capture_evidence=False)
    assert result.success is True
    assert len(click_calls) == 1, "exactly one browser action must be performed per execute() call"
