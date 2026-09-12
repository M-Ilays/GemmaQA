"""A headed run must not screencast the page the operator is watching.

`context.tracing.start(screenshots=True)` does not take one screenshot per
action — it starts a CDP screencast that runs for the entire session. Counted in
the trace of the operator's own run 5b4268e0:

    1987 JPEG frames / 521 s  =  3.8 captures per second, 62.6 MB of a 120 MB trace

Every run the operator watches is `headless=False`, and that continuous surface
capture is what they saw as the page blinking non-stop — reported since the first
version of the tool.

Three things were ruled out by measurement first, so this is not a guess among
candidates:

  * the page itself feels nothing — 5 viewport screenshots, 1 full-page
    screenshot and 3 observe passes produced 0 viewport, DPR, scroll or
    visibility changes
  * discrete captures cost nothing — 10 `page.screenshot()` calls in a headed
    browser dropped 0 frames, median frame gap 16.7 ms throughout, and
    `fromSurface: false` made no difference because there was nothing to fix
  * the run loop never navigates or reloads

The screencast is what remains, and it is the only thing in the system that runs
continuously rather than per action.

Verified after the change by driving the real `BrowserManager` both ways and
counting the frames each trace contains: headed 0, headless 47 over 6 s (7.8/s).
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import inspect  # noqa: E402

from app.browser.manager import BrowserManager  # noqa: E402

SOURCE = inspect.getsource(BrowserManager.start)

# The comment above the fixed line quotes the old `screenshots=True` to explain
# what it did, so a naive substring check on the whole source would match the
# prose and pass no matter what the code says. Compare against code only.
CODE = "\n".join(
    line for line in SOURCE.splitlines() if not line.lstrip().startswith("#")
)


def test_the_screencast_is_tied_to_headlessness_not_hardcoded_on():
    """The regression: `screenshots=True` was a literal, so every headed run
    screencast itself."""
    assert "screenshots=True" not in CODE
    assert "trace_screenshots = self.headless" in CODE
    assert "screenshots=trace_screenshots" in CODE


def test_dom_snapshots_survive():
    """The screencast is the flicker; the DOM snapshots are the part of a trace
    that is actually used to reconstruct what happened, and they cost nothing
    visually. Losing them to fix a flicker would be a bad trade."""
    assert "snapshots=True" in SOURCE


def test_a_headed_manager_reports_headless_false():
    """`trace_screenshots` is derived from `self.headless`, so that attribute has
    to mean what it says for both explicit values."""
    assert BrowserManager("r", headless=False).headless is False
    assert BrowserManager("r", headless=True).headless is True


def test_tracing_can_still_be_turned_off_entirely():
    assert BrowserManager("r", enable_tracing=False).enable_tracing is False
    assert BrowserManager("r", enable_tracing=True).enable_tracing is True


def test_the_operator_is_told_which_mode_the_trace_is_in():
    """A trace with no frames must not look like a broken trace. The run log says
    the screencast was skipped and why."""
    assert "Tracing started" in SOURCE
    assert "screencast" in SOURCE


def test_the_reason_is_recorded_where_the_decision_is_made():
    """The measurement that justifies this belongs next to the line it explains —
    the next person to see an empty trace will look here first."""
    assert "5b4268e0" in SOURCE, "the run the frames were counted in"
    assert "1987" in SOURCE, "how many frames, so the scale is not re-litigated"
