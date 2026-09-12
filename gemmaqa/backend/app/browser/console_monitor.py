"""Browser console and page-error monitor."""

from __future__ import annotations

from datetime import datetime

from playwright.async_api import ConsoleMessage, Page

from app.schemas import ConsoleEntry
from app.utils.logging import get_logger

logger = get_logger("browser.console")


class ConsoleMonitor:
    """Collect console errors/warnings and uncaught page exceptions."""

    def __init__(self, max_entries: int = 200) -> None:
        self.max_entries = max_entries
        self.entries: list[ConsoleEntry] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self._attached_pages: set[int] = set()

    def attach(self, page: Page) -> None:
        page_id = id(page)
        if page_id in self._attached_pages:
            return

        def on_console(msg: ConsoleMessage) -> None:
            try:
                level = msg.type or "log"
                text = (msg.text or "")[:500]
                entry = ConsoleEntry(
                    level=level,
                    message=text,
                    timestamp=datetime.utcnow(),
                    url=page.url,
                    source="console",
                )
                self._store(entry)
            except Exception as exc:
                logger.debug("console handler error: %s", exc)

        def on_page_error(exc: Exception) -> None:
            try:
                text = str(exc)[:500]
                entry = ConsoleEntry(
                    level="error",
                    message=text,
                    timestamp=datetime.utcnow(),
                    url=page.url,
                    source="pageerror",
                )
                self._store(entry)
            except Exception as err:
                logger.debug("pageerror handler error: %s", err)

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        self._attached_pages.add(page_id)

    def _store(self, entry: ConsoleEntry) -> None:
        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries :]
        if entry.level == "error":
            self.errors.append(entry.message)
            if len(self.errors) > self.max_entries:
                self.errors = self.errors[-self.max_entries :]
            logger.debug("Console error: %s", entry.message[:200])
        elif entry.level == "warning":
            self.warnings.append(entry.message)
            if len(self.warnings) > self.max_entries:
                self.warnings = self.warnings[-self.max_entries :]

    def snapshot(self) -> list[str]:
        return list(self.errors[-20:])

    def snapshot_entries(self) -> list[ConsoleEntry]:
        return list(self.entries[-20:])

    def errors_since(self, index: int) -> list[str]:
        return list(self.errors[index:])

    def mark(self) -> int:
        return len(self.errors)

    def clear(self) -> None:
        self.entries.clear()
        self.errors.clear()
        self.warnings.clear()
