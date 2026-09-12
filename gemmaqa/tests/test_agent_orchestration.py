"""Autonomous agent orchestration tests (mock Gemma + local demo site)."""

from __future__ import annotations

import asyncio
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_demo"
sys.path.insert(0, str(BACKEND))

from app.agent.bug_analyzer import BugAnalyzer  # noqa: E402
from app.agent.controller import AgentController  # noqa: E402
from app.agent.documenter import Documenter  # noqa: E402
from app.agent.memory import RunMemory, action_signature  # noqa: E402
from app.agent.planner import Planner  # noqa: E402
from app.database import AsyncSessionLocal, init_db  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.schemas import (  # noqa: E402
    ActionResult,
    ActionType,
    BrowserAction,
    CreateRunRequest,
    InteractiveElement,
    PageState,
    RiskLevel,
    RunConfiguration,
)
from app.utils.ids import new_id  # noqa: E402


@pytest.fixture(scope="module")
def demo_server():
    handler = partial(SimpleHTTPRequestHandler, directory=str(FIXTURES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _page(url: str = "https://example.com", *element_ids: str) -> PageState:
    els = [
        InteractiveElement(
            element_id=eid,
            tag="a",
            category="link",
            href=f"/{eid}",
            accessible_name=eid,
            visible_text=eid,
            is_visible=True,
        )
        for eid in element_ids
    ]
    return PageState(
        page_id=new_id(),
        url=url,
        title="T",
        interactive_elements=els,
        state_fingerprint=f"fp-{url}",
    )


def test_action_signature_and_loop_detection():
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.configuration = RunConfiguration(max_actions=20, max_pages=10)
    memory.bootstrap_budgets(memory.configuration)

    # Simulate A-B-A-B navigation
    for url in ["http://x/a", "http://x/b", "http://x/a", "http://x/b"]:
        result = ActionResult(
            action_id=new_id(),
            run_id=memory.run_id,
            action=BrowserAction(action=ActionType.CLICK, reason="nav", expected_result="ok", risk=RiskLevel.LOW),
            success=True,
            before_url="http://x",
            after_url=url,
        )
        memory.remember_action(result, before_fingerprint="fp", made_progress=True)

    assert memory.detect_navigation_loop() is True
    # Phase 6: should_stop() now uses the canonical stop-reason vocabulary
    # ("persistent_navigation_loop", not the old raw "navigation_loop") and, with no
    # build_frontier/goals/unexplored_urls available here, correctly finds no
    # alternative before reporting the loop as persistent.
    stop, reason = memory.should_stop(5)
    assert stop and reason == "persistent_navigation_loop"

    sig = action_signature(
        page_fingerprint="fp1",
        action_type="click",
        element_id="el_001",
        value_category="empty",
    )
    memory.mark_signature(sig)
    assert memory.has_seen_signature(sig)


def test_agent_does_not_stop_on_action_count():
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_actions=2, max_pages=10))
    memory.remaining_action_budget = 0
    stop, reason = memory.should_stop()
    assert not stop
    assert reason is None


def test_agent_detects_http_500():
    analyzer = BugAnalyzer(gemma=None)
    page = _page("https://app.example.com/x")
    page.network_failures = ["500 GET https://app.example.com/api/items"]
    findings = analyzer.deterministic_signals(page)
    assert any(f.title.startswith("HTTP 500") for f in findings)
    assert findings[0].classification.value == "confirmed_bug"


def test_agent_records_required_field_issue():
    analyzer = BugAnalyzer(gemma=None)
    page = _page("https://app.example.com/form")
    page.alerts = []
    action = BrowserAction(
        action=ActionType.FILL,
        element_id="el_001",
        value="",
        reason="Safe empty-value check on required Name",
        expected_result="validation",
        risk=RiskLevel.LOW,
        metadata={"value_category": "empty"},
    )
    result = ActionResult(
        action_id=new_id(),
        run_id=new_id(),
        action=action,
        success=True,
    )
    findings = analyzer.deterministic_signals(page, action_result=result)
    assert any("Required field" in f.title for f in findings)


