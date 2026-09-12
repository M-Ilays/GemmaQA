"""Optimization #3: analyze_potential_bug only when hard bug/error signals exist."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.bug_analyzer import BugAnalyzer  # noqa: E402
from app.schemas import (  # noqa: E402
    ActionResult,
    ActionType,
    BrowserAction,
    BugAnalysisResult,
    BugClassification,
    FormDescriptor,
    FormField,
    PageClassification,
    PageState,
)
from app.utils.ids import new_id  # noqa: E402

LOGIN = "https://thinking-tester-contact-list.herokuapp.com/"
BENIGN_401 = "401 POST https://thinking-tester-contact-list.herokuapp.com/users/login"
BENIGN_CONSOLE = "Failed to load resource: the server responded with a status of 401 (Unauthorized)"


def _page(**overrides) -> PageState:
    data = {
        "page_id": new_id(),
        "url": LOGIN,
        "title": "Contact List App",
        "console_errors": [],
        "network_failures": [],
        "dialogs": [],
        "alerts": [],
    }
    data.update(overrides)
    return PageState(**data)


def _gemma(*, result: BugAnalysisResult | None = None) -> AsyncMock:
    gemma = AsyncMock()
    gemma.analyze_potential_bug = AsyncMock(
        return_value=result
        or BugAnalysisResult(
            classification=BugClassification.NO_DEFECT,
            confidence=0.0,
        )
    )
    return gemma


def _empty_required_result() -> ActionResult:
    return ActionResult(
        action_id=new_id(),
        run_id="run-1",
        action=BrowserAction(
            action=ActionType.FILL,
            element_id="el_001",
            value="",
            reason="Safe empty-value check on required Name",
            expected_result="validation",
            metadata={"value_category": "empty"},
        ),
        success=True,
    )


@pytest.mark.asyncio
async def test_quiet_page_does_not_call_gemma_bug_analysis() -> None:
    gemma = _gemma()
    analyzer = BugAnalyzer(gemma=gemma)
    defects = await analyzer.analyze_page(_page(), "run-1")
    gemma.analyze_potential_bug.assert_not_awaited()
    assert analyzer.gemma_bug_analysis_calls == 0
    assert analyzer.skipped_gemma_bug_analyses == 1
    assert defects == []


@pytest.mark.asyncio
async def test_console_error_calls_gemma_bug_analysis() -> None:
    gemma = _gemma()
    analyzer = BugAnalyzer(gemma=gemma)
    page = _page(console_errors=["TypeError: x is undefined"])
    await analyzer.analyze_page(page, "run-1")
    gemma.analyze_potential_bug.assert_awaited_once()
    assert analyzer.gemma_bug_analysis_calls == 1
    obs = gemma.analyze_potential_bug.await_args.args[0]
    assert obs["console_errors"] == ["TypeError: x is undefined"]


@pytest.mark.asyncio
async def test_form_validation_400_does_not_call_gemma() -> None:
    gemma = _gemma()
    analyzer = BugAnalyzer(gemma=gemma)
    page = _page(
        url="https://thinking-tester-contact-list.herokuapp.com/addContact",
        title="Add Contact",
        console_errors=[
            "Failed to load resource: the server responded with a status of 400 (Bad Request)"
        ],
        network_failures=[
            "400 POST https://thinking-tester-contact-list.herokuapp.com/contacts"
        ],
        classification=PageClassification(page_type="create_form", confidence=0.82),
        forms=[
            FormDescriptor(
                form_id="form_add",
                fields=[FormField(name="phone", field_type="text", label="Phone")],
            )
        ],
    )
    await analyzer.analyze_page(page, "run-1")
    gemma.analyze_potential_bug.assert_not_awaited()


@pytest.mark.asyncio
async def test_network_failure_calls_gemma_bug_analysis() -> None:
    gemma = _gemma()
    analyzer = BugAnalyzer(gemma=gemma)
    page = _page(network_failures=["500 GET https://thinking-tester-contact-list.herokuapp.com/api"])
    await analyzer.analyze_page(page, "run-1")
    gemma.analyze_potential_bug.assert_awaited_once()
    obs = gemma.analyze_potential_bug.await_args.args[0]
    assert obs["network_failures"] == ["500 GET https://thinking-tester-contact-list.herokuapp.com/api"]


@pytest.mark.asyncio
async def test_dialog_calls_gemma_bug_analysis() -> None:
    gemma = _gemma()
    analyzer = BugAnalyzer(gemma=gemma)
    await analyzer.analyze_page(_page(dialogs=["Default password is admin"]), "run-1")
    gemma.analyze_potential_bug.assert_awaited_once()
    obs = gemma.analyze_potential_bug.await_args.args[0]
    assert obs["dialogs"] == ["Default password is admin"]


@pytest.mark.asyncio
async def test_alert_calls_gemma_bug_analysis() -> None:
    gemma = _gemma()
    analyzer = BugAnalyzer(gemma=gemma)
    await analyzer.analyze_page(_page(alerts=["Invalid email address"]), "run-1")
    gemma.analyze_potential_bug.assert_awaited_once()
    obs = gemma.analyze_potential_bug.await_args.args[0]
    assert obs["alerts"] == ["Invalid email address"]


@pytest.mark.asyncio
async def test_deterministic_error_page_does_not_call_gemma_bug_analysis() -> None:
    gemma = _gemma()
    analyzer = BugAnalyzer(gemma=gemma)
    page = _page(classification=PageClassification(page_type="error_page", confidence=0.8))
    assert analyzer.deterministic_signals(page)
    defects = await analyzer.analyze_page(page, "run-1")
    gemma.analyze_potential_bug.assert_not_awaited()
    assert analyzer.skipped_gemma_bug_analyses == 1
    assert any("Error page" in d.title for d in defects)


@pytest.mark.asyncio
async def test_deterministic_empty_required_does_not_call_gemma_bug_analysis() -> None:
    gemma = _gemma()
    analyzer = BugAnalyzer(gemma=gemma)
    page = _page(alerts=[])
    result = _empty_required_result()
    assert analyzer.deterministic_signals(page, action_result=result)
    defects = await analyzer.analyze_page(page, "run-1", action_result=result)
    gemma.analyze_potential_bug.assert_not_awaited()
    assert any("empty" in d.title.lower() for d in defects)


@pytest.mark.asyncio
async def test_form_validation_alert_does_not_call_gemma() -> None:
    gemma = _gemma()
    analyzer = BugAnalyzer(gemma=gemma)
    page = _page(
        url="https://thinking-tester-contact-list.herokuapp.com/addContact",
        title="Add Contact",
        alerts=["Contact validation failed: phone is invalid"],
        classification=PageClassification(page_type="create_form", confidence=0.82),
        forms=[
            FormDescriptor(
                form_id="form_add",
                fields=[FormField(name="phone", field_type="text", label="Phone")],
            )
        ],
    )
    await analyzer.analyze_page(page, "run-1")
    gemma.analyze_potential_bug.assert_not_awaited()
    assert analyzer.skipped_gemma_bug_analyses == 1


@pytest.mark.asyncio
async def test_same_hard_signals_do_not_call_gemma_twice() -> None:
    gemma = _gemma()
    analyzer = BugAnalyzer(gemma=gemma)
    page = _page(console_errors=["TypeError: x is undefined"])
    await analyzer.analyze_page(page, "run-1")
    await analyzer.analyze_page(page, "run-1")
    gemma.analyze_potential_bug.assert_awaited_once()
    assert analyzer.skipped_gemma_bug_analyses == 1


@pytest.mark.asyncio
async def test_benign_console_and_network_skip_gemma_bug_analysis() -> None:
    gemma = _gemma()
    analyzer = BugAnalyzer(gemma=gemma)
    page = _page(console_errors=[BENIGN_CONSOLE], network_failures=[BENIGN_401])
    assert analyzer.deterministic_signals(page) == []
    defects = await analyzer.analyze_page(page, "run-1")
    gemma.analyze_potential_bug.assert_not_awaited()
    assert analyzer.skipped_gemma_bug_analyses == 1
    assert defects == []


@pytest.mark.asyncio
async def test_invoked_behavior_unchanged_console_finding_and_observation() -> None:
    gemma = _gemma(
        result=BugAnalysisResult(
            classification=BugClassification.NO_DEFECT,
            confidence=0.0,
        )
    )
    analyzer = BugAnalyzer(gemma=gemma)
    page = _page(console_errors=["Uncaught TypeError: boom"])
    defects = await analyzer.analyze_page(page, "run-1")
    gemma.analyze_potential_bug.assert_awaited_once()
    args, kwargs = gemma.analyze_potential_bug.await_args
    assert kwargs == {}
    assert set(args[0]) == {
        "url",
        "title",
        "console_errors",
        "network_failures",
        "dialogs",
        "alerts",
        "page_type",
    }
    assert args[1] == {"run_id": "run-1", "deterministic_count": 1}
    assert any(d.title == "JavaScript console error" for d in defects)


@pytest.mark.asyncio
async def test_no_gemma_provider_still_records_deterministic_findings() -> None:
    analyzer = BugAnalyzer(gemma=None)
    page = _page(network_failures=["500 GET https://x/api"])
    defects = await analyzer.analyze_page(page, "run-1")
    assert analyzer.gemma_bug_analysis_calls == 0
    assert any("HTTP 500" in d.title for d in defects)
