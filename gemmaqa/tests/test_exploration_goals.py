"""ExplorationGoal model + lifecycle (Phase 3): goal creation, activation,
completion, and goal-first candidate selection in the Planner."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.frontier import FrontierBuilder  # noqa: E402
from app.agent.goals import (  # noqa: E402
    GOAL_STATUSES,
    GOAL_TYPES,
    candidates_for_goal,
    refresh_goal_status,
    select_active_goal,
    sync_goals,
)
from app.agent.memory import RunMemory  # noqa: E402
from app.agent.planner import Planner  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.schemas import ActionType, InteractiveElement, PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402


def _nav_page() -> PageState:
    return PageState(
        page_id=new_id(),
        url="https://example.com/home",
        title="Home",
        headings=["Home"],
        interactive_elements=[
            InteractiveElement(
                element_id="el_nav1",
                tag="a",
                role="link",
                category="link",
                accessible_name="Products",
                text="Products",
                href="https://example.com/products",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )


def _memory() -> RunMemory:
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.remaining_action_budget = 30
    return memory


def test_goal_created_from_navigation_candidates():
    page = _nav_page()
    memory = _memory()
    from app.agent.auth_strategy import AuthenticationStrategy

    frontier = FrontierBuilder(AuthenticationStrategy()).build(page, memory=memory)
    goals = sync_goals(frontier, memory, iteration=0)
    nav_goals = [g for g in goals if g.goal_type == "discover_navigation_region"]
    assert nav_goals, "expected a discover_navigation_region goal for the nav link"
    goal = nav_goals[0]
    assert goal.status == "proposed"
    assert goal.goal_type in GOAL_TYPES
    assert goal.status in GOAL_STATUSES
    nav_cand = next(c for c in frontier if c.candidate_type == "navigation_control")
    assert nav_cand.candidate_id in goal.candidate_ids


def test_select_active_goal_promotes_and_is_stable():
    page = _nav_page()
    memory = _memory()
    from app.agent.auth_strategy import AuthenticationStrategy

    frontier = FrontierBuilder(AuthenticationStrategy()).build(page, memory=memory)
    sync_goals(frontier, memory, iteration=0)

    first = select_active_goal(memory)
    assert first is not None
    assert first.status == "active"

    second = select_active_goal(memory)
    assert second is not None
    assert second.goal_id == first.goal_id, "an already-active goal must not be swapped out arbitrarily"


def test_goal_completes_when_all_tracked_candidates_exhausted():
    page = _nav_page()
    memory = _memory()
    from app.agent.auth_strategy import AuthenticationStrategy

    frontier = FrontierBuilder(AuthenticationStrategy()).build(page, memory=memory)
    sync_goals(frontier, memory, iteration=0)
    goal = next(g for g in memory.goals if g.goal_type == "discover_navigation_region")

    for cand in frontier:
        if cand.candidate_id in goal.candidate_ids:
            cand.status = "exhausted"
    by_id = {c.candidate_id: c for c in frontier}
    refresh_goal_status(by_id, memory, iteration=5)

    assert goal.status == "completed"
    assert goal.completed_at_iteration == 5
    assert goal.evidence, "completion must record structured evidence, not a free-text-only decision"


def test_goal_blocked_when_all_tracked_candidates_blocked():
    page = _nav_page()
    memory = _memory()
    from app.agent.auth_strategy import AuthenticationStrategy

    frontier = FrontierBuilder(AuthenticationStrategy()).build(page, memory=memory)
    sync_goals(frontier, memory, iteration=0)
    goal = next(g for g in memory.goals if g.goal_type == "discover_navigation_region")

    for cand in frontier:
        if cand.candidate_id in goal.candidate_ids:
            cand.status = "blocked"
    by_id = {c.candidate_id: c for c in frontier}
    refresh_goal_status(by_id, memory, iteration=3)

    assert goal.status == "blocked"
    assert goal.blocked_reason


def test_candidates_for_goal_excludes_terminal_candidates():
    page = _nav_page()
    memory = _memory()
    from app.agent.auth_strategy import AuthenticationStrategy

    frontier = FrontierBuilder(AuthenticationStrategy()).build(page, memory=memory)
    sync_goals(frontier, memory, iteration=0)
    goal = next(g for g in memory.goals if g.goal_type == "discover_navigation_region")

    live = candidates_for_goal(goal, frontier)
    assert live, "goal should have at least one live candidate before anything is attempted"

    for cand in frontier:
        cand.status = "exhausted"
    assert candidates_for_goal(goal, frontier) == []


def test_planner_dispatches_action_through_goal_and_tags_goal_id():
    page = _nav_page()
    memory = _memory()
    memory.remaining_action_budget = 30
    planner = Planner(MockGemmaProvider())

    action = planner._select_auth_candidate(page, memory, {})
    assert action is not None
    assert action.action == ActionType.CLICK
    assert (action.metadata or {}).get("goal_id"), "dispatched action must be attributable to the goal that produced it"
    assert any(g.goal_id == action.metadata["goal_id"] for g in memory.goals)


def test_goal_selection_never_hides_an_otherwise_dispatchable_candidate():
    """Goal-first ordering must not lose coverage: if the flat frontier had exactly one
    dispatchable candidate, goal-first selection must still find and return it."""
    page = _nav_page()
    memory = _memory()
    memory.remaining_action_budget = 30
    planner = Planner(MockGemmaProvider())

    direct_frontier = planner._build_full_frontier(page, memory, {})
    dispatchable = [c for c in direct_frontier if c.status not in {"exhausted", "blocked"}]
    assert dispatchable

    fresh_memory = _memory()
    fresh_memory.remaining_action_budget = 30
    action = planner._select_auth_candidate(page, fresh_memory, {})
    assert action is not None


def test_goal_ownership_never_outranks_a_globally_better_priority_candidate():
    """Regression: a worse-priority candidate that happens to belong to the active
    goal (e.g. a "Cancel" button in a discover_navigation_region goal, priority ~90)
    must never be dispatched ahead of a strictly better-priority candidate that
    belongs to a DIFFERENT goal (e.g. inspect_form, priority 20) — this let SauceDemo's
    checkout form's inspect_form/start_form_workflow candidates get starved by a
    same-page "Cancel" link every time a nav goal was already active."""
    from app.agent.frontier import FrontierBuilder
    from app.agent.goals import sync_goals
    from app.schemas import ActionType, FormDescriptor, FormField

    page = PageState(
        page_id=new_id(),
        url="https://example.com/checkout",
        title="Checkout",
        headings=["Checkout"],
        forms=[
            FormDescriptor(
                form_id="checkout_form",
                fields=[FormField(element_id="el_first", label="First Name", field_type="text", required=True)],
                submit_element_id="el_continue",
            )
        ],
        interactive_elements=[
            InteractiveElement(
                element_id="el_cancel",
                tag="a",
                category="link",
                accessible_name="Cancel",
                text="Cancel",
                href="https://example.com/cart",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )
    from app.agent.auth_strategy import AuthenticationStrategy

    memory = _memory()
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = True

    # Pre-activate a discover_navigation_region goal that owns the "Cancel" candidate,
    # so it's the selected active goal before inspect_form is ever considered.
    frontier = FrontierBuilder(memory.auth_strategy).build(page, memory=memory)
    sync_goals(frontier, memory, iteration=0)
    nav_goal = next(g for g in memory.goals if g.goal_type == "discover_navigation_region")
    nav_goal.status = "active"

    planner = Planner(MockGemmaProvider())
    action = planner._select_auth_candidate(page, memory, {})
    assert action is not None
    assert action.action == ActionType.INSPECT_FORM, (
        f"expected inspect_form (priority 20) to win over the active goal's own "
        f"Cancel candidate (priority ~90), got {action.action}"
    )