def test_agent_updates_documentation():
    memory = RunMemory(run_id=new_id(), start_url="http://127.0.0.1/demo")
    memory.bootstrap_budgets(RunConfiguration())
    memory.remember_page(_page("http://127.0.0.1/demo", "el_001"))
    doc = Documenter(gemma=MockGemmaProvider())

    sections = asyncio.run(doc.sync(memory))
    assert "Product Overview" in sections
    assert "Confirmed Bugs" in sections
    assert "Coverage Summary" in sections
    assert "Executive Summary" in sections
    assert memory.doc_sections["Page Inventory"]
    assert len(sections) >= 25


def test_invalid_model_output_falls_back():
    gemma = MockGemmaProvider(scripted_responses=["NOT JSON", "STILL BAD"])
    planner = Planner(gemma)
    page = _page("http://x", "el_001")
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_actions=10, max_pages=5))
    action = asyncio.run(
        planner.next_action(
            page_state=page,
            previous_actions=[],
            unexplored=[],
            context={"safe_mode": True, "authorized_domain": "x"},
            memory=memory,
        )
    )
    assert action.risk == RiskLevel.LOW
    assert action.action in {ActionType.CLICK, ActionType.FINISH, ActionType.INSPECT_FORM}


def test_completes_when_no_safe_actions_remain():
    gemma = MockGemmaProvider()
    planner = Planner(gemma)
    # Empty page — no interactive elements
    page = PageState(page_id=new_id(), url="http://x/empty", title="Empty")
    memory = RunMemory(run_id=new_id(), start_url="http://x/empty")
    memory.bootstrap_budgets(RunConfiguration(max_actions=10, max_pages=5))
    action = planner.plan_by_priority(page, memory, {"authorized_domain": "x", "safe_mode": True})
    assert action.action == ActionType.FINISH


def test_agent_explores_three_pages_and_preserves_evidence(demo_server):
    asyncio.run(init_db())

    request = CreateRunRequest(
        url=f"{demo_server}/index.html",
        configuration=RunConfiguration(
            max_pages=3,
            max_actions=12,
            safe_mode=True,
            allow_controlled_writes=False,
            headless=True,
        ),
    )
    run_id = new_id()
    events: list[str] = []

    async def on_event(event_type: str, data: dict) -> None:
        events.append(event_type)

    controller = AgentController(
        run_id=run_id,
        request=request,
        db_factory=AsyncSessionLocal,
        on_event=on_event,
        gemma=MockGemmaProvider(),
    )

    # Persist a shell run row so updates succeed
    async def seed():
        from app.models import QARun

        async with AsyncSessionLocal() as session:
            session.add(
                QARun(
                    id=run_id,
                    url=request.url,
                    status="created",
                    config_json=request.configuration.model_dump_json(),
                )
            )
            await session.commit()

    asyncio.run(seed())
    asyncio.run(controller.run())

    assert len(controller.memory.visited_urls) >= 2
    # max_pages is no longer a stop limit; visiting every demo page is fine.
    assert controller.memory.doc_sections
    assert "state_changed" in events or "page_observed" in events
    assert "run_completed" in events or "run_failed" in events

    evidence_root = Path(controller.settings.run_evidence_dir(run_id))
    # evidence collector created inside run — path layout
    from app.config import get_settings

    root = get_settings().evidence_dir / run_id
    assert root.exists()
    assert (root / "screenshots").exists() or any(root.rglob("*.png"))


