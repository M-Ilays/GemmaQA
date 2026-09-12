"""
Open a public demo page, extract PageState, save a screenshot, and close.

Usage (from gemmaqa/backend with venv active):

    python scripts/observe_demo.py
    python scripts/observe_demo.py https://example.com
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow running as `python scripts/observe_demo.py` from backend/
BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.browser.console_monitor import ConsoleMonitor
from app.browser.evidence import EvidenceCollector
from app.browser.manager import BrowserManager
from app.browser.network_monitor import NetworkMonitor
from app.browser.observer import PageObserver
from app.utils.ids import new_id
from app.utils.logging import setup_logging


async def main(url: str = "https://example.com") -> None:
    setup_logging()
    run_id = new_id()
    browser = BrowserManager(run_id=run_id, headless=True, enable_tracing=True)
    evidence = EvidenceCollector(run_id)
    console = ConsoleMonitor()
    network = NetworkMonitor()
    observer = PageObserver(console, network)

    try:
        page = await browser.start()
        console.attach(page)
        network.attach(page)

        await page.goto(url, wait_until="domcontentloaded")
        shot = await evidence.screenshot(page, "demo_observe", full_page=False)
        state = await observer.observe(page, screenshot_path=shot.path)

        summary = {
            "run_id": run_id,
            "url": state.url,
            "title": state.title,
            "headings": state.headings,
            "element_count": len(state.interactive_elements),
            "forms": len(state.forms),
            "tables": len(state.tables),
            "fingerprint": state.state_fingerprint,
            "screenshot": shot.path,
            "sample_elements": [
                {
                    "element_id": el.element_id,
                    "tag": el.tag,
                    "category": el.category,
                    "accessible_name": el.accessible_name,
                    "locator": el.locator_strategy.kind if el.locator_strategy else None,
                }
                for el in state.interactive_elements[:8]
            ],
        }
        out = evidence.reports_dir / "observe_demo_summary.json"
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
        print(f"\nSummary written to: {out}")
    finally:
        await browser.stop()
        if browser.trace_path and browser.trace_path.exists():
            evidence.record_trace(browser.trace_path)
            print(f"Trace: {browser.trace_path}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    asyncio.run(main(target))
