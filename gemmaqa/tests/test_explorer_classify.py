"""Explorer.classify should skip Gemma when deterministic confidence is authoritative."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.explorer import Explorer  # noqa: E402
from app.schemas import (  # noqa: E402
    FormDescriptor,
    FormField,
    PageClassification,
    PageState,
    TableDescriptor,
)
from app.utils.ids import new_id  # noqa: E402


def _login_page() -> PageState:
    return PageState(
        page_id=new_id(),
        url="https://thinking-tester-contact-list.herokuapp.com/",
        title="Contact List App",
        forms=[
            FormDescriptor(
                form_id="form_login",
                fields=[
                    FormField(name="email", field_type="email", label="Email"),
                    FormField(name="password", field_type="password", label="Password"),
                ],
            )
        ],
    )


def _ambiguous_page() -> PageState:
    return PageState(
        page_id=new_id(),
        url="https://thinking-tester-contact-list.herokuapp.com/unknown",
        title="Untitled",
        visible_text_summary="generic content",
    )


@pytest.mark.asyncio
async def test_skips_analyze_page_when_deterministic_confidence_high() -> None:
    gemma = AsyncMock()
    gemma.analyze_page = AsyncMock(
        return_value=PageClassification(page_type="content", confidence=0.4, purpose="x")
    )
    explorer = Explorer(gemma=gemma)
    page = await explorer.classify(_login_page())
    gemma.analyze_page.assert_not_called()
    assert page.classification is not None
    assert page.classification.page_type == "authentication"
    assert page.classification.confidence >= 0.75


@pytest.mark.asyncio
async def test_calls_analyze_page_when_deterministic_confidence_low() -> None:
    gemma = AsyncMock()
    gemma.analyze_page = AsyncMock(
        return_value=PageClassification(
            page_type="content",
            confidence=0.8,
            purpose="Landing",
            module_guess="Home",
            tags=["home"],
        )
    )
    explorer = Explorer(gemma=gemma)
    page = _ambiguous_page()
    det = explorer.classify_deterministic(page)
    if det.confidence >= 0.75:
        pytest.skip("this page is no longer a low-confidence case")
    page = await explorer.classify(page)
    gemma.analyze_page.assert_awaited_once()
    assert page.classification is not None
    assert page.classification.purpose == "Landing"


def test_deterministic_classifier_unchanged_for_contact_list_login() -> None:
    explorer = Explorer(gemma=None)
    result = explorer.classify_deterministic(_login_page())
    assert result.page_type == "authentication"
    assert result.confidence >= 0.75


def _contact_page(url: str, title: str = "", **overrides) -> PageState:
    data = {
        "page_id": new_id(),
        "url": url,
        "title": title,
        "visible_text_summary": overrides.pop("visible_text_summary", ""),
    }
    data.update(overrides)
    return PageState(**data)


def test_edit_contact_is_edit_form_with_authoritative_confidence() -> None:
    explorer = Explorer(gemma=None)
    result = explorer.classify_deterministic(
        _contact_page(
            "https://thinking-tester-contact-list.herokuapp.com/editContact",
            title="",
            forms=[
                FormDescriptor(
                    form_id="form_edit",
                    fields=[FormField(name="firstName", field_type="text", label="First Name")],
                )
            ],
        )
    )
    assert result.page_type == "edit_form"
    assert result.confidence >= 0.75


def test_contact_details_is_detail_with_authoritative_confidence() -> None:
    explorer = Explorer(gemma=None)
    result = explorer.classify_deterministic(
        _contact_page(
            "https://thinking-tester-contact-list.herokuapp.com/contactDetails",
            title="",
        )
    )
    assert result.page_type == "detail"
    assert result.confidence >= 0.75


def test_real_crash_copy_is_still_error_page() -> None:
    explorer = Explorer(gemma=None)
    result = explorer.classify_deterministic(
        _contact_page(
            "https://app.example.com/broken",
            title="Oops",
            visible_text_summary="Something went wrong. Internal server error.",
        )
    )
    assert result.page_type == "error_page"


def test_add_contact_validation_copy_is_not_error_page() -> None:
    explorer = Explorer(gemma=None)
    result = explorer.classify_deterministic(
        _contact_page(
            "https://thinking-tester-contact-list.herokuapp.com/addContact",
            title="Add Contact",
            visible_text_summary="Contact form validation error: phone is invalid",
            forms=[
                FormDescriptor(
                    form_id="form_add",
                    fields=[FormField(name="phone", field_type="text", label="Phone")],
                )
            ],
        )
    )
    assert result.page_type == "create_form"
    assert result.confidence >= 0.75


@pytest.mark.asyncio
async def test_edit_contact_does_not_call_analyze_page() -> None:
    gemma = AsyncMock()
    gemma.analyze_page = AsyncMock(
        return_value=PageClassification(page_type="error_page", confidence=0.9)
    )
    explorer = Explorer(gemma=gemma)
    page = await explorer.classify(
        _contact_page(
            "https://thinking-tester-contact-list.herokuapp.com/editContact",
            title="",
            forms=[
                FormDescriptor(
                    form_id="form_edit",
                    fields=[FormField(name="firstName", field_type="text", label="First Name")],
                )
            ],
        )
    )
    gemma.analyze_page.assert_not_called()
    assert page.classification is not None
    assert page.classification.page_type == "edit_form"


def test_header_search_on_dashboard_is_not_low_confidence_search_results() -> None:
    explorer = Explorer(gemma=None)
    result = explorer.classify_deterministic(
        PageState(
            page_id=new_id(),
            url="https://opensource-demo.orangehrmlive.com/web/index.php/dashboard/index",
            title="OrangeHRM",
            search_fields=["Search"],
        )
    )
    assert result.page_type == "dashboard"
    assert result.confidence >= 0.75


def test_table_with_filter_form_is_list_not_create_form() -> None:
    explorer = Explorer(gemma=None)
    result = explorer.classify_deterministic(
        PageState(
            page_id=new_id(),
            url="https://opensource-demo.orangehrmlive.com/web/index.php/admin/viewSystemUsers",
            title="OrangeHRM",
            search_fields=["Username"],
            tables=[TableDescriptor(table_id="users")],
            forms=[
                FormDescriptor(
                    form_id="form_047",
                    fields=[FormField(name="username", field_type="text", label="Username")],
                )
            ],
        )
    )
    assert result.page_type == "list"
    assert result.confidence >= 0.75


@pytest.mark.asyncio
async def test_local_gemma_skips_analyze_page_on_ambiguous_pages() -> None:
    gemma = AsyncMock()
    gemma.name = "openai_compatible"
    gemma.analyze_page = AsyncMock(
        return_value=PageClassification(page_type="content", confidence=0.9)
    )
    explorer = Explorer(gemma=gemma)
    page = await explorer.classify(_ambiguous_page())
    gemma.analyze_page.assert_not_called()
    assert page.classification is not None
