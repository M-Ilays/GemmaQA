"""Focused tests: ActionExecutor via BrowserAdapter (Direct + mocked MCP)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.memory import RunMemory  # noqa: E402
from app.browser.adapters.mcp import (  # noqa: E402
    FakeMcpTransport,
    PlaywrightMCPAdapter,
    snapshot_to_observation,
)
from app.browser.adapters.types import AdapterCapabilities  # noqa: E402
from app.browser.evidence import EvidenceCollector  # noqa: E402
from app.browser.executor import ActionExecutor  # noqa: E402
from app.browser.observer import PageObserver  # noqa: E402
from app.reporting.report_builder import ReportBuilder  # noqa: E402
from app.schemas import (  # noqa: E402
    ActionType,
    BrowserAction,
    InteractiveElement,
    PageState,
)
from app.utils.ids import new_id  # noqa: E402


@pytest.fixture
def page_state() -> PageState:
    el_link = InteractiveElement(
        element_id="el_001",
        tag="a",
        role="link",
        accessible_name="Sign up",
        visible_text="Sign up",
        text="Sign up",
        category="link",
        href="/signup",
        selector_hint="Sign up",
        is_visible=True,
        is_enabled=True,
    )
    el_input = InteractiveElement(
        element_id="el_002",
        tag="input",
        role="textbox",
        accessible_name="Email",
        visible_text="Email",
        category="input",
        input_type="email",
        selector_hint="Email",
        is_visible=True,
        is_enabled=True,
    )
    return PageState(
        page_id=new_id(),
        url="https://example.com/",
        title="Home",
        headings=["Home"],
        interactive_elements=[el_link, el_input],
        state_fingerprint="fp_home",
    )


@pytest.mark.asyncio
async def test_executor_works_without_native_page(page_state: PageState):
    fake = FakeMcpTransport()
    adapter = PlaywrightMCPAdapter("ex-no-page", transport=fake)
    await adapter.start_session()
    await adapter.navigate("https://example.com/")
    assert adapter.native_page is None
    assert adapter.supports_native_page is False

    evidence = EvidenceCollector(new_id())
    observer = PageObserver()
    executor = ActionExecutor(
        new_id(),
        adapter=adapter,
        evidence=evidence,
        observer=observer,
    )
    result = await executor.execute(
        action=BrowserAction(
            action=ActionType.CLICK,
            element_id="el_001",
            reason="nav",
            metadata={"name": "Sign up"},
        ),
        page_state=page_state,
        capture_evidence=True,
    )
    assert result.success is True
    assert result.inspected.get("adapter") == "playwright_mcp"
    assert result.after_url.endswith("/signup") or "signup" in result.after_url
    await adapter.close_session()


@pytest.mark.asyncio
async def test_mcp_click_translation(page_state: PageState):
    fake = FakeMcpTransport()
    adapter = PlaywrightMCPAdapter("ex-click", transport=fake)
    await adapter.start_session()
    await adapter.navigate("https://example.com/")
    executor = ActionExecutor(new_id(), adapter=adapter)
    result = await executor.execute(
        action=BrowserAction(
            action=ActionType.CLICK,
            element_id="el_001",
            reason="click",
            metadata={"name": "Sign up"},
        ),
        page_state=page_state,
        capture_evidence=False,
    )
    assert result.success
    assert any(n == "browser_click" for n, _ in fake.calls)
    await adapter.close_session()


@pytest.mark.asyncio
async def test_mcp_fill_translation(page_state: PageState):
    fake = FakeMcpTransport()
    adapter = PlaywrightMCPAdapter("ex-fill", transport=fake)
    await adapter.start_session()
    await adapter.navigate("https://example.com/")
    executor = ActionExecutor(new_id(), adapter=adapter)
    result = await executor.execute(
        action=BrowserAction(
            action=ActionType.FILL,
            element_id="el_002",
            value="qa@example.com",
            reason="fill",
            metadata={"name": "Email"},
        ),
        page_state=page_state,
        capture_evidence=False,
    )
    assert result.success
    type_calls = [a for n, a in fake.calls if n == "browser_type"]
    assert type_calls
    assert type_calls[-1].get("text") == "qa@example.com"
    await adapter.close_session()


@pytest.mark.asyncio
async def test_mcp_navigate_translation():
    fake = FakeMcpTransport()
    adapter = PlaywrightMCPAdapter("ex-nav", transport=fake)
    await adapter.start_session()
    executor = ActionExecutor(new_id(), adapter=adapter)
    result = await executor.execute(
        action=BrowserAction(
            action=ActionType.OPEN_URL,
            url="https://example.com/login",
            reason="open",
        ),
        capture_evidence=False,
    )
    assert result.success
    assert ("browser_navigate", {"url": "https://example.com/login"}) in [
        (n, a) for n, a in fake.calls
    ]
    assert result.after_url == "https://example.com/login"
    await adapter.close_session()


@pytest.mark.asyncio
async def test_mcp_result_normalisation(page_state: PageState):
    fake = FakeMcpTransport()
    adapter = PlaywrightMCPAdapter("ex-norm", transport=fake)
    await adapter.start_session()
    await adapter.navigate("https://example.com/")
    executor = ActionExecutor(new_id(), adapter=adapter)
    result = await executor.execute(
        action=BrowserAction(
            action=ActionType.CLICK,
            element_id="el_001",
            reason="nav",
            metadata={"name": "Sign up"},
        ),
        page_state=page_state,
        capture_evidence=False,
    )
    assert result.before_url == "https://example.com/"
    assert result.after_url != result.before_url
    assert result.page_state_changed is True
    assert result.inspected["adapter"] == "playwright_mcp"
    assert isinstance(result.evidence_ids, list)
    await adapter.close_session()


@pytest.mark.asyncio
async def test_missing_capability_blocks_safely():
    fake = FakeMcpTransport()
    caps = AdapterCapabilities(fill=False, console_events=False, network_events=False)
    adapter = PlaywrightMCPAdapter(
        "ex-cap", transport=fake, capabilities_override=caps
    )
    await adapter.start_session()
    executor = ActionExecutor(new_id(), adapter=adapter)
    result = await executor.execute(
        action=BrowserAction(
            action=ActionType.FILL,
            element_id="el_002",
            value="x",
            reason="fill",
        ),
        capture_evidence=False,
    )
    assert result.success is False
    assert result.error == "capability_unavailable:fill"
    assert executor.capability_blocks == 1
    assert not any(n == "browser_type" for n, _ in fake.calls)
    await adapter.close_session()


@pytest.mark.asyncio
async def test_mcp_disconnection_structured_failure(page_state: PageState):
    fake = FakeMcpTransport(disconnect_after=2)
    adapter = PlaywrightMCPAdapter("ex-disc", transport=fake)
    await adapter.start_session()
    fake._closed = True
    executor = ActionExecutor(new_id(), adapter=adapter)
    result = await executor.execute(
        action=BrowserAction(
            action=ActionType.OPEN_URL,
            url="https://example.com/x",
            reason="nav",
        ),
        page_state=page_state,
        capture_evidence=False,
    )
    assert result.success is False
    assert result.error
    assert executor.adapter_execution_failures >= 1
    await adapter.close_session()


@pytest.mark.asyncio
async def test_no_silent_fallback_to_direct():
    class BoomTransport(FakeMcpTransport):
        async def initialize(self) -> None:
            raise RuntimeError("MCP process not running")

    adapter = PlaywrightMCPAdapter("ex-nofallback", transport=BoomTransport())
    with pytest.raises(RuntimeError, match="Refusing silent fallback"):
        await adapter.start_session()
    assert adapter.native_page is None


@pytest.mark.asyncio
async def test_observer_consumes_adapter_observations():
    fake = FakeMcpTransport()
    adapter = PlaywrightMCPAdapter("ex-obs", transport=fake)
    await adapter.start_session()
    await adapter.navigate("https://example.com/")
    observer = PageObserver()
    state = await observer.observe_adapter(adapter)
    assert state.url.startswith("https://example.com")
    assert state.title
    assert any(e.element_id == "el_001" for e in state.interactive_elements)
    assert state.state_fingerprint
    await adapter.close_session()


@pytest.mark.asyncio
async def test_evidence_adapter_independent():
    fake = FakeMcpTransport()
    adapter = PlaywrightMCPAdapter("ex-ev", transport=fake)
    await adapter.start_session()
    evidence = EvidenceCollector(new_id())
    item = await evidence.screenshot_via_adapter(adapter, "probe")
    assert item.kind in {"screenshot", "other"}
    assert item.path
    assert Path(item.path).name
    meta = evidence.record_metadata(
        url="https://example.com/",
        title="Home",
        console_errors=[],
        network_errors=[],
        action={"adapter": "playwright_mcp"},
    )
    assert meta.path
    await adapter.close_session()


@pytest.mark.asyncio
async def test_navigation_edges_recorded_in_mcp_mode(page_state: PageState):
    fake = FakeMcpTransport()
    adapter = PlaywrightMCPAdapter("ex-edge", transport=fake)
    await adapter.start_session()
    await adapter.navigate("https://example.com/")
    executor = ActionExecutor(new_id(), adapter=adapter)
    result = await executor.execute(
        action=BrowserAction(
            action=ActionType.CLICK,
            element_id="el_001",
            reason="nav",
            metadata={"name": "Sign up", "action_label": "Sign up"},
        ),
        page_state=page_state,
        capture_evidence=False,
    )
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.remember_action(
        result,
        before_fingerprint=page_state.state_fingerprint,
        made_progress=True,
    )
    assert memory.navigation_edges
    edge = memory.navigation_edges[-1]
    assert "example.com" in edge.source_url
    assert "signup" in edge.target_url.lower()
    await adapter.close_session()


def test_report_identifies_active_adapter():
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.browser_adapter_id = "playwright_mcp"
    memory.adapter_connection_status = "healthy"
    memory.adapter_capabilities = AdapterCapabilities(
        console_events=False, network_events=False
    ).to_dict()
    memory.unsupported_evidence_features = ["console_events", "network_events"]
    memory.adapter_execution_failures = 2
    memory.provider_type = "mock"
    builder = ReportBuilder()
    report = builder.build(memory)
    section = report.sections_markdown.get("Run Environment") or ""
    assert "Playwright MCP" in section
    assert "healthy" in section.lower() or "MCP connection" in section
    assert "unsupported" in section.lower()
    assert "2" in section
    assert report.runtime.get("browser_adapter_id") == "playwright_mcp"


def test_snapshot_to_observation_normalises():
    raw = snapshot_to_observation(
        {
            "role": "WebArea",
            "name": "Home",
            "children": [
                {"role": "link", "name": "Sign Up", "ref": "e1"},
                {"role": "button", "name": "Go", "ref": "e2"},
            ],
        },
        url="https://example.com/",
        title="Home",
    )
    assert raw["url"] == "https://example.com/"
    assert len(raw["interactive_elements"]) >= 2


@pytest.mark.asyncio
async def test_legacy_page_executor_still_works():
    """Direct Playwright compatibility: page-only execute path."""
    executor = ActionExecutor(run_id=new_id())
    page = AsyncMock()
    page.url = "https://example.com"
    page.title = AsyncMock(return_value="Example")
    action = BrowserAction(action=ActionType.FINISH, reason="done")
    result = await executor.execute(page, action, capture_evidence=False)
    assert result.success is True


@pytest.mark.skipif(
    os.environ.get("GEMMAQA_LIVE_MCP") != "1",
    reason="Set GEMMAQA_LIVE_MCP=1 with a running Playwright MCP server",
)
@pytest.mark.asyncio
async def test_live_mcp_optional():
    from app.browser.adapters import create_browser_adapter

    adapter = create_browser_adapter("live-mcp", adapter_name="playwright_mcp")
    await adapter.start_session()
    try:
        url = await adapter.navigate("https://example.com/")
        assert "example.com" in url
        obs = await adapter.get_observation_payload()
        assert obs
    finally:
        await adapter.close_session()
