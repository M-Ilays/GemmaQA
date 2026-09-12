"""Deterministic prerequisite tracking (Phase 8): goals with unmet prerequisites are
blocked with a truthful reason (not discarded), and reactivated the moment the
prerequisite becomes true — e.g. checkout requires auth + a prior successful
safe-write (the generic stand-in for "a cart item exists")."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.goals import ExplorationGoal, refresh_prerequisites  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.agent.prerequisites import DEFAULT_PREREQUISITE_RULES, evaluate_prerequisites  # noqa: E402
from app.utils.ids import new_id  # noqa: E402


def _memory() -> RunMemory:
    return RunMemory(run_id=new_id(), start_url="https://example.com/")


def test_authenticated_rule_reflects_memory_state():
    memory = _memory()
    assert evaluate_prerequisites(["authenticated"], memory) == (False, ["authenticated"])
    memory.authenticated = True
    assert evaluate_prerequisites(["authenticated"], memory) == (True, [])


def test_has_completed_safe_write_rule():
    memory = _memory()
    assert evaluate_prerequisites(["has_completed_safe_write"], memory) == (
        False,
        ["has_completed_safe_write"],
    )
    memory.safe_writes_completed += 1
    assert evaluate_prerequisites(["has_completed_safe_write"], memory) == (True, [])


def test_unknown_prerequisite_key_never_blocks():
    memory = _memory()
    assert evaluate_prerequisites(["some_future_rule_not_yet_defined"], memory) == (True, [])


def test_multiple_prerequisites_report_all_missing():
    memory = _memory()
    satisfied, missing = evaluate_prerequisites(["authenticated", "has_completed_safe_write"], memory)
    assert satisfied is False
    assert set(missing) == {"authenticated", "has_completed_safe_write"}


def test_goal_blocked_when_prerequisites_unmet():
    memory = _memory()
    goal = ExplorationGoal(
        goal_id=new_id(),
        goal_type="continue_workflow",
        title="Checkout",
        status="proposed",
        candidate_ids=["c1"],
        prerequisites=["authenticated", "has_completed_safe_write"],
    )
    memory.goals.append(goal)
    refresh_prerequisites(memory)
    assert goal.status == "blocked"
    assert goal.blocked_reason and "authenticated" in goal.blocked_reason
    assert goal.evidence


def test_goal_reactivates_once_prerequisites_satisfied():
    memory = _memory()
    goal = ExplorationGoal(
        goal_id=new_id(),
        goal_type="continue_workflow",
        title="Checkout",
        status="proposed",
        candidate_ids=["c1"],
        prerequisites=["authenticated", "has_completed_safe_write"],
    )
    memory.goals.append(goal)
    refresh_prerequisites(memory)
    assert goal.status == "blocked"

    memory.authenticated = True
    memory.safe_writes_completed = 1
    refresh_prerequisites(memory)
    assert goal.status == "deferred"
    assert goal.blocked_reason is None


def test_goal_without_prerequisites_is_never_touched():
    memory = _memory()
    goal = ExplorationGoal(
        goal_id=new_id(),
        goal_type="discover_navigation_region",
        title="Explore",
        status="proposed",
        candidate_ids=["c1"],
    )
    memory.goals.append(goal)
    refresh_prerequisites(memory)
    assert goal.status == "proposed"


def test_checkout_purpose_goal_gets_generic_checkout_prerequisites():
    from app.agent.auth_strategy import AuthenticationStrategy
    from app.agent.frontier import FrontierBuilder
    from app.agent.goals import sync_goals
    from app.schemas import FormDescriptor, FormField, PageState

    memory = _memory()
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = True

    page = PageState(
        page_id=new_id(),
        url="https://example.com/checkout-step-one.html",
        title="Checkout",
        headings=["Checkout"],
        forms=[
            FormDescriptor(
                form_id="checkout_form",
                fields=[FormField(element_id="el_first", label="First Name", field_type="text")],
                submit_element_id="el_continue",
            )
        ],
    )
    frontier = FrontierBuilder(memory.auth_strategy).build(page, allow_safe_test_data=True, memory=memory)
    sync_goals(frontier, memory, iteration=0)

    goal = next(g for g in memory.goals if g.goal_type == "continue_workflow")
    assert set(goal.prerequisites) == {"authenticated", "has_completed_safe_write"}


def test_all_default_rule_keys_are_documented_and_deterministic():
    for key, rule in DEFAULT_PREREQUISITE_RULES.items():
        assert rule.key == key
        assert rule.description
        assert callable(rule.check)