def test_blocked_actions_are_persisted_to_db(demo_server):
    """Regression: when the safety validator rejects a proposed action, the
    controller used to count it via remember_action() but never called
    _persist_action() — so rejected decisions were invisible in the DB.
    """
    from unittest.mock import patch

    from app.safety.validator import ActionValidator, ValidationResult

    asyncio.run(init_db())

    request = CreateRunRequest(
        url=f"{demo_server}/index.html",
        configuration=RunConfiguration(
            safe_mode=True,
            allow_controlled_writes=False,
            headless=True,
        ),
    )
    run_id = new_id()

    async def on_event(event_type: str, data: dict) -> None:
        pass

    controller = AgentController(
        run_id=run_id,
        request=request,
        db_factory=AsyncSessionLocal,
        on_event=on_event,
        gemma=MockGemmaProvider(),
    )

    async def seed():
        from app.models import QARun

        async with AsyncSessionLocal() as session:
            session.add(
                QARun(
                    id=run_id,
                    url=request.url,
                    status="created",
                    config_json=request.configuration.model_dump_json(),
                )
            )
            await session.commit()

    _real_validate = ActionValidator.validate
    blocked_once = {"done": False}

    def _block_first_non_finish(self, action, *args, **kwargs):
        if not blocked_once["done"] and action.action != ActionType.FINISH:
            blocked_once["done"] = True
            return ValidationResult(False, reason="Blocked URL pattern: /billing/pay")
        return _real_validate(self, action, *args, **kwargs)

    asyncio.run(seed())
    with patch.object(ActionValidator, "validate", _block_first_non_finish):
        asyncio.run(controller.run())

    assert controller.memory.decisions_rejected >= 1

    async def count_persisted() -> int:
        from sqlalchemy import select

        from app.models import ActionRecord

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ActionRecord).where(ActionRecord.run_id == run_id)
            )
            return len(result.scalars().all())

    persisted_count = asyncio.run(count_persisted())
    assert persisted_count == len(controller.memory.actions)


def test_href_less_control_not_deprioritized_below_placeholder_links():
    """Regression: a real app (SauceDemo) implements its cart icon as a plain
    <a class="shopping_cart_link"> with NO href at all (pure onClick JS handler) — a
    completely legitimate, common pattern. _rank_elements() previously gave priority 1
    to ANY anchor with a truthy href, including meaningless placeholder href="#" values
    used by ordinary product links, while genuinely functional href-less controls like
    the cart icon sank to the bottom and were never explored — an entire module
    (Cart/Checkout) was unreachable as a direct result."""
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_actions=20, max_pages=10))
    page = PageState(
        page_id=new_id(),
        url="http://x",
        title="T",
        interactive_elements=[
            InteractiveElement(
                element_id="el_placeholder",
                tag="a",
                category="link",
                href="#",
                accessible_name="Some Product",
                is_visible=True,
            ),
            InteractiveElement(
                element_id="el_cart",
                tag="a",
                category="link",
                href=None,
                accessible_name="3",
                is_visible=True,
            ),
        ],
    )
    planner = Planner(MockGemmaProvider())
    ranked = planner._rank_elements(page, memory)
    rank_position = {el.element_id: idx for idx, (el, _, _) in enumerate(ranked)}
    assert rank_position["el_cart"] <= rank_position["el_placeholder"], (
        "href-less but functional control should not rank below a '#' placeholder link"
    )


def test_nav_hint_does_not_match_substring_inside_unrelated_word():
    """Regression: NAV_HINTS includes "back" (for "Back"/"Go back" controls), and a
    plain substring check matched it inside "Sauce Labs Backpack" — an unrelated
    product name — giving that one product link an undeserved priority-1 ranking
    over every other equally-valid, unvisited element on the page."""
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_actions=20, max_pages=10))
    page = PageState(
        page_id=new_id(),
        url="http://x",
        title="T",
        interactive_elements=[
            InteractiveElement(
                element_id="el_backpack",
                tag="a",
                category="link",
                href="#",
                accessible_name="Sauce Labs Backpack",
                is_visible=True,
            ),
        ],
    )
    planner = Planner(MockGemmaProvider())
    ranked = planner._rank_elements(page, memory)
    _, reason, _ = next(r for r in ranked if r[0].element_id == "el_backpack")
    assert reason != "Main navigation / unvisited link"


