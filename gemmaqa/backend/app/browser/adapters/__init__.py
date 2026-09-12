"""Factory for browser adapters (direct Playwright vs Playwright MCP)."""

from __future__ import annotations

from app.browser.adapters.base import BrowserAdapter
from app.browser.adapters.direct import DirectPlaywrightAdapter
from app.browser.adapters.mcp import PlaywrightMCPAdapter
from app.browser.console_monitor import ConsoleMonitor
from app.browser.manager import BrowserManager
from app.browser.network_monitor import NetworkMonitor
from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger("browser.adapter.factory")


def create_browser_adapter(
    run_id: str,
    *,
    headless: bool | None = None,
    manager: BrowserManager | None = None,
    console: ConsoleMonitor | None = None,
    network: NetworkMonitor | None = None,
    adapter_name: str | None = None,
) -> BrowserAdapter:
    """
    Build the configured BrowserAdapter.

    Default remains DirectPlaywrightAdapter so existing runs keep working.
    """
    settings = get_settings()
    name = (adapter_name or settings.normalized_browser_adapter).lower().strip()

    if name == "playwright_mcp":
        logger.info("Creating PlaywrightMCPAdapter for run=%s", run_id)
        return PlaywrightMCPAdapter(run_id=run_id)

    if name != "direct_playwright":
        raise RuntimeError(
            f"Unknown BROWSER_ADAPTER={settings.browser_adapter!r}. "
            "Use 'direct_playwright' or 'playwright_mcp'."
        )

    logger.info("Creating DirectPlaywrightAdapter for run=%s", run_id)
    return DirectPlaywrightAdapter(
        run_id=run_id,
        headless=headless,
        manager=manager,
        console=console,
        network=network,
    )
