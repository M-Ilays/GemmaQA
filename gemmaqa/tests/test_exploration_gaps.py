"""ApplicationStore-derived exploration gaps (Phase 4): gap computation, and gap ->
deferred goal creation that merges with live frontier-derived goals instead of
duplicating them once the relevant page comes back into view."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.auth_strategy import AuthenticationStrategy  # noqa: E402
from app.agent.goals import sync_gap_goals, sync_goals  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.agent.planner import Planner  # noqa: E402
from app.application.gaps import compute_gaps  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.schemas import FormDescriptor, FormField, InteractiveElement, PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402


def _feedback_page(url: str = "https://example.com/feedback") -> PageState:
    return PageState(
        page_id=new_id(),
        url=url,
        title="Feedback",
        headings=["Feedback"],
        forms=[
            FormDescriptor(
                form_id="feedback_form",
                fields=[FormField(element_id="el_comment", label="Comment", field_type="text", required=True)],
                submit_element_id="el_fsubmit",
            )
        ],
        interactive_elements=[
            InteractiveElement(
                element_id="el_comment",
                tag="input",
                input_type="text",
                category="input",
                accessible_name="Comment",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_fsubmit",
                tag="button",
                role="button",
                category="button",
                accessible_name="Submit",
                text="Submit",
                is_visible=True,
                is_enabled=True,
            ),
            InteractiveElement(
                element_id="el_other",
                tag="a",
                category="link",
                accessible_name="Other Page",
                text="Other Page",
                href="https://example.com/other",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )


def _other_page(url: str = "https://example.com/other") -> PageState:
    return PageState(
        page_id=new_id(),
        url=url,
        title="Other",
        headings=["Other"],
        interactive_elements=[
            InteractiveElement(
                element_id="el_back",
                tag="a",
                category="link",
                accessible_name="Back",
                text="Back",
                href="https://example.com/feedback",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )


def _memory() -> RunMemory:
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.auth_strategy = AuthenticationStrategy()
    memory.remaining_action_budget = 30
    return memory


def test_unvisited_form_gap_detected_after_observation():
    memory = _memory()
    memory.remember_page(_feedback_page())
    gaps = compute_gaps(memory.app_store)
    form_gaps = [g for g in gaps if g.gap_type == "unvisited_form"]
    assert form_gaps, "a form observed but never inspected must produce a gap"
    assert form_gaps[0].related_id == "feedback_form"


def test_unvisited_form_gap_clears_once_actually_inspected():
    memory = _memory()
    memory.remember_page(_feedback_page())
    assert any(g.gap_type == "unvisited_form" for g in compute_gaps(memory.app_store))

    memory.app_store.mark_form_inspected("feedback_form")
    gaps_after = compute_gaps(memory.app_store)
    assert not any(g.gap_type == "unvisited_form" for g in gaps_after)


def test_unexplored_candidate_url_gap_detected():
    memory = _memory()
    memory.remember_page(_feedback_page())
    gaps = compute_gaps(memory.app_store)
    url_gaps = [g for g in gaps if g.gap_type == "unexplored_candidate_url"]
    assert any(g.related_id == "https://example.com/other" for g in url_gaps)


def test_gap_creates_deferred_goal_not_completed_by_empty_candidates():
    memory = _memory()
    memory.remember_page(_feedback_page())
    sync_gap_goals(memory, iteration=0)

    form_goals = [g for g in memory.goals if g.goal_type == "inspect_form"]
    assert form_goals, "a form gap must produce an inspect_form goal"
    goal = form_goals[0]
    assert goal.status == "deferred"
    assert goal.candidate_ids == []
    assert goal.anchor == "feedback_form"


def test_gap_goal_merges_with_live_frontier_goal_on_return_to_page():
    """The whole point of Phase 4: a gap discovered while we're on a different page
    must not spawn a second, duplicate goal once the frontier can finally see the
    real candidate for the same target — it must be the SAME goal, now backed."""
    memory = _memory()
    feedback = _feedback_page()
    other = _other_page()

    # Observe the feedback page, then leave it (mirrors production OBSERVE ordering).
    memory.remember_page(feedback)
    memory.remember_page(other)

    # While standing on `other`, the frontier has no inspect_form candidate for the
    # feedback form at all — only the gap layer can know it still needs inspecting.
    sync_gap_goals(memory, iteration=1)
    deferred = [g for g in memory.goals if g.goal_type == "inspect_form"]
    assert len(deferred) == 1
    assert deferred[0].status == "deferred"
    assert deferred[0].candidate_ids == []

    # Now the run navigates back to the feedback page; the frontier regenerates a
    # real inspect_form candidate for the same form.
    planner = Planner(MockGemmaProvider())
    frontier = planner._build_full_frontier(feedback, memory, {})
    sync_goals(frontier, memory, iteration=2)

    form_goals = [g for g in memory.goals if g.goal_type == "inspect_form"]
    assert len(form_goals) == 1, "gap-created and frontier-created goals must merge, not duplicate"
    assert form_goals[0].goal_id == deferred[0].goal_id
    assert form_goals[0].candidate_ids, "the merged goal must now be backed by a real candidate"
    assert form_goals[0].status == "deferred"  # not yet selected active


def test_planner_dispatches_inspect_form_after_gap_backed_goal_is_selected():
    memory = _memory()
    feedback = _feedback_page()
    other = _other_page()
    memory.remember_page(feedback)
    memory.remember_page(other)

    planner = Planner(MockGemmaProvider())
    # One full planning pass while away from the page just records the gap.
    action_away = planner._select_auth_candidate(other, memory, {})
    assert action_away is not None  # some navigation action on `other` itself

    # Back on the feedback page, the (now merged, backed) goal should be selectable
    # and dispatch a real INSPECT_FORM action.
    from app.schemas import ActionType

    action = planner._select_auth_candidate(feedback, memory, {})
    assert action is not None
    assert action.action == ActionType.INSPECT_FORM
    assert action.element_id == "feedback_form"
