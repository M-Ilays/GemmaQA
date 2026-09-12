"""generate_test_scenarios runs once per page per Tester (per run)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.tester import Tester  # noqa: E402
from app.schemas import FormDescriptor, FormField, PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402

LOGIN = "https://thinking-tester-contact-list.herokuapp.com/"
ADD_USER = "https://thinking-tester-contact-list.herokuapp.com/addUser"


def _page(url: str, title: str, *, password: bool = True) -> PageState:
    fields = [FormField(name="email", field_type="email", label="Email")]
    if password:
        fields.append(FormField(name="password", field_type="password", label="Password"))
    return PageState(
        page_id=new_id(),
        url=url,
        title=title,
        forms=[FormDescriptor(form_id="form_1", fields=fields)],
    )


def _gemma(*titles: str) -> AsyncMock:
    gemma = AsyncMock()
    gemma.generate_test_scenarios = AsyncMock(
        return_value=[
            {
                "title": title,
                "description": "ai",
                "category": "exploratory",
                "priority": "medium",
                "steps": ["step"],
                "expected_results": ["ok"],
            }
            for title in titles
        ]
    )
    return gemma


@pytest.mark.asyncio
async def test_first_observation_calls_gemma_and_keeps_scenarios() -> None:
    gemma = _gemma("Explore signup link")
    tester = Tester(gemma=gemma)
    page = _page(LOGIN, "Contact List App")
    created = await tester.propose_for_page(page, "run-1")
    gemma.generate_test_scenarios.assert_awaited_once()
    assert created
    assert any("Smoke:" in s.title for s in tester.scenarios)
    assert any(s.title == "Explore signup link" for s in tester.scenarios)


@pytest.mark.asyncio
async def test_repeated_observation_skips_gemma_and_preserves_scenarios() -> None:
    gemma = _gemma("Explore signup link")
    tester = Tester(gemma=gemma)
    first_page = _page(LOGIN, "Contact List App")
    await tester.propose_for_page(first_page, "run-1")
    snapshot = [(s.test_id, s.title) for s in tester.scenarios]
    assert snapshot

    created_again = await tester.propose_for_page(_page(LOGIN, "Contact List App"), "run-1")
    gemma.generate_test_scenarios.assert_awaited_once()
    assert created_again == []
    assert [(s.test_id, s.title) for s in tester.scenarios] == snapshot


@pytest.mark.asyncio
async def test_new_page_still_calls_gemma() -> None:
    gemma = _gemma("Explore signup link")
    tester = Tester(gemma=gemma)
    await tester.propose_for_page(_page(LOGIN, "Contact List App"), "run-1")
    await tester.propose_for_page(_page(ADD_USER, "Add User"), "run-1")
    assert gemma.generate_test_scenarios.await_count == 2
    urls_seen = {call.args[0].url for call in gemma.generate_test_scenarios.await_args_list}
    assert LOGIN.rstrip("/") in {u.rstrip("/") for u in urls_seen}
    assert any(u.rstrip("/").endswith("/addUser") for u in urls_seen)


@pytest.mark.asyncio
async def test_skip_llm_keeps_deterministic_scenarios_and_does_not_call_gemma() -> None:
    gemma = _gemma("Explore signup link")
    tester = Tester(gemma=gemma)
    created = await tester.propose_for_page(_page(LOGIN, "Contact List App"), "run-1", skip_llm=True)
    gemma.generate_test_scenarios.assert_not_awaited()
    assert any("Smoke:" in s.title for s in created)
    assert all(s.title != "Explore signup link" for s in created)


@pytest.mark.asyncio
async def test_failed_first_call_still_skips_repeat() -> None:
    gemma = AsyncMock()
    gemma.generate_test_scenarios = AsyncMock(side_effect=TimeoutError("timeout"))
    tester = Tester(gemma=gemma)
    await tester.propose_for_page(_page(LOGIN, "Contact List App"), "run-1")
    before = [(s.test_id, s.title) for s in tester.scenarios]
    await tester.propose_for_page(_page(LOGIN, "Contact List App"), "run-1")
    assert gemma.generate_test_scenarios.await_count == 1
    assert [(s.test_id, s.title) for s in tester.scenarios] == before
