"""Execute validated BrowserAction commands via BrowserAdapter (or legacy Page)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from playwright.async_api import Locator, Page

from app.browser.adapters.base import BrowserAdapter
from app.browser.adapters.types import AdapterCapabilities, TargetRef
from app.browser.console_monitor import ConsoleMonitor
from app.browser.evidence import EvidenceCollector
from app.browser.fingerprint import fingerprint_page_state
from app.browser.locators import LocatorRegistry, resolve_selector
from app.browser.network_monitor import NetworkMonitor
from app.browser.observer import PageObserver
from app.schemas import ActionResult, ActionType, BrowserAction, InteractiveElement, PageState
from app.utils.ids import new_id
from app.browser.custom_controls import (
    FillOutcome,
    accepting_native_dialogs,
    check_or_force,
    click_element,
    fill_or_choose,
)
from app.browser.live_indicator import (
    show_live_indicator,
    show_live_indicator_for_adapter,
)
from app.safety.policies import CLEANUP_RISK_CLASS
from app.utils.logging import get_logger

logger = get_logger("browser.executor")

ELEMENT_ACTIONS = {
    ActionType.CLICK,
    ActionType.FILL,
    ActionType.SELECT,
    ActionType.CHECK,
    ActionType.UNCHECK,
    ActionType.PRESS,
    ActionType.HOVER,
    ActionType.OPEN_TAB,
}

_CLEANUP_DIALOG_STEPS = frozenset({"delete_control", "confirm_delete"})


def cleanup_click_accepts_native_dialog(action: BrowserAction) -> bool:
    """True only for an authorized Temporary Record Registry cleanup click.

    Contact List (and similar apps) delete with a native `window.confirm`.
    Playwright's default is to dismiss that dialog, so the click "succeeds"
    and the record stays. Accepting is correct here; auto-accepting confirms
    on arbitrary clicks is not.
    """
    meta = action.metadata or {}
    return (
        meta.get("risk_class") == CLEANUP_RISK_CLASS
        and meta.get("cleanup_step") in _CLEANUP_DIALOG_STEPS
    )


async def _click_accepting_cleanup_dialog(action: BrowserAction, page: Any, click) -> None:
    dialog_page = page if cleanup_click_accepts_native_dialog(action) else None
    async with accepting_native_dialogs(dialog_page):
        await click()


ACTION_CAPABILITY: dict[ActionType, str] = {
    ActionType.OPEN_URL: "navigate",
    ActionType.GO_BACK: "go_back",
    ActionType.REFRESH: "reload",
    ActionType.WAIT: "wait",
    ActionType.TAKE_SCREENSHOT: "screenshots",
    ActionType.CLICK: "click",
    ActionType.FILL: "fill",
    ActionType.SELECT: "select",
    ActionType.CHECK: "check",
    ActionType.UNCHECK: "check",
    ActionType.PRESS: "press",
    ActionType.HOVER: "hover",
    ActionType.OPEN_TAB: "click",
    ActionType.INSPECT_FORM: "observe",
    ActionType.INSPECT_TABLE: "observe",
}

# How long to give a submit's write to START. A form that submits over the
# network sends its request within a few hundred ms of the click. If nothing has
# started by now there is nothing to wait for — client-side validation blocked
# the submit, or the form does not use XHR at all — and waiting longer would only
# slow every such submit down.
SUBMIT_WRITE_START_TIMEOUT_MS = 1500
# How long to then give it to FINISH. The application's own answer is the only
# direct evidence of what it did with the data, so it is worth waiting for.
SUBMIT_WRITE_COMPLETE_TIMEOUT_MS = 8000
_SUBMIT_WRITE_POLL_MS = 100


class ActionExecutor:
    """Maps structured actions onto BrowserAdapter (preferred) or Playwright Page."""

    def __init__(
        self,
        run_id: str,
        *,
        registry: LocatorRegistry | None = None,
        evidence: EvidenceCollector | None = None,
        console_monitor: ConsoleMonitor | None = None,
        network_monitor: NetworkMonitor | None = None,
        observer: PageObserver | None = None,
        adapter: BrowserAdapter | None = None,
        settle_ms: int = 400,
        credential_vault: Any | None = None,
    ) -> None:
        self.run_id = run_id
        self.registry = registry or LocatorRegistry()
        self.evidence = evidence
        self.console = console_monitor
        self.network = network_monitor
        self.observer = observer
        self.adapter = adapter
        self.settle_ms = settle_ms
        self.credential_vault = credential_vault
        self.adapter_execution_failures = 0
        self.capability_blocks = 0
        # Optional. When unset, speed=1x / pause=0s — identical to today's timing.
        self.pacing: Any | None = None

    async def execute(
        self,
        page: Page | None = None,
        action: BrowserAction | None = None,
        page_state: PageState | None = None,
        before_screenshot: str | None = None,
        after_screenshot: str | None = None,
        *,
        capture_evidence: bool = True,
        adapter: BrowserAdapter | None = None,
        **kwargs: Any,
    ) -> ActionResult:
        """
        Execute a validated action.

        Preferred: ActionExecutor(adapter=...).execute(action=..., page_state=...)
        Legacy: execute(page, action, page_state=...)
        """
        # Support legacy positional: execute(page, action, ...)
        if action is None and isinstance(page, BrowserAction):
            action = page
            page = None
        if action is None:
            # kwargs form
            action = kwargs.get("action")  # type: ignore[assignment]
        if action is None:
            raise TypeError("action is required")

        use_adapter = adapter or self.adapter
        if use_adapter is not None:
            result = await self._execute_via_adapter(
                use_adapter,
                action,
                page_state=page_state,
                before_screenshot=before_screenshot,
                after_screenshot=after_screenshot,
                capture_evidence=capture_evidence,
            )
        elif page is None:
            raise RuntimeError(
                "ActionExecutor requires a BrowserAdapter or a Playwright Page"
            )
        else:
            result = await self._execute_via_page(
                page,
                action,
                page_state=page_state,
                before_screenshot=before_screenshot,
                after_screenshot=after_screenshot,
                capture_evidence=capture_evidence,
            )
        if self.pacing is not None:
            await self.pacing.after_action(skip=action.action == ActionType.FINISH)
        return result

    async def _wait_for_highlight(self) -> None:
        if self.pacing is None:
            return
        await self.pacing.wait_for_highlight()

    def _highlight_ms(self) -> int | None:
        if self.pacing is None:
            return None
        return self.pacing.highlight_ms()

    # ------------------------------------------------------------------
    # Adapter path (works without native_page)
    # ------------------------------------------------------------------

    async def _execute_via_adapter(
        self,
        adapter: BrowserAdapter,
        action: BrowserAction,
        *,
        page_state: PageState | None,
        before_screenshot: str | None,
        after_screenshot: str | None,
        capture_evidence: bool,
    ) -> ActionResult:
        action_id = new_id()
        before_url = ""
        try:
            before_url = await adapter.get_current_url()
        except Exception:
            before_url = page_state.url if page_state else ""
        started = time.perf_counter()
        success = False
        message = ""
        error: str | None = None
        inspected: dict[str, Any] = {}
        evidence_ids: list[str] = []
        before_fp = page_state.state_fingerprint if page_state else None
        after_fp = before_fp
        console_before = await adapter.get_console_events()
        network_before = await adapter.get_network_events()
        caps = adapter.capabilities()

        # Capability gate (except finish / inspect which are local)
        cap_name = ACTION_CAPABILITY.get(action.action)
        if action.action != ActionType.FINISH and cap_name:
            if not getattr(caps, cap_name, True):
                self.capability_blocks += 1
                return ActionResult(
                    action_id=action_id,
                    run_id=self.run_id,
                    action=action,
                    success=False,
                    message=f"Blocked: adapter lacks capability '{cap_name}'",
                    before_url=before_url,
                    after_url=before_url,
                    error=f"capability_unavailable:{cap_name}",
                    duration_ms=0,
                    evidence_ids=[],
                    inspected={"adapter": adapter.name, "capability": cap_name},
                )

        if capture_evidence and self.evidence and not before_screenshot:
            try:
                shot = await self.evidence.before_action_adapter(
                    adapter, action.action.value, action_id
                )
                before_screenshot = shot.path
                evidence_ids.append(shot.evidence_id)
            except Exception as exc:
                logger.warning("Before screenshot (adapter) skipped: %s", type(exc).__name__)
                if not caps.screenshots:
                    inspected["screenshot_limitation"] = "screenshots unsupported by adapter"

        try:
            if action.action == ActionType.FINISH:
                success = True
                message = "Finish signal accepted"

            elif action.action == ActionType.OPEN_URL:
                url = action.url or action.value
                if not url:
                    raise ValueError("open_url missing url")
                await adapter.navigate(url)
                await adapter.wait_for_page_stable()
                success = True
                message = f"Opened {url}"

            elif action.action == ActionType.GO_BACK:
                await adapter.go_back()
                await adapter.wait_for_page_stable()
                success = True
                message = "Navigated back"

            elif action.action == ActionType.REFRESH:
                await adapter.reload()
                await adapter.wait_for_page_stable()
                success = True
                message = "Page refreshed"

            elif action.action == ActionType.WAIT:
                await adapter.wait(action.wait_ms or 1000)
                success = True
                message = f"Waited {action.wait_ms or 1000}ms"

            elif action.action == ActionType.TAKE_SCREENSHOT:
                if self.evidence:
                    shot = await self.evidence.full_page_screenshot_adapter(
                        adapter, "manual", action_id
                    )
                    evidence_ids.append(shot.evidence_id)
                    after_screenshot = shot.path
                success = True
                message = "Screenshot captured"

            elif action.action == ActionType.INSPECT_FORM:
                inspected = self._inspect_form(page_state, action.element_id)
                success = True
                message = f"Inspected form {action.element_id or ''}".strip()

            elif action.action == ActionType.INSPECT_TABLE:
                inspected = self._inspect_table(page_state, action.element_id)
                success = True
                message = f"Inspected table {action.element_id or ''}".strip()

            elif action.action in ELEMENT_ACTIONS:
                target = self._target_from_action(action, page_state)
                await adapter.resolve_target(target)
                # Observational only: ring + label, then the same click/fill as before.
                await show_live_indicator_for_adapter(
                    adapter, target, action, page_state, highlight_ms=self._highlight_ms()
                )
                fill_value = self._resolve_fill_value(action)
                # Read the write counter before anything happens, for the same
                # reason the page path does. THIS is the path production takes —
                # `browser_adapter: direct_playwright` — and it settled with a
                # plain `adapter.wait(settle_ms)` that knew nothing about submits.
                # Measured on run 5b4268e0: an Add Candidate submit executed in
                # 1091ms, far too short to have waited for anything, and the create
                # was recorded `submitted_unverified` all over again. The write
                # wait had been wired into `_execute_via_page` only — one rule,
                # two homes, and the fix went to the home that is not used.
                submit_mark = self._submit_write_mark(action)
                if action.action == ActionType.CLICK or action.action == ActionType.OPEN_TAB:
                    await _click_accepting_cleanup_dialog(
                        action,
                        getattr(adapter, "native_page", None),
                        lambda: adapter.click_target(target),
                    )
                elif action.action == ActionType.FILL:
                    fill_outcome = await adapter.fill_target(target, fill_value)
                    if fill_outcome is not None and fill_outcome.kind != "fill":
                        inspected["fill_outcome"] = fill_outcome.to_dict()
                elif action.action == ActionType.SELECT:
                    await adapter.select_target(target, fill_value)
                elif action.action == ActionType.CHECK:
                    await adapter.check_target(target, True)
                elif action.action == ActionType.UNCHECK:
                    await adapter.check_target(target, False)
                elif action.action == ActionType.PRESS:
                    await adapter.press_target(
                        target, action.key or action.value or "Enter"
                    )
                elif action.action == ActionType.HOVER:
                    await adapter.hover_target(target)
                extra_ms = 1600 if (action.metadata or {}).get("auth_submit") else 0
                await adapter.wait(self.settle_ms + extra_ms)
                await adapter.wait_for_page_stable()
                if submit_mark is not None:
                    await self._await_submitted_write(adapter.wait, submit_mark)
                success = True
                message = f"{action.action.value} on {action.element_id}"

            else:
                raise ValueError(f"Unsupported action: {action.action}")

        except Exception as exc:
            self.adapter_execution_failures += 1
            error = str(exc)
            message = f"Action failed: {exc}"
            logger.warning("Adapter action %s failed: %s", action.action, exc)

        after_url = before_url
        try:
            after_url = await adapter.get_current_url()
        except Exception:
            pass

        await self._wait_for_highlight()

        if (
            capture_evidence
            and self.evidence
            and not after_screenshot
            and action.action != ActionType.TAKE_SCREENSHOT
        ):
            try:
                shot = await self.evidence.after_action_adapter(
                    adapter, action.action.value, action_id
                )
                after_screenshot = shot.path
                evidence_ids.append(shot.evidence_id)
            except Exception as exc:
                logger.warning("After screenshot (adapter) skipped: %s", type(exc).__name__)

        try:
            if self.observer:
                after_state = await self.observer.observe_adapter(
                    adapter, screenshot_path=after_screenshot
                )
                after_fp = after_state.state_fingerprint
            else:
                title = await adapter.get_page_title()
                after_fp = fingerprint_page_state(
                    PageState(
                        page_id=page_state.page_id if page_state else new_id(),
                        url=after_url,
                        title=title,
                        headings=page_state.headings if page_state else [],
                        interactive_elements=(
                            page_state.interactive_elements if page_state else []
                        ),
                    )
                )
        except Exception as exc:
            logger.debug("Post-action fingerprint failed: %s", exc)

        console_after = await adapter.get_console_events()
        network_after = await adapter.get_network_events()
        new_console = [e for e in console_after if e not in console_before]
        new_network = [e for e in network_after if e not in network_before]
        if not caps.console_events:
            inspected.setdefault("console_limitation", "console capture unsupported")
        if not caps.network_events:
            inspected.setdefault("network_limitation", "network capture unsupported")

        if self.evidence:
            try:
                title = await adapter.get_page_title()
                meta = self.evidence.record_metadata(
                    url=after_url,
                    title=title,
                    console_errors=new_console,
                    network_errors=new_network,
                    action={
                        "action_id": action_id,
                        "action": action.action.value,
                        "element_id": action.element_id,
                        "success": success,
                        "error": error,
                        "adapter": adapter.name,
                    },
                )
                evidence_ids.append(meta.evidence_id)
            except Exception:
                pass

        duration_ms = int((time.perf_counter() - started) * 1000)
        inspected = {**inspected, "adapter": adapter.name}
        return ActionResult(
            action_id=action_id,
            run_id=self.run_id,
            action=action,
            success=success,
            message=message,
            before_url=before_url,
            after_url=after_url,
            before_screenshot=before_screenshot,
            after_screenshot=after_screenshot,
            before_fingerprint=before_fp,
            after_fingerprint=after_fp,
            page_state_changed=bool(before_fp and after_fp and before_fp != after_fp)
            or (before_url != after_url),
            new_console_errors=new_console,
            new_network_errors=new_network,
            evidence_ids=evidence_ids,
            inspected=inspected,
            error=error,
            duration_ms=duration_ms,
        )

    def _resolve_fill_value(self, action: BrowserAction) -> str:
        meta = action.metadata or {}
        ref = meta.get("credential_ref")
        if ref and self.credential_vault is not None:
            resolved = self.credential_vault.resolve_ref(str(ref))
            if resolved is not None:
                return str(resolved)
        return action.value or ""

    def _target_from_action(
        self,
        action: BrowserAction,
        page_state: PageState | None,
    ) -> TargetRef:
        meta = action.metadata or {}
        el: InteractiveElement | None = None
        if action.element_id and page_state:
            for candidate in page_state.interactive_elements:
                if candidate.element_id == action.element_id:
                    el = candidate
                    break
        if el is None and action.element_id:
            el = self.registry.get_element(action.element_id)

        name = (
            meta.get("action_label")
            or meta.get("name")
            or meta.get("accessible_name")
            or (el.accessible_name if el else None)
            or (el.label if el else None)
            or (el.visible_text if el else None)
            or (el.text if el else None)
        )
        selector = (
            meta.get("selector")
            or (el.selector_hint if el else None)
            or (
                resolve_selector(page_state, action.element_id)
                if page_state and action.element_id
                else None
            )
        )
        return TargetRef(
            target_id=action.element_id,
            selector_hint=str(selector) if selector else None,
            role=str(meta.get("role") or (el.role if el else "") or "") or None,
            name=str(name) if name else None,
            tag=el.tag if el else None,
            href=el.href if el else None,
            input_type=(el.input_type or el.type) if el else None,
            placeholder=el.placeholder if el else None,
            test_id=str(meta["test_id"]) if meta.get("test_id") else None,
        )

    # ------------------------------------------------------------------
    # Legacy Playwright Page path (tests / optimisation)
    # ------------------------------------------------------------------

    async def _execute_via_page(
        self,
        page: Page,
        action: BrowserAction,
        *,
        page_state: PageState | None,
        before_screenshot: str | None,
        after_screenshot: str | None,
        capture_evidence: bool,
    ) -> ActionResult:
        action_id = new_id()
        before_url = page.url
        started = time.perf_counter()
        success = False
        message = ""
        error: str | None = None
        inspected: dict[str, Any] = {}
        evidence_ids: list[str] = []
        before_fp = page_state.state_fingerprint if page_state else None
        after_fp = before_fp

        console_mark = self.console.mark() if self.console else 0
        network_mark = self.network.mark() if self.network else 0

        if capture_evidence and self.evidence and not before_screenshot:
            try:
                shot = await self.evidence.before_action(page, action.action.value, action_id)
                before_screenshot = shot.path
                evidence_ids.append(shot.evidence_id)
            except Exception as exc:
                logger.warning("Before screenshot failed: %s", exc)

        try:
            if action.action == ActionType.FINISH:
                success = True
                message = "Finish signal accepted"
            elif action.action == ActionType.OPEN_URL:
                url = action.url or action.value
                if not url:
                    raise ValueError("open_url missing url")
                await page.goto(url, wait_until="domcontentloaded")
                await self._wait_settled(page)
                success = True
                message = f"Opened {url}"
            elif action.action == ActionType.GO_BACK:
                await page.go_back(wait_until="domcontentloaded")
                await self._wait_settled(page)
                success = True
                message = "Navigated back"
            elif action.action == ActionType.REFRESH:
                await page.reload(wait_until="domcontentloaded")
                await self._wait_settled(page)
                success = True
                message = "Page refreshed"
            elif action.action == ActionType.WAIT:
                await page.wait_for_timeout(action.wait_ms or 1000)
                success = True
                message = f"Waited {action.wait_ms or 1000}ms"
            elif action.action == ActionType.TAKE_SCREENSHOT:
                if self.evidence:
                    shot = await self.evidence.full_page_screenshot(page, "manual", action_id)
                    evidence_ids.append(shot.evidence_id)
                    after_screenshot = shot.path
                success = True
                message = "Screenshot captured"
            elif action.action == ActionType.INSPECT_FORM:
                inspected = self._inspect_form(page_state, action.element_id)
                success = True
                message = f"Inspected form {action.element_id or ''}".strip()
            elif action.action == ActionType.INSPECT_TABLE:
                inspected = self._inspect_table(page_state, action.element_id)
                success = True
                message = f"Inspected table {action.element_id or ''}".strip()
            elif action.action in ELEMENT_ACTIONS:
                locator, element = await self._resolve_element(page, action, page_state)
                await self._validate_element(locator, element, action)
                await locator.scroll_into_view_if_needed()
                # Observational only: ring + label, then the same click/fill as before.
                await show_live_indicator(
                    locator, action, page_state, highlight_ms=self._highlight_ms()
                )
                fill_outcome = await self._perform_element_action(locator, action)
                if fill_outcome is not None and fill_outcome.kind != "fill":
                    # A lookup satisfied with a real record, or a custom chooser
                    # operated by click. Either way the value in the field is not
                    # the value that was asked for, and a report that quietly
                    # showed the requested one would be wrong.
                    inspected["fill_outcome"] = fill_outcome.to_dict()
                # Auth submits (login/registration) often trigger an async sign-in call
                # before any client-side redirect — a generic UI-click settle window is
                # frequently too short to observe the result truthfully.
                #
                # Whether this action is a submission is decided by
                # `_submit_write_mark`, shared with the adapter path above, so the
                # two cannot answer that question differently.
                is_auth_submit = bool((action.metadata or {}).get("auth_submit"))
                await self._wait_settled(
                    page,
                    extra_ms=1600 if is_auth_submit else 0,
                    await_write=self._submit_write_mark(action) is not None,
                )
                success = True
                message = f"{action.action.value} on {action.element_id}"
            else:
                raise ValueError(f"Unsupported action: {action.action}")
        except Exception as exc:
            error = str(exc)
            message = f"Action failed: {exc}"
            logger.warning("Action %s failed: %s", action.action, exc)

        await self._wait_for_highlight()

        if (
            capture_evidence
            and self.evidence
            and not after_screenshot
            and action.action != ActionType.TAKE_SCREENSHOT
        ):
            try:
                shot = await self.evidence.after_action(page, action.action.value, action_id)
                after_screenshot = shot.path
                evidence_ids.append(shot.evidence_id)
            except Exception as exc:
                logger.warning("After screenshot failed: %s", exc)

        try:
            if self.observer:
                after_state = await self.observer.observe(page, screenshot_path=after_screenshot)
                after_fp = after_state.state_fingerprint
            elif page_state is not None:
                after_fp = fingerprint_page_state(
                    PageState(
                        page_id=page_state.page_id,
                        url=page.url,
                        title=await page.title(),
                        headings=page_state.headings,
                        interactive_elements=page_state.interactive_elements,
                        modals=page_state.modals,
                        dialogs=page_state.dialogs,
                        visible_text_summary=page_state.visible_text_summary,
                    )
                )
        except Exception as exc:
            logger.debug("Post-action fingerprint failed: %s", exc)

        new_console = self.console.errors_since(console_mark) if self.console else []
        new_network = self.network.errors_since(network_mark) if self.network else []

        if self.evidence:
            try:
                meta = self.evidence.record_metadata(
                    url=page.url,
                    title=await page.title(),
                    console_errors=new_console,
                    network_errors=new_network,
                    action={
                        "action_id": action_id,
                        "action": action.action.value,
                        "element_id": action.element_id,
                        "success": success,
                        "error": error,
                    },
                )
                evidence_ids.append(meta.evidence_id)
            except Exception:
                pass

        duration_ms = int((time.perf_counter() - started) * 1000)
        return ActionResult(
            action_id=action_id,
            run_id=self.run_id,
            action=action,
            success=success,
            message=message,
            before_url=before_url,
            after_url=page.url,
            before_screenshot=before_screenshot,
            after_screenshot=after_screenshot,
            before_fingerprint=before_fp,
            after_fingerprint=after_fp,
            page_state_changed=bool(before_fp and after_fp and before_fp != after_fp)
            or (before_url != page.url),
            new_console_errors=new_console,
            new_network_errors=new_network,
            evidence_ids=evidence_ids,
            inspected=inspected,
            error=error,
            duration_ms=duration_ms,
        )

    async def _resolve_element(
        self,
        page: Page,
        action: BrowserAction,
        page_state: PageState | None,
    ) -> tuple[Locator, InteractiveElement | None]:
        meta = action.metadata or {}
        element_id = action.element_id

        if meta.get("test_id") or meta.get("selector") or meta.get("role"):
            locator = await self._resolve_semantic(page, action)
            return locator, None

        if not element_id:
            raise ValueError(f"{action.action.value} requires element_id")

        element = self.registry.get_element(element_id)
        if element is None and page_state:
            for el in page_state.interactive_elements:
                if el.element_id == element_id:
                    element = el
                    self.registry.register(el)
                    break

        if self.registry.get(element_id):
            locator = await self.registry.resolve_locator(page, element_id)
        else:
            selector = resolve_selector(page_state, element_id) if page_state else None
            if not selector:
                raise ValueError(f"Unknown element_id: {element_id}")
            locator = page.locator(selector).first

        try:
            count = await locator.count()
        except TypeError:
            count = 0
        if count == 0:
            if element is not None:
                locator = await self._semantic_fallback(page, action, element)
                try:
                    if await locator.count() > 0:
                        return locator, element
                except TypeError:
                    pass
            locator = page.locator(f'[data-gemmaqa-id="{element_id}"]').first
            try:
                if await locator.count() == 0:
                    raise ValueError(f"Unknown element_id: {element_id}")
            except TypeError as exc:
                raise ValueError(f"Unknown element_id: {element_id}") from exc

        return locator, element

    async def _resolve_semantic(self, page: Page, action: BrowserAction) -> Locator:
        meta = action.metadata or {}
        candidates: list[Locator] = []
        test_id = meta.get("test_id")
        if test_id:
            candidates.append(page.get_by_test_id(str(test_id)))
            candidates.append(page.locator(f'[data-testid="{test_id}"]'))
        if meta.get("selector"):
            candidates.append(page.locator(str(meta["selector"])))
        role = meta.get("role")
        name = meta.get("name") or meta.get("accessible_name")
        if role and name:
            candidates.append(page.get_by_role(str(role), name=str(name)))
        if name:
            candidates.append(page.get_by_label(str(name)))
            candidates.append(page.get_by_placeholder(str(name)))
            candidates.append(page.get_by_text(str(name), exact=False))

        for loc in candidates:
            first = loc.first
            try:
                if await first.count() > 0:
                    return first
            except Exception:
                continue
        raise ValueError(
            f"Could not resolve semantic locator for action {action.action.value} "
            f"(test_id={test_id!r})"
        )

    async def _semantic_fallback(
        self,
        page: Page,
        action: BrowserAction,
        element: InteractiveElement | None,
    ) -> Locator:
        if element is None:
            return page.locator("[data-gemmaqa-missing='1']").first
        name = element.accessible_name or element.label or element.text or element.aria_label
        if element.role and name:
            loc = page.get_by_role(element.role, name=name).first
            if await loc.count() > 0:
                return loc
        if name:
            for builder in (
                lambda: page.get_by_label(name).first,
                lambda: page.get_by_placeholder(name).first,
                lambda: page.get_by_text(name, exact=False).first,
            ):
                try:
                    loc = builder()
                    if await loc.count() > 0:
                        return loc
                except Exception:
                    continue
        return page.locator("html")

    async def _validate_element(
        self,
        locator: Locator,
        element: InteractiveElement | None,
        action: BrowserAction,
    ) -> None:
        try:
            visible = await locator.is_visible()
        except Exception:
            visible = element.is_visible if element else False
        if not visible:
            raise ValueError(f"Element {action.element_id} is not visible")

        if action.action in {
            ActionType.CLICK,
            ActionType.FILL,
            ActionType.SELECT,
            ActionType.CHECK,
            ActionType.UNCHECK,
            ActionType.PRESS,
            ActionType.OPEN_TAB,
        }:
            try:
                enabled = await locator.is_enabled()
            except Exception:
                enabled = element.is_enabled if element else True
            if not enabled or (element and element.disabled):
                raise ValueError(f"Element {action.element_id} is disabled")

    async def _perform_element_action(
        self, locator: Locator, action: BrowserAction
    ) -> FillOutcome | None:
        """Returns what the fill actually did, when that differs from what was
        asked for (a chooser clicked, a lookup satisfied with a real record)."""
        if action.action == ActionType.CLICK:
            await _click_accepting_cleanup_dialog(
                action, getattr(locator, "page", None), lambda: click_element(locator)
            )
        elif action.action == ActionType.FILL:
            return await fill_or_choose(locator, self._resolve_fill_value(action))
        elif action.action == ActionType.SELECT:
            await locator.select_option(self._resolve_fill_value(action))
        elif action.action == ActionType.CHECK:
            await check_or_force(locator, True)
        elif action.action == ActionType.UNCHECK:
            await check_or_force(locator, False)
        elif action.action == ActionType.PRESS:
            await locator.press(action.key or action.value or "Enter")
        elif action.action == ActionType.HOVER:
            await locator.hover()
        elif action.action == ActionType.OPEN_TAB:
            await _click_accepting_cleanup_dialog(
                action, getattr(locator, "page", None), lambda: click_element(locator)
            )
        else:
            raise ValueError(f"Unsupported element action: {action.action}")

    def _submit_write_mark(self, action: BrowserAction) -> int | None:
        """Take a reading of the write counter if this action is a submission.

        Taken BEFORE the settle, never after: a fast application can answer the
        write while the settle is still running, and a mark taken afterwards would
        miss it and conclude no write ever happened.

        Auth submits and data submits are both submissions. The auth path already
        leant on a longer fixed settle for this; the data path had the short one.
        """
        meta = action.metadata or {}
        is_submit = bool(meta.get("auth_submit")) or bool(meta.get("form_workflow_submit"))
        if not is_submit or self.network is None:
            return None
        return self.network.mutation_responses

    async def _wait_settled(
        self, page: Page, *, extra_ms: int = 0, await_write: bool = False
    ) -> None:
        mark = self.network.mutation_responses if (await_write and self.network) else None
        await page.wait_for_timeout(self.settle_ms + extra_ms)
        try:
            wait_kind = "networkidle" if extra_ms else "domcontentloaded"
            await page.wait_for_load_state(wait_kind, timeout=5000 if extra_ms else 3000)
        except Exception:
            pass
        if mark is not None:
            await self._await_submitted_write(page.wait_for_timeout, mark)

    async def _await_submitted_write(self, sleep_ms: Any, mark: int) -> None:
        """Hold the submit open until the application has ANSWERED the write.

        Recording successful mutating requests was necessary but not sufficient:
        measured on OrangeHRM's Buzz save, with the click, the settle above, and
        then the observation, `POST 200 /api/v2/buzz/posts` arrived **365ms after
        `after_state` was built**. The entry the submission classifier reads did
        not exist yet, so it saw an unchanged page, returned `unknown`, and a
        create that plainly succeeded was recorded `submitted_unverified` — no
        record registered, nothing to open, update or delete.

        A fixed longer sleep would trade correctness for wall-clock on every
        submit and still be a guess. This waits on the event itself, bounded at
        both ends: briefly for a write to appear, then for it to complete.

        `domcontentloaded` cannot substitute for this. On a single-page
        application the submit causes no navigation, so it returns immediately
        and reports a page that is done rendering but has not been answered.
        """
        monitor = self.network
        if monitor is None:
            return

        waited = 0
        started = False
        while waited < SUBMIT_WRITE_START_TIMEOUT_MS:
            if monitor.mutation_responses > mark or monitor.mutations_in_flight():
                started = True
                break
            await sleep_ms(_SUBMIT_WRITE_POLL_MS)
            waited += _SUBMIT_WRITE_POLL_MS
        if not started:
            logger.debug(
                "Submit started no write within %dms; nothing to wait for",
                SUBMIT_WRITE_START_TIMEOUT_MS,
            )
            return

        waited = 0
        while monitor.mutations_in_flight() and waited < SUBMIT_WRITE_COMPLETE_TIMEOUT_MS:
            await sleep_ms(_SUBMIT_WRITE_POLL_MS)
            waited += _SUBMIT_WRITE_POLL_MS
        if monitor.mutations_in_flight():
            # Reported, not silently absorbed: an unanswered write means the
            # classification that follows is made on incomplete evidence.
            logger.info(
                "Submit write still unanswered after %dms; classifying on what is known",
                SUBMIT_WRITE_COMPLETE_TIMEOUT_MS,
            )
        else:
            logger.debug("Submit write answered after %dms", waited)

    def _inspect_form(
        self, page_state: PageState | None, element_id: str | None
    ) -> dict[str, Any]:
        if not page_state:
            return {"error": "no page_state"}
        for form in page_state.forms:
            if element_id is None or form.form_id == element_id:
                return form.model_dump()
        if page_state.forms:
            return page_state.forms[0].model_dump()
        return {"error": "form not found", "element_id": element_id}

    def _inspect_table(
        self, page_state: PageState | None, element_id: str | None
    ) -> dict[str, Any]:
        if not page_state:
            return {"error": "no page_state"}
        for table in page_state.tables:
            if element_id is None or table.table_id == element_id:
                return table.model_dump()
        if page_state.tables:
            return page_state.tables[0].model_dump()
        return {"error": "table not found", "element_id": element_id}