def test_blocked_href_not_reproposed_by_rank_elements():
    """Once the validator has blocked a link (e.g. an external social-media icon),
    _rank_elements() must never surface it again — element IDs get renumbered on every
    page load, so this has to be tracked by the actual href, not element_id."""
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_actions=20, max_pages=10))
    memory.remember_blocked_href("https://twitter.com/saucelabs")
    page = PageState(
        page_id=new_id(),
        url="http://x",
        title="T",
        interactive_elements=[
            InteractiveElement(
                element_id="el_twitter",
                tag="a",
                category="link",
                href="https://twitter.com/saucelabs",
                accessible_name="Twitter",
                is_visible=True,
            ),
        ],
    )
    planner = Planner(MockGemmaProvider())
    ranked = planner._rank_elements(page, memory)
    assert not any(el.element_id == "el_twitter" for el, _, _ in ranked)


def test_blocked_href_not_reproposed_by_fallback_safe_action():
    """Same guarantee for the mock provider's own deterministic heuristic, which
    drives most turns directly (not just the plan_by_priority fallback)."""
    from app.gemma.parser import fallback_safe_action

    page = PageState(
        page_id=new_id(),
        url="http://x",
        title="T",
        interactive_elements=[
            InteractiveElement(
                element_id="el_twitter",
                tag="a",
                category="link",
                href="https://twitter.com/saucelabs",
                accessible_name="Twitter",
                is_visible=True,
            ),
        ],
    )
    action = fallback_safe_action(
        page_state=page,
        remaining_action_budget=10,
        blocked_hrefs={"https://twitter.com/saucelabs"},
    )
    assert action.element_id != "el_twitter"
    assert action.action == ActionType.FINISH


def test_duplicate_links_to_same_product_collapsed_to_one_candidate():
    """Regression: SauceDemo (and many product-card UIs) render TWO separate anchors
    per item — an image wrapper and a title link — both routing to the same detail
    page. Ranked as two distinct "unexplored" candidates, exploration burned two turns
    revisiting the SAME product before ever reaching a different one, so 5 of 6
    products were never discovered even with ample action budget."""
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_actions=20, max_pages=10))
    page = PageState(
        page_id=new_id(),
        url="http://x",
        title="T",
        interactive_elements=[
            InteractiveElement(
                element_id="el_image_wrapper",
                tag="a",
                category="link",
                href="#",
                accessible_name="Sauce Labs Backpack",  # via child <img alt> fallback
                is_visible=True,
            ),
            InteractiveElement(
                element_id="el_title_link",
                tag="a",
                category="link",
                href="#",
                accessible_name="Sauce Labs Backpack",
                is_visible=True,
            ),
            InteractiveElement(
                element_id="el_other_product",
                tag="a",
                category="link",
                href="#",
                accessible_name="Sauce Labs Bike Light",
                is_visible=True,
            ),
        ],
    )
    planner = Planner(MockGemmaProvider())
    ranked = planner._rank_elements(page, memory)
    backpack_candidates = [el for el, _, _ in ranked if el.accessible_name == "Sauce Labs Backpack"]
    assert len(backpack_candidates) == 1
    assert any(el.accessible_name == "Sauce Labs Bike Light" for el, _, _ in ranked)


def test_agent_avoids_repeat_signature():
    memory = RunMemory(run_id=new_id(), start_url="http://x")
    memory.bootstrap_budgets(RunConfiguration(max_actions=20, max_pages=10))
    page = _page("http://x", "el_001", "el_002")
    page.state_fingerprint = "fpA"
    sig = action_signature(
        page_fingerprint="fpA",
        action_type="click",
        element_id="el_001",
    )
    memory.mark_signature(sig)
    planner = Planner(MockGemmaProvider())
    # Force priority planner path
    action = planner.plan_by_priority(page, memory, {"authorized_domain": "x", "safe_mode": True})
    assert action.element_id != "el_001" or action.action == ActionType.FINISH
