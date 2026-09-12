from app.browser.adapters import create_browser_adapter
from app.browser.adapters.base import BrowserAdapter
from app.browser.adapters.direct import DirectPlaywrightAdapter
from app.browser.adapters.mcp import FakeMcpTransport, PlaywrightMCPAdapter
from app.browser.console_monitor import ConsoleMonitor
from app.browser.evidence import EvidenceCollector
from app.browser.executor import ActionExecutor
from app.browser.fingerprint import compute_fingerprint, fingerprint_page_state, normalize_url
from app.browser.locators import LocatorRegistry, choose_locator_strategy, resolve_selector
from app.browser.manager import BrowserManager
from app.browser.network_monitor import NetworkMonitor, sanitize_network_url
from app.browser.observer import PageObserver, normalize_element

__all__ = [
    "BrowserAdapter",
    "DirectPlaywrightAdapter",
    "PlaywrightMCPAdapter",
    "FakeMcpTransport",
    "create_browser_adapter",
    "BrowserManager",
    "PageObserver",
    "ActionExecutor",
    "EvidenceCollector",
    "ConsoleMonitor",
    "NetworkMonitor",
    "LocatorRegistry",
    "choose_locator_strategy",
    "resolve_selector",
    "normalize_element",
    "compute_fingerprint",
    "fingerprint_page_state",
    "normalize_url",
    "sanitize_network_url",
]
