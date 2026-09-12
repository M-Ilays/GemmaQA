"""LLM goal-ranking boundary (Phase 10): structured, validated, advisory-only
prioritization. The LLM may reorder goals as a tie-break but can never invent an
unknown id, override deterministic priority, or otherwise touch execution — and a
failed/malformed response must fall back to the existing deterministic order, never
an unrelated strategy."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.goals import ExplorationGoal, select_active_goal  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.agent.planner import Planner  # noqa: E402
from app.gemma.base import GemmaProvider  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.utils.ids import new_id  # noqa: E402

GOALS = [
    {"goal_id": "g1", "goal_type": "discover_navigation_region", "priority": 90, "confidence": 0.5},
    {"goal_id": "g2", "goal_type": "inspect_form", "priority": 20, "confidence": 0.9},
]


@pytest.mark.asyncio
async def test_base_rank_goals_accepts_valid_ranking():
    provider = MockGemmaProvider(generate_hook=lambda s, u: json.dumps({"ranked_goal_ids": ["g2", "g1"]}))
    ranked = await GemmaProvider.rank_goals(provider, GOALS)
    assert ranked == ["g2", "g1"]


@pytest.mark.asyncio
async def test_base_rank_goals_drops_unknown_ids():
    provider = MockGemmaProvider(
        generate_hook=lambda s, u: json.dumps({"ranked_goal_ids": ["g2", "g_invented", "g1"]})
    )
    ranked = await GemmaProvider.rank_goals(provider, GOALS)
    assert ranked == ["g2", "g1"]
    assert "g_invented" not in ranked


@pytest.mark.asyncio
async def test_base_rank_goals_dedupes():
    provider = MockGemmaProvider(generate_hook=lambda s, u: json.dumps({"ranked_goal_ids": ["g1", "g1", "g2"]}))
    ranked = await GemmaProvider.rank_goals(provider, GOALS)
    assert ranked == ["g1", "g2"]


@pytest.mark.asyncio
async def test_base_rank_goals_returns_empty_on_malformed_json():
    provider = MockGemmaProvider(generate_hook=lambda s, u: "not json at all")
    ranked = await GemmaProvider.rank_goals(provider, GOALS)
    assert ranked == []


@pytest.mark.asyncio
async def test_base_rank_goals_returns_empty_when_field_missing():
    provider = MockGemmaProvider(generate_hook=lambda s, u: json.dumps({"reasoning": "no ranking field"}))
    ranked = await GemmaProvider.rank_goals(provider, GOALS)
    assert ranked == []


@pytest.mark.asyncio
async def test_base_rank_goals_survives_provider_exception():
    def _boom(system, user):
        raise RuntimeError("provider down")

    provider = MockGemmaProvider(generate_hook=_boom)
    ranked = await GemmaProvider.rank_goals(provider, GOALS)
    assert ranked == []


@pytest.mark.asyncio
async def test_base_rank_goals_empty_input_short_circuits():
    provider = MockGemmaProvider()
    ranked = await GemmaProvider.rank_goals(provider, [])
    assert ranked == []


@pytest.mark.asyncio
async def test_mock_provider_deterministic_ranking_matches_priority_confidence():
    provider = MockGemmaProvider()
    ranked = await provider.rank_goals(GOALS)
    assert ranked == ["g2", "g1"]  # g2 has the better (lower) priority


def test_llm_rank_only_breaks_ties_never_overrides_priority():
    better_priority_no_rank = ExplorationGoal(
        goal_id="a", goal_type="inspect_form", title="a", status="proposed",
        candidate_ids=["c1"], priority=20, llm_rank=None,
    )
    worse_priority_ranked_first = ExplorationGoal(
        goal_id="b", goal_type="discover_navigation_region", title="b", status="proposed",
        candidate_ids=["c2"], priority=90, llm_rank=0,
    )
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.goals = [worse_priority_ranked_first, better_priority_no_rank]

    selected = select_active_goal(memory)
    assert selected is not None
    assert selected.goal_id == "a", "a strictly better priority must win regardless of llm_rank"


def test_llm_rank_breaks_a_genuine_priority_tie():
    tied_a = ExplorationGoal(
        goal_id="a", goal_type="discover_navigation_region", title="a", status="proposed",
        candidate_ids=["c1"], priority=90, llm_rank=1,
    )
    tied_b = ExplorationGoal(
        goal_id="b", goal_type="discover_navigation_region", title="b", status="proposed",
        candidate_ids=["c2"], priority=90, llm_rank=0,
    )
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.goals = [tied_a, tied_b]

    selected = select_active_goal(memory)
    assert selected is not None
    assert selected.goal_id == "b", "lower llm_rank should win a genuine priority tie"


@pytest.mark.asyncio
async def test_next_action_sets_llm_rank_on_existing_goals_before_dispatch():
    from app.schemas import InteractiveElement, PageState

    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.remaining_action_budget = 30
    g1 = ExplorationGoal(
        goal_id="only-a", goal_type="discover_navigation_region", title="a",
        status="proposed", candidate_ids=["x"], priority=90,
    )
    g2 = ExplorationGoal(
        goal_id="only-b", goal_type="discover_navigation_region", title="b",
        status="proposed", candidate_ids=["y"], priority=90,
    )
    memory.goals = [g1, g2]

    provider = MockGemmaProvider()
    planner = Planner(provider)
    page = PageState(
        page_id=new_id(),
        url="https://example.com/",
        title="Home",
        interactive_elements=[
            InteractiveElement(
                element_id="el_a", tag="button", role="button", category="button",
                accessible_name="Something", text="Something", is_visible=True, is_enabled=True,
            )
        ],
    )
    await planner.next_action(page, [], [], {}, memory=memory)
    assert g1.llm_rank is not None or g2.llm_rank is not None


def _two_goals() -> list[ExplorationGoal]:
    return [
        ExplorationGoal(
            goal_id="only-a", goal_type="discover_navigation_region", title="a",
            status="proposed", candidate_ids=["x"], priority=90,
        ),
        ExplorationGoal(
            goal_id="only-b", goal_type="discover_navigation_region", title="b",
            status="proposed", candidate_ids=["y"], priority=90,
        ),
    ]


class _CountingRankProvider(MockGemmaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.rank_calls = 0

    async def rank_goals(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.rank_calls += 1
        return await super().rank_goals(*args, **kwargs)


@pytest.mark.asyncio
async def test_rank_goals_skipped_when_auth_workflow_has_next_step():
    from app.agent.auth_strategy import AuthenticationStrategy, AuthWorkflow
    from app.schemas import ActionType, BrowserAction, InteractiveElement, PageState

    memory = RunMemory(run_id=new_id(), start_url="https://example.com/addUser")
    memory.remaining_action_budget = 30
    g1, g2 = _two_goals()
    memory.goals = [g1, g2]
    auth = AuthenticationStrategy()
    fill = BrowserAction(
        action=ActionType.FILL, element_id="el_001", value="GemmaQA",
        reason="Authentication write: fill first name",
    )
    auth.active_workflow = AuthWorkflow(
        workflow_id="auth_register_form",
        method="registration",
        form_id="form_signup",
        steps=[fill],
    )
    memory.auth_strategy = auth

    provider = _CountingRankProvider()
    page = PageState(
        page_id=new_id(),
        url="https://example.com/addUser",
        title="Add User",
        interactive_elements=[
            InteractiveElement(
                element_id="el_001", tag="input", type="text", category="input",
                accessible_name="First Name", is_visible=True, is_enabled=True,
            )
        ],
    )
    action = await Planner(provider).next_action(page, [], [], {}, memory=memory)
    assert provider.rank_calls == 0
    assert g1.llm_rank is None and g2.llm_rank is None
    assert action.action == ActionType.FILL
    assert action.element_id == "el_001"


@pytest.mark.asyncio
async def test_rank_goals_skipped_when_form_workflow_is_filling():
    from app.schemas import ActionType, BrowserAction, InteractiveElement, PageState

    class _FakeFormWf:
        state = "filling"
        workflow_id = "wf1"
        form_id = "form_1"
        purpose = "create_contact"

        def next_action(self) -> BrowserAction:
            return BrowserAction(
                action=ActionType.FILL, element_id="el_002", value="Pat",
                reason="Fill safe test data for first name",
            )

    from app.agent.auth_strategy import AuthenticationStrategy

    memory = RunMemory(run_id=new_id(), start_url="https://example.com/addContact")
    memory.remaining_action_budget = 30
    g1, g2 = _two_goals()
    memory.goals = [g1, g2]
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = True
    memory.active_form_workflow = _FakeFormWf()

    provider = _CountingRankProvider()
    page = PageState(
        page_id=new_id(),
        url="https://example.com/addContact",
        title="Add Contact",
        interactive_elements=[
            InteractiveElement(
                element_id="el_002", tag="input", type="text", category="input",
                accessible_name="First Name", is_visible=True, is_enabled=True,
            )
        ],
    )
    action = await Planner(provider).next_action(page, [], [], {}, memory=memory)
    assert provider.rank_calls == 0
    assert g1.llm_rank is None and g2.llm_rank is None
    assert action.action == ActionType.FILL
    assert action.element_id == "el_002"


@pytest.mark.asyncio
async def test_rank_goals_skipped_while_not_authenticated():
    from app.agent.auth_strategy import AuthenticationStrategy
    from app.schemas import InteractiveElement, PageState

    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.remaining_action_budget = 30
    g1, g2 = _two_goals()
    memory.goals = [g1, g2]
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = False
    provider = _CountingRankProvider()
    page = PageState(
        page_id=new_id(),
        url="https://example.com/",
        title="Contact List App",
        interactive_elements=[
            InteractiveElement(
                element_id="el_signup", tag="a", role="link", category="link",
                accessible_name="Sign up", text="Sign up", is_visible=True, is_enabled=True,
            )
        ],
    )
    await Planner(provider).next_action(page, [], [], {}, memory=memory)
    assert provider.rank_calls == 0
    assert g1.llm_rank is None and g2.llm_rank is None


@pytest.mark.asyncio
async def test_rank_goals_still_runs_when_no_workflow_is_pending():
    from app.schemas import InteractiveElement, PageState

    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.remaining_action_budget = 30
    g1, g2 = _two_goals()
    memory.goals = [g1, g2]
    provider = _CountingRankProvider()
    page = PageState(
        page_id=new_id(),
        url="https://example.com/",
        title="Home",
        interactive_elements=[
            InteractiveElement(
                element_id="el_a", tag="button", role="button", category="button",
                accessible_name="Something", text="Something", is_visible=True, is_enabled=True,
            )
        ],
    )
    await Planner(provider).next_action(page, [], [], {}, memory=memory)
    assert provider.rank_calls == 1
    assert g1.llm_rank is not None or g2.llm_rank is not None


@pytest.mark.asyncio
async def test_rank_goals_skipped_for_local_ollama_provider():
    from app.schemas import InteractiveElement, PageState

    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.remaining_action_budget = 30
    g1, g2 = _two_goals()
    memory.goals = [g1, g2]
    provider = _CountingRankProvider()
    provider.name = "openai_compatible"
    page = PageState(
        page_id=new_id(),
        url="https://example.com/",
        title="Home",
        interactive_elements=[
            InteractiveElement(
                element_id="el_a", tag="button", role="button", category="button",
                accessible_name="Something", text="Something", is_visible=True, is_enabled=True,
            )
        ],
    )
    await Planner(provider).next_action(page, [], [], {}, memory=memory)
    assert provider.rank_calls == 0
    assert g1.llm_rank is None and g2.llm_rank is None
