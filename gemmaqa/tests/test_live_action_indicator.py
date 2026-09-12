"""Live Action Indicator — observational overlay + activity lines.

The indicator must describe what GemmaQA is already doing. It must not decide,
delay, or change a click/fill/submit, and it must reuse the existing activity
log rather than invent a second event stream.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
import sys

sys.path.insert(0, str(BACKEND))

from app.reporting.activity_log import ActivityLog, summarize  # noqa: E402
from app.reporting.live_action import (  # noqa: E402
    format_live_action_line,
    format_live_status_line,
    live_action_fields,
    live_action_target,
)
from app.schemas import (  # noqa: E402
    ActionCategory,
    ActionType,
    BrowserAction,
    InteractiveElement,
    PageState,
    RiskLevel,
)

EXECUTOR = BACKEND / "app" / "browser" / "executor.py"
INDICATOR = BACKEND / "app" / "browser" / "live_indicator.py"
EVIDENCE = BACKEND / "app" / "browser" / "evidence.py"
CUSTOM = BACKEND / "app" / "browser" / "custom_controls.py"


def _action(kind: ActionType, *, element_id: str = "el_1", label: str | None = None, **meta) -> BrowserAction:
    metadata = dict(meta)
    if label:
        metadata["action_label"] = label
    return BrowserAction(
        action=kind,
        element_id=element_id,
        reason="test",
        risk=RiskLevel.LOW,
        category=ActionCategory.EXPLORATION,
        metadata=metadata,
    )


def _page(*elements: InteractiveElement) -> PageState:
    return PageState(page_id="p1", url="https://example.test/", title="Test", interactive_elements=list(elements))


def test_click_event_line_is_click_arrow_target():
    action = _action(ActionType.CLICK, label="Sign up")
    fields = live_action_fields(action, None)
    assert fields["live_action_line"] == "CLICK → Sign up"
    assert fields["live_status_line"] == "CLICKING: Sign up"
    assert summarize("action_started", fields | {"action": "click"}) == "CLICK → Sign up"


def test_fill_event_line_is_fill_arrow_field():
    action = _action(ActionType.FILL, label="First Name")
    fields = live_action_fields(action, None)
    assert fields["live_action_line"] == "FILL → First Name"
    assert fields["live_status_line"] == "FILLING: First Name"
    assert summarize("action_started", fields | {"action": "fill"}) == "FILL → First Name"


def test_submit_event_line_and_status():
    action = _action(
        ActionType.CLICK,
        label="Add Contact",
        form_workflow_submit=True,
    )
    fields = live_action_fields(action, None)
    assert fields["live_action_line"] == "CLICK → Add Contact / Submit"
    assert fields["live_status_line"] == "SUBMITTING FORM"
    assert summarize("action_started", fields | {"action": "click"}) == "CLICK → Add Contact / Submit"


def test_sign_up_is_not_treated_as_a_form_submit():
    """A Sign up *navigation* click must stay CLICKING, not SUBMITTING FORM."""
    fields = live_action_fields(_action(ActionType.CLICK, label="Sign up"), None)
    assert fields["live_action_line"] == "CLICK → Sign up"
    assert fields["live_status_line"] == "CLICKING: Sign up"


def test_blocked_action_is_represented_with_reason():
    line = format_live_action_line(
        "click",
        "Delete contact",
        outcome="blocked",
        reason="Safety policy",
    )
    assert "BLOCKED → Delete contact" in line
    assert "REASON → Safety policy" in line
    text = summarize(
        "action_blocked",
        {"action": "click", "action_label": "Delete contact", "reason": "Safety policy"},
    )
    assert "BLOCKED → Delete contact" in text
    assert "REASON → Safety policy" in text


def test_failed_action_is_represented_with_reason():
    line = format_live_action_line("click", "Submit", outcome="failed", reason="Validation error")
    assert "FAILED → Submit" in line
    assert "REASON → Validation error" in line
    text = summarize(
        "action_executed",
        {
            "action": "click",
            "action_label": "Submit",
            "success": False,
            "error": "Validation error",
        },
    )
    assert "FAILED → Submit" in text
    assert "REASON → Validation error" in text


def test_supported_action_types_have_readable_verbs():
    labels = {
        "click": "CLICK",
        "fill": "FILL",
        "fill_form": "FILL",
        "submit": "CLICK",
        "select": "SELECT",
        "check": "CHECK",
        "uncheck": "UNCHECK",
        "open_url": "NAVIGATE",
        "inspect_form": "INSPECT_FORM",
    }
    for action_type, verb in labels.items():
        assert format_live_action_line(action_type, "Target").startswith(f"{verb} → ")


def test_element_accessible_name_is_used_when_metadata_has_no_label():
    page = _page(
        InteractiveElement(element_id="el_9", tag="button", accessible_name="Edit"),
    )
    action = _action(ActionType.CLICK, element_id="el_9")
    assert live_action_target(action, page) == "Edit"


def test_fill_prefers_the_field_name_over_a_copied_page_heading():
    page = _page(
        InteractiveElement(element_id="el_1", tag="input", accessible_name="First Name"),
    )
    action = _action(
        ActionType.FILL,
        element_id="el_1",
        label="Sign up to begin adding your contacts!",
    )
    assert live_action_target(action, page) == "First Name"
    assert live_action_fields(action, page)["live_action_line"] == "FILL → First Name"


def test_existing_activity_log_still_records_through_the_same_funnel(tmp_path: Path):
    """The indicator adds keys to the existing record — it does not open a second log."""
    log = ActivityLog("run_live", root=tmp_path)
    fields = live_action_fields(_action(ActionType.CLICK, label="Sign up"), None)
    record = log.record("action_started", {"action": "click", "element_id": "el_1", **fields})
    assert record["summary"] == "CLICK → Sign up"
    assert record["event"] == "action_started"
    lines = (tmp_path / "activity.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    stored = json.loads(lines[0])
    assert stored["summary"] == "CLICK → Sign up"
    assert stored["event"] == "action_started"


def test_historical_activity_summaries_without_live_keys_still_work():
    """Existing activity-log tests and stored runs keep their original sentences."""
    assert "failed" in summarize("action_executed", {"action": "click", "success": False})
    assert "succeeded" in summarize("action_executed", {"action": "click", "success": True})
    text = summarize(
        "action_planned",
        {"action": "click", "element_id": "el_008", "reason": "Open the record"},
    )
    assert "click" in text and "el_008" in text and "Open the record" in text


def test_indicator_does_not_modify_click_execution_path():
    """The executor still clicks via click_element; the overlay is painted first."""
    executor_src = EXECUTOR.read_text(encoding="utf-8")
    custom_src = CUSTOM.read_text(encoding="utf-8")
    assert "click_element(locator)" in executor_src or "click_element" in executor_src
    assert "show_live_indicator" in executor_src
    assert "def click_element" in custom_src
    tree = ast.parse(executor_src)
    # The indicator import must not replace click_element.
    imports = [
        n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
    imported_names: set[str] = set()
    for node in imports:
        if isinstance(node, ast.ImportFrom) and node.module == "app.browser.custom_controls":
            imported_names.update(alias.name for alias in node.names)
    assert "click_element" in imported_names


def test_indicator_never_sleeps_or_calls_a_model():
    src = INDICATOR.read_text(encoding="utf-8")
    assert "wait_for_timeout" not in src
    assert "asyncio.sleep" not in src
    assert "time.sleep" not in src
    lowered = src.lower()
    assert "llm" not in lowered
    assert "openai_compatible" not in lowered
    assert "generate(" not in lowered
    assert HIGHLIGHT_MS_FROM_SOURCE(src) <= 2000


def HIGHLIGHT_MS_FROM_SOURCE(src: str) -> int:
    for line in src.splitlines():
        if line.startswith("HIGHLIGHT_MS"):
            return int(line.split("=")[1].strip())
    raise AssertionError("HIGHLIGHT_MS missing")


@pytest.mark.asyncio
async def test_show_live_indicator_returns_immediately_without_changing_the_locator():
    from app.browser.live_indicator import HIGHLIGHT_MS, show_live_indicator

    calls: list[dict] = []

    class FakeLocator:
        async def evaluate(self, script, payload):
            calls.append({"script": script, "payload": payload})

    action = _action(ActionType.CLICK, label="Sign up")
    started = time.perf_counter()
    await show_live_indicator(FakeLocator(), action, None)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.25, f"indicator awaited the highlight ({elapsed:.3f}s)"
    assert calls and calls[0]["payload"]["label"] == "CLICK → Sign up"
    assert calls[0]["payload"]["ms"] == HIGHLIGHT_MS
    script = calls[0]["script"]
    assert "pointer-events:none" in script
    assert "pointer-events:auto" not in script
    assert 'document.documentElement.appendChild' in script


def test_evidence_strips_the_indicator_before_screenshots():
    src = EVIDENCE.read_text(encoding="utf-8")
    assert "clear_live_indicator" in src
    assert "await page.screenshot" in src
    # Overlay is removed, then the existing screenshot path still runs.
    clear_at = src.index("await clear_live_indicator(page)")
    shot_at = src.index("await page.screenshot")
    assert clear_at < shot_at


def test_overlay_script_does_not_touch_form_values_or_navigation():
    src = INDICATOR.read_text(encoding="utf-8")
    forbidden = (
        ".value =",
        "location.href",
        "location.assign",
        "history.",
        "click()",
        "submit()",
        "data-gemmaqa-id",
    )
    for token in forbidden:
        assert token not in src, token
