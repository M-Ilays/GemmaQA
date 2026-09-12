"""Browser adapter interface — QA executor depends on this, not a concrete engine."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.browser.adapters.types import AdapterCapabilities, TargetRef
from app.schemas import InteractiveElement, PageState


class BrowserAdapter(ABC):
    """
    Execution gateway between the custom QA executor and a browser engine.

    Gemma never calls this directly. Validated decisions flow:
    Planner → SafetyValidator → ActionExecutor → BrowserAdapter → browser.
    """

    name: str = "base"

    @abstractmethod
    async def start_session(self) -> None: ...

    @abstractmethod
    async def close_session(self) -> None: ...

    @abstractmethod
    async def navigate(self, url: str) -> str: ...

    @abstractmethod
    async def go_back(self) -> str: ...

    @abstractmethod
    async def reload(self) -> str: ...

    @abstractmethod
    async def click_target(self, target: TargetRef) -> None: ...

    @abstractmethod
    # Returns an `app.browser.custom_controls.FillOutcome` where the adapter can
    # tell what the fill actually did (a chooser clicked, a lookup satisfied
    # with an existing record), and None where it cannot.
    async def fill_target(self, target: TargetRef, value: str): ...

    @abstractmethod
    async def select_target(self, target: TargetRef, value: str) -> None: ...

    async def submit_target(self, target: TargetRef) -> None:
        """Submit a form control (default: click). Adapters may override."""
        await self.click_target(target)

    @abstractmethod
    async def check_target(self, target: TargetRef, checked: bool = True) -> None: ...

    @abstractmethod
    async def hover_target(self, target: TargetRef) -> None: ...

    @abstractmethod
    async def press_target(self, target: TargetRef, key: str) -> None: ...

    @abstractmethod
    async def take_screenshot(self, path: str, *, full_page: bool = False) -> str: ...

    @abstractmethod
    async def get_current_url(self) -> str: ...

    @abstractmethod
    async def get_page_title(self) -> str: ...

    @abstractmethod
    async def get_accessibility_snapshot(self) -> dict[str, Any] | str: ...

    @abstractmethod
    async def get_observation_payload(self) -> dict[str, Any]:
        """Raw observation dict for PageObserver.build_page_state."""

    @abstractmethod
    async def get_console_events(self) -> list[str]: ...

    @abstractmethod
    async def get_network_events(self) -> list[str]: ...

    @abstractmethod
    async def wait_for_page_stable(self, timeout_ms: int = 3000) -> None: ...

    @abstractmethod
    async def wait(self, ms: int) -> None: ...

    @abstractmethod
    async def element_exists(self, target: TargetRef) -> bool: ...

    @abstractmethod
    async def resolve_target(self, target: TargetRef) -> TargetRef:
        """Resolve/validate a target; raise if unknown. May enrich with engine refs."""

    @abstractmethod
    async def list_tabs(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def switch_tab(self, tab_id: str) -> None: ...

    @abstractmethod
    async def health_check(self) -> dict[str, Any]: ...

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities()

    # ------------------------------------------------------------------
    # Backward-compatible thin wrappers (selector strings)
    # ------------------------------------------------------------------

    async def click(self, selector: str, *, element_id: str | None = None) -> None:
        await self.click_target(
            TargetRef(target_id=element_id, selector_hint=selector, name=selector)
        )

    async def fill(
        self,
        selector: str,
        value: str,
        *,
        element_id: str | None = None,
    ) -> None:
        await self.fill_target(
            TargetRef(target_id=element_id, selector_hint=selector, name=selector),
            value,
        )

    async def select_option(
        self,
        selector: str,
        value: str,
        *,
        element_id: str | None = None,
    ) -> None:
        await self.select_target(
            TargetRef(target_id=element_id, selector_hint=selector, name=selector),
            value,
        )

    @property
    def native_page(self) -> Any | None:
        return None

    @property
    def supports_native_page(self) -> bool:
        return self.native_page is not None

    async def observe_raw(self) -> PageState | None:
        return None

    def target_from_element(self, el: InteractiveElement) -> TargetRef:
        return TargetRef(
            target_id=el.element_id,
            selector_hint=el.selector_hint,
            role=el.role,
            name=el.accessible_name or el.label or el.visible_text or el.text,
            tag=el.tag,
            href=el.href,
            input_type=el.input_type or el.type,
            placeholder=el.placeholder,
        )
