"""Evidence capture organized per QA run."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from app.browser.live_indicator import clear_live_indicator
from app.config import get_settings
from app.schemas import EvidenceItem
from app.utils.ids import new_id
from app.utils.logging import get_logger

logger = get_logger("browser.evidence")


class EvidenceCollector:
    """
    Capture and catalog evidence artifacts for a run.

    Layout:
      evidence/<run_id>/screenshots/
      evidence/<run_id>/traces/
      evidence/<run_id>/reports/
    """

    def __init__(self, run_id: str, *, max_screenshots: int | None = None) -> None:
        settings = get_settings()
        self.run_id = run_id
        self.root = settings.run_evidence_dir(run_id)
        self.screenshots_dir = self.root / "screenshots"
        self.traces_dir = self.root / "traces"
        self.reports_dir = self.root / "reports"
        self.items: list[EvidenceItem] = []
        self.max_screenshots = max_screenshots if max_screenshots is not None else settings.max_screenshots
        self._bytes_used = 0

    def screenshot_count(self) -> int:
        return sum(1 for i in self.items if i.kind == "screenshot")

    def total_bytes(self) -> int:
        return self._bytes_used

    async def _mask_password_fields(self, page: Page) -> None:
        """Blur/mask visible password inputs before capturing screenshots."""
        try:
            await page.evaluate(
                """() => {
                  document.querySelectorAll('input[type="password"]').forEach((el) => {
                    try {
                      el.value = '********';
                      el.setAttribute('value', '********');
                      el.style.setProperty('-webkit-text-security', 'disc');
                    } catch (e) {}
                  });
                }"""
            )
        except Exception:
            logger.debug("Password field mask skipped", exc_info=True)

    async def screenshot(
        self,
        page: Page,
        label: str,
        *,
        full_page: bool = False,
        page_id: str | None = None,
        action_id: str | None = None,
    ) -> EvidenceItem:
        """Capture a viewport (default) or full-page screenshot."""
        if self.screenshot_count() >= self.max_screenshots:
            # Return a lightweight placeholder record without writing another file
            evidence_id = new_id()
            path = self.screenshots_dir / f"skipped_{evidence_id[:8]}.txt"
            path.write_text("screenshot budget exhausted", encoding="utf-8")
            return self._record(
                kind="other",
                path=path,
                description=f"screenshot skipped (budget): {label}",
                page_id=page_id,
                action_id=action_id,
                evidence_id=evidence_id,
            )

        evidence_id = new_id()
        stamp = datetime.utcnow().strftime("%H%M%S")
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
        filename = f"{safe_label}_{stamp}_{evidence_id[:8]}.png"
        path = self.screenshots_dir / filename
        await clear_live_indicator(page)
        await self._mask_password_fields(page)
        await page.screenshot(path=str(path), full_page=full_page)
        try:
            self._bytes_used += path.stat().st_size
        except OSError:
            pass
        return self._record(
            kind="screenshot",
            path=path,
            description=f"{label} ({'full' if full_page else 'viewport'})",
            page_id=page_id,
            action_id=action_id,
            evidence_id=evidence_id,
        )

    async def before_action(
        self,
        page: Page,
        action_name: str,
        action_id: str | None = None,
    ) -> EvidenceItem:
        return await self.screenshot(
            page, f"before_{action_name}", full_page=False, action_id=action_id
        )

    async def after_action(
        self,
        page: Page,
        action_name: str,
        action_id: str | None = None,
    ) -> EvidenceItem:
        return await self.screenshot(
            page, f"after_{action_name}", full_page=False, action_id=action_id
        )

    async def full_page_screenshot(
        self,
        page: Page,
        label: str = "fullpage",
        action_id: str | None = None,
    ) -> EvidenceItem:
        return await self.screenshot(page, label, full_page=True, action_id=action_id)

    async def screenshot_via_adapter(
        self,
        adapter: Any,
        label: str,
        *,
        full_page: bool = False,
        action_id: str | None = None,
    ) -> EvidenceItem:
        """Adapter-independent screenshot (Direct or MCP)."""
        if self.screenshot_count() >= self.max_screenshots:
            evidence_id = new_id()
            path = self.screenshots_dir / f"skipped_{evidence_id[:8]}.txt"
            path.write_text("screenshot budget exhausted", encoding="utf-8")
            return self._record(
                kind="other",
                path=path,
                description=f"screenshot skipped (budget): {label}",
                action_id=action_id,
                evidence_id=evidence_id,
            )
        evidence_id = new_id()
        stamp = datetime.utcnow().strftime("%H%M%S")
        safe_label = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:60]
        filename = f"{safe_label}_{stamp}_{evidence_id[:8]}.png"
        path = self.screenshots_dir / filename
        try:
            await clear_live_indicator(getattr(adapter, "native_page", None))
            await adapter.take_screenshot(str(path), full_page=full_page)
        except Exception as exc:
            # Capability limitation — write a note instead of failing the run
            note = path.with_suffix(".txt")
            note.write_text(
                f"screenshot unavailable via adapter={getattr(adapter, 'name', '?')}: "
                f"{type(exc).__name__}",
                encoding="utf-8",
            )
            return self._record(
                kind="other",
                path=note,
                description=f"screenshot limitation: {label}",
                action_id=action_id,
                evidence_id=evidence_id,
            )
        try:
            self._bytes_used += path.stat().st_size
        except OSError:
            pass
        return self._record(
            kind="screenshot",
            path=path,
            description=f"{label} ({'full' if full_page else 'viewport'})",
            action_id=action_id,
            evidence_id=evidence_id,
        )

    async def before_action_adapter(
        self, adapter: Any, action_name: str, action_id: str | None = None
    ) -> EvidenceItem:
        return await self.screenshot_via_adapter(
            adapter, f"before_{action_name}", full_page=False, action_id=action_id
        )

    async def after_action_adapter(
        self, adapter: Any, action_name: str, action_id: str | None = None
    ) -> EvidenceItem:
        return await self.screenshot_via_adapter(
            adapter, f"after_{action_name}", full_page=False, action_id=action_id
        )

    async def full_page_screenshot_adapter(
        self, adapter: Any, label: str = "fullpage", action_id: str | None = None
    ) -> EvidenceItem:
        return await self.screenshot_via_adapter(
            adapter, label, full_page=True, action_id=action_id
        )

    def record_trace(self, path: Path | str, description: str = "Playwright trace") -> EvidenceItem:
        return self._record(kind="trace", path=Path(path), description=description)

    def record_metadata(
        self,
        *,
        url: str,
        title: str,
        console_errors: list[str] | None = None,
        network_errors: list[str] | None = None,
        action: dict[str, Any] | None = None,
        path: Path | None = None,
    ) -> EvidenceItem:
        """Persist a small JSON sidecar with safe action/page metadata."""
        import json

        evidence_id = new_id()
        out = path or (self.reports_dir / f"meta_{evidence_id[:8]}.json")
        payload = {
            "run_id": self.run_id,
            "url": url,
            "title": title,
            "console_errors": (console_errors or [])[:20],
            "network_errors": (network_errors or [])[:20],
            "action": action or {},
            "captured_at": datetime.utcnow().isoformat() + "Z",
        }
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return self._record(kind="other", path=out, description="action metadata")

    def add(
        self,
        kind: str,
        path: str,
        description: str = "",
        page_id: str | None = None,
        action_id: str | None = None,
    ) -> EvidenceItem:
        return self._record(
            kind=kind,
            path=Path(path),
            description=description,
            page_id=page_id,
            action_id=action_id,
        )

    def _record(
        self,
        *,
        kind: str,
        path: Path,
        description: str = "",
        page_id: str | None = None,
        action_id: str | None = None,
        evidence_id: str | None = None,
    ) -> EvidenceItem:
        item = EvidenceItem(
            evidence_id=evidence_id or new_id(),
            run_id=self.run_id,
            kind=kind,  # type: ignore[arg-type]
            path=str(path),
            description=description,
            page_id=page_id,
            action_id=action_id,
        )
        self.items.append(item)
        logger.info("Evidence saved (%s): %s", kind, path)
        return item
