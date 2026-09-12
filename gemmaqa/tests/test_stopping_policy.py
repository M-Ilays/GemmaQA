"""Intelligent stopping policy (Phase 6): a navigation loop or no-progress streak
must not immediately end the run while an alternative (frontier candidate, pending
goal, or unexplored URL) exists — it should backtrack (deprioritize the current
goal) and continue, only truly stopping once no alternative remains."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.goals import ExplorationGoal  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.agent.planner import Planner  # noqa: E402
from app.schemas import ActionResult, ActionType, BrowserAction, RiskLevel, RunConfiguration  # noqa: E402
from app.utils.ids import new_id  # noqa: E402


def _bounce_memory() -> RunMemory:
    """A RunMemory whose recent actions form an A-B-A-B navigation loop."""
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.configuration = RunConfiguration(max_actions=50, max_pages=20)
    memory.bootstrap_budgets(memory.configuration)
    for url in ["http://x/a", "http://x/b", "http://x/a", "http://x/b"]:
        result = ActionResult(
            action_id=new_id(),
            run_id=memory.run_id,
            action=BrowserAction(
                action=ActionType.CLICK, reason="nav", expected_result="ok", risk=RiskLevel.LOW
            ),
            success=True,
            before_url="http://x",
            after_url=url,
        )
        memory.remember_action(result, before_fingerprint="fp", made_progress=True)
    assert memory.detect_navigation_loop() is True
    return memory


def _fake_candidate(status: str = "available"):
    class _C:
        pass

    c = _C()
    c.status = status
    return c


def test_contact_detail_edit_cycle_is_not_a_navigation_loop() -> None:
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.configuration = RunConfiguration(max_actions=50, max_pages=20)
    memory.bootstrap_budgets(memory.configuration)
    urls = [
        "http://x/editContact",
        "http://x/contactDetails",
        "http://x/editContact",
        "http://x/contactDetails",
    ]
    for url in urls:
        result = ActionResult(
            action_id=new_id(),
            run_id=memory.run_id,
            action=BrowserAction(
                action=ActionType.CLICK, reason="nav", expected_result="ok", risk=RiskLevel.LOW
            ),
            success=True,
            before_url="http://x",
            after_url=url,
        )
        memory.remember_action(result, before_fingerprint="fp", made_progress=True)
    assert memory.detect_navigation_loop() is False


def test_loop_backtracks_when_frontier_candidate_still_available():
    memory = _bounce_memory()
    stop, reason = memory.should_stop(5, build_frontier=lambda: [_fake_candidate("available")])
    assert stop is False
    assert reason is None
    assert memory.loop_recovery_count == 1


def test_loop_backtracks_when_pending_goal_exists_even_without_frontier():
    memory = _bounce_memory()
    memory.goals.append(
        ExplorationGoal(goal_id=new_id(), goal_type="inspect_form", title="t", status="proposed")
    )
    stop, reason = memory.should_stop(5)
    assert stop is False
    assert reason is None


def test_loop_backtracks_when_unexplored_urls_remain():
    memory = _bounce_memory()
    memory.unexplored_urls.append("http://x/c")
    stop, reason = memory.should_stop(5)
    assert stop is False
    assert reason is None


def test_backtrack_deprioritizes_the_active_goal():
    memory = _bounce_memory()
    goal = ExplorationGoal(
        goal_id=new_id(), goal_type="discover_navigation_region", title="t", status="active", priority=50
    )
    memory.goals.append(goal)
    stop, _ = memory.should_stop(5, build_frontier=lambda: [_fake_candidate("available")])
    assert stop is False
    assert goal.status == "deferred"
    assert goal.priority > 50
    assert goal.evidence


def test_loop_recovery_cap_eventually_stops_with_persistent_navigation_loop():
    memory = _bounce_memory()
    build_frontier = lambda: [_fake_candidate("available")]  # noqa: E731
    for _ in range(memory._max_loop_recoveries):
        stop, reason = memory.should_stop(5, build_frontier=build_frontier)
        assert stop is False
        # Re-establish the loop condition for the next check (no_progress_streak was reset).
        memory.no_progress_streak = 999
    stop, reason = memory.should_stop(5, build_frontier=build_frontier)
    assert stop is True
    assert reason == "persistent_navigation_loop"


def test_no_alternative_stops_immediately_without_wasting_a_recovery():
    memory = _bounce_memory()
    stop, reason = memory.should_stop(5)
    assert stop is True
    assert reason == "persistent_navigation_loop"
    assert memory.loop_recovery_count == 0


def test_action_count_does_not_stop_the_run():
    memory = _bounce_memory()
    memory.remaining_action_budget = 0
    memory.unexplored_urls.append("http://x/c")
    stop, reason = memory.should_stop(5, build_frontier=lambda: [_fake_candidate("available")])
    assert stop is False
    assert reason is None


def test_page_count_does_not_stop_the_run():
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_pages=1, max_actions=3))
    memory.visited_urls.update([f"http://x/{i}" for i in range(10)])
    stop, reason = memory.should_stop(5)
    assert stop is False
    assert reason is None


def test_unresolved_authentication_reported_truthfully_during_loop():
    memory = _bounce_memory()
    memory.authenticated = False
    memory.auth_blocker = "credentials_missing"
    stop, reason = memory.should_stop(5, build_frontier=lambda: [_fake_candidate("available")])
    assert stop is True
    assert reason == "unresolved_authentication"


def test_build_frontier_exception_does_not_crash_should_stop():
    memory = _bounce_memory()

    def _boom():
        raise RuntimeError("boom")

    stop, reason = memory.should_stop(5, build_frontier=_boom)
    assert stop is True
    assert reason == "persistent_navigation_loop"


def test_finish_action_carries_canonical_exploration_complete_code():
    """Planner._finish's natural-completion path must tag a canonical stop_reason_code,
    not just a free-text reason string a caller would have to pattern-match."""
    from app.gemma.mock_provider import MockGemmaProvider

    planner = Planner(MockGemmaProvider())
    action = planner._finish(
        "No remaining same-origin navigation controls, uninspected forms, "
        "or unvisited application URLs",
        code="exploration_complete",
    )
    assert action.action == ActionType.FINISH
    assert action.metadata["stop_reason_code"] == "exploration_complete"
