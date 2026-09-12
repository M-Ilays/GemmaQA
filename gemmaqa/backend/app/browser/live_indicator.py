"""In-page highlight for the live headed browser — observational only.

The overlay is a sibling of <html>, pointer-events:none, and is stripped before
every evidence screenshot. It never waits: a page-side timer removes it. QA
timing, selectors, and form values are untouched.
"""

from __future__ import annotations

from typing import Any

from app.reporting.live_action import live_action_fields
from app.utils.logging import get_logger

logger = get_logger("browser.live_indicator")

INDICATOR_ID = "gemmaqa-live-indicator"
HIGHLIGHT_MS = 1600

# Injected as one evaluate() so we do not add Playwright round-trips per style.
_SHOW_SCRIPT = """
(el, payload) => {
  try {
    const existing = document.getElementById(payload.id);
    if (existing) existing.remove();
    const rect = el.getBoundingClientRect();
    const root = document.createElement("div");
    root.id = payload.id;
    root.setAttribute("data-gemmaqa-live-indicator", "1");
    root.setAttribute("aria-hidden", "true");
    root.style.cssText = [
      "position:fixed", "inset:0", "pointer-events:none",
      "z-index:2147483647", "margin:0", "padding:0", "border:0",
    ].join(";");
    const ring = document.createElement("div");
    ring.style.cssText = [
      "position:fixed",
      "left:" + Math.round(rect.left - 3) + "px",
      "top:" + Math.round(rect.top - 3) + "px",
      "width:" + Math.round(rect.width + 6) + "px",
      "height:" + Math.round(rect.height + 6) + "px",
      "border:2px solid #22d3ee",
      "border-radius:6px",
      "box-shadow:0 0 0 2px rgba(34,211,238,0.35)",
      "pointer-events:none",
      "box-sizing:border-box",
    ].join(";");
    const tag = document.createElement("div");
    tag.textContent = payload.label;
    const tagTop = Math.max(8, Math.round(rect.top) - 30);
    tag.style.cssText = [
      "position:fixed",
      "left:" + Math.round(rect.left) + "px",
      "top:" + tagTop + "px",
      "max-width:280px",
      "padding:3px 8px",
      "background:#0f172a",
      "color:#e2e8f0",
      "font:12px/1.3 ui-sans-serif,system-ui,sans-serif",
      "border-radius:4px",
      "pointer-events:none",
      "white-space:nowrap",
      "overflow:hidden",
      "text-overflow:ellipsis",
    ].join(";");
    root.appendChild(ring);
    root.appendChild(tag);
    document.documentElement.appendChild(root);
    window.setTimeout(() => { try { root.remove(); } catch (e) {} }, payload.ms);
  } catch (e) {}
}
"""

_CLEAR_SCRIPT = """
() => {
  const el = document.getElementById("gemmaqa-live-indicator");
  if (el) el.remove();
}
"""


async def show_live_indicator(
    target: Any,
    action: Any,
    page_state: Any | None = None,
    *,
    highlight_ms: int | None = None,
) -> None:
    """Paint a ring + label on `target` (a Locator). Never raises, never waits."""
    if target is None:
        return
    try:
        fields = live_action_fields(action, page_state)
        label = fields.get("live_action_line") or "ACTION"
        ms = HIGHLIGHT_MS if highlight_ms is None else int(highlight_ms)
        await target.evaluate(
            _SHOW_SCRIPT,
            {"id": INDICATOR_ID, "label": label, "ms": ms},
        )
    except Exception as exc:
        logger.debug("Live indicator skipped (%s)", type(exc).__name__)


async def show_live_indicator_for_adapter(
    adapter: Any,
    target: Any,
    action: Any,
    page_state: Any | None = None,
    *,
    highlight_ms: int | None = None,
) -> None:
    """Highlight via the adapter's locator when a native page exists. MCP skips."""
    if adapter is None or target is None:
        return
    if getattr(adapter, "native_page", None) is None:
        return
    locator_fn = getattr(adapter, "_locator_for", None)
    if locator_fn is None:
        return
    try:
        locator = await locator_fn(target)
        await show_live_indicator(locator, action, page_state, highlight_ms=highlight_ms)
    except Exception as exc:
        logger.debug("Live indicator (adapter) skipped (%s)", type(exc).__name__)


async def clear_live_indicator(page: Any | None) -> None:
    """Remove the overlay so evidence screenshots stay clean. Never raises."""
    if page is None:
        return
    try:
        await page.evaluate(_CLEAR_SCRIPT)
    except Exception:
        pass
