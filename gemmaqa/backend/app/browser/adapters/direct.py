"""Direct Playwright browser adapter — wraps existing BrowserManager."""

from __future__ import annotations

from typing import Any

from app.browser.custom_controls import check_or_force, click_element, fill_or_choose
from playwright.async_api import Locator

from app.browser.adapters.base import BrowserAdapter
from app.browser.adapters.types import AdapterCapabilities, TargetRef
from app.browser.console_monitor import ConsoleMonitor
from app.browser.manager import BrowserManager
from app.browser.network_monitor import NetworkMonitor
from app.browser.observer import OBSERVE_SCRIPT
from app.utils.logging import get_logger

logger = get_logger("browser.adapter.direct")


class DirectPlaywrightAdapter(BrowserAdapter):
    """Default production browser path: in-process Playwright Chromium."""

    name = "direct_playwright"

    def __init__(
        self,
        run_id: str,
        *,
        headless: bool | None = None,
        manager: BrowserManager | None = None,
        console: ConsoleMonitor | None = None,
        network: NetworkMonitor | None = None,
    ) -> None:
        self.run_id = run_id
        self.manager = manager or BrowserManager(run_id=run_id, headless=headless)
        self.console = console or ConsoleMonitor()
        self.network = network or NetworkMonitor()
        self._attached = False

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(tabs=True)

    async def start_session(self) -> None:
        page = await self.manager.start()
        if not self._attached:
            self.console.attach(page)
            self.network.attach(page)
            self._attached = True
        logger.info("Browser adapter active: Direct Playwright (run=%s)", self.run_id)

    async def close_session(self) -> None:
        await self.manager.stop()
        self._attached = False

    @property
    def native_page(self) -> Any | None:
        return self.manager.page

    async def navigate(self, url: str) -> str:
        page = self.manager.require_page()
        await page.goto(url, wait_until="domcontentloaded")
        return page.url

    async def go_back(self) -> str:
        page = self.manager.require_page()
        await page.go_back(wait_until="domcontentloaded")
        return page.url

    async def reload(self) -> str:
        page = self.manager.require_page()
        await page.reload(wait_until="domcontentloaded")
        return page.url

    async def resolve_target(self, target: TargetRef) -> TargetRef:
        locator = await self._locator_for(target)
        try:
            if await locator.count() == 0:
                raise ValueError(f"Target not found: {target.display()}")
        except TypeError as exc:
            raise ValueError(f"Target not found: {target.display()}") from exc
        return target

    async def element_exists(self, target: TargetRef) -> bool:
        try:
            locator = await self._locator_for(target)
            return await locator.count() > 0
        except Exception:
            return False

    async def click_target(self, target: TargetRef) -> None:
        locator = await self._require_locator(target)
        await locator.scroll_into_view_if_needed()
        # Shared with the executor's native path, for the same reason `fill_target`
        # shares `fill_or_choose`: a rule about how to operate a control that has
        # two implementations ends up with two answers.
        await click_element(locator)

    async def fill_target(self, target: TargetRef, value: str):
        locator = await self._require_locator(target)
        # A div-based chooser cannot receive typed text, and a lookup accepts
        # only records the application already holds — see
        # app.browser.custom_controls. Shared with the executor's native path so
        # the two cannot diverge, and the outcome is returned for the same
        # reason: a value the field did NOT end up holding must not be reported
        # as though it did.
        return await fill_or_choose(locator, value)

    async def select_target(self, target: TargetRef, value: str) -> None:
        locator = await self._require_locator(target)
        await locator.select_option(value)

    async def check_target(self, target: TargetRef, checked: bool = True) -> None:
        locator = await self._require_locator(target)
        # Shared with the executor's native path: an application that styles the
        # real checkbox out of sight otherwise costs a 15s actionability timeout
        # per tick. See app.browser.custom_controls.check_or_force.
        await check_or_force(locator, checked)

    async def hover_target(self, target: TargetRef) -> None:
        locator = await self._require_locator(target)
        await locator.hover()

    async def press_target(self, target: TargetRef, key: str) -> None:
        locator = await self._require_locator(target)
        await locator.press(key)

    async def take_screenshot(self, path: str, *, full_page: bool = False) -> str:
        page = self.manager.require_page()
        await page.screenshot(path=path, full_page=full_page)
        return path

    async def get_current_url(self) -> str:
        return self.manager.require_page().url

    async def get_page_title(self) -> str:
        return await self.manager.require_page().title()

    async def get_accessibility_snapshot(self) -> dict[str, Any] | str:
        page = self.manager.require_page()
        try:
            return await page.accessibility.snapshot() or {}
        except Exception:
            return {"url": page.url, "title": await page.title()}

    async def get_observation_payload(self) -> dict[str, Any]:
        page = self.manager.require_page()
        raw = await page.evaluate(OBSERVE_SCRIPT)
        return raw if isinstance(raw, dict) else {}

    async def get_console_events(self) -> list[str]:
        return list(self.console.errors)

    async def get_network_events(self) -> list[str]:
        return list(self.network.failures)

    async def wait_for_page_stable(self, timeout_ms: int = 3000) -> None:
        page = self.manager.require_page()
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass

    async def wait(self, ms: int) -> None:
        await self.manager.require_page().wait_for_timeout(ms)

    async def list_tabs(self) -> list[dict[str, Any]]:
        ctx = self.manager.context
        if not ctx:
            return []
        tabs = []
        for idx, p in enumerate(ctx.pages):
            tabs.append({"tab_id": str(idx), "url": p.url, "title": await p.title()})
        return tabs

    async def switch_tab(self, tab_id: str) -> None:
        ctx = self.manager.context
        if not ctx:
            raise RuntimeError("No browser context")
        pages = ctx.pages
        idx = int(tab_id)
        if idx < 0 or idx >= len(pages):
            raise ValueError(f"Unknown tab_id: {tab_id}")
        self.manager.page = pages[idx]
        await pages[idx].bring_to_front()

    async def health_check(self) -> dict[str, Any]:
        ok = bool(self.manager.page and not self.manager.page.is_closed())
        return {
            "adapter": self.name,
            "available": True,
            "session_active": ok,
            "engine": "playwright-chromium",
            "headless": self.manager.headless,
            "capabilities": self.capabilities().to_dict(),
        }

    async def _require_locator(self, target: TargetRef) -> Locator:
        await self.resolve_target(target)
        return await self._locator_for(target)

    async def _locator_for(self, target: TargetRef) -> Locator:
        page = self.manager.require_page()
        candidates: list[Locator] = []
        if target.test_id:
            candidates.append(page.get_by_test_id(target.test_id))
            candidates.append(page.locator(f'[data-testid="{target.test_id}"]'))
        if target.target_id:
            candidates.append(page.locator(f'[data-gemmaqa-id="{target.target_id}"]'))
        if target.selector_hint:
            candidates.append(page.locator(target.selector_hint))
        if target.role and target.name:
            candidates.append(page.get_by_role(target.role, name=target.name))
        if target.name:
            candidates.append(page.get_by_label(target.name))
            candidates.append(page.get_by_placeholder(target.name))
            candidates.append(page.get_by_text(target.name, exact=False))
        if target.href:
            candidates.append(page.locator(f'a[href="{target.href}"]'))

        for loc in candidates:
            first = loc.first
            try:
                if await first.count() > 0:
                    return first
            except Exception:
                continue
        # Last resort empty locator
        return page.locator("[data-gemmaqa-missing='1']").first
