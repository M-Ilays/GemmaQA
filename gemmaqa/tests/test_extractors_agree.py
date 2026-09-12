"""The two page extractors must describe the same page the same way.

`browser/observer.py` builds `PageState`; `perception/dom_extractor.py` builds the
Canonical Page Model. Both parse forms, independently. Three bugs this session came
from that duplication — each time a fix was applied to the copy that was NOT on the
path being measured, tests passed, and live behaviour did not move:

  * record identity — two matchers (frontier vs cleanup planner), different answers
  * form fields     — custom controls added to one extractor, not the other
  * field semantics — `field_kind` set by one extractor, never read on the other path

These tests assert the extractors stay in step. They are cheap; the bugs they guard
against cost hours each.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

OBSERVER = (BACKEND / "app/browser/observer.py").read_text(encoding="utf-8")
DOM_EXTRACTOR = (BACKEND / "app/perception/dom_extractor.py").read_text(encoding="utf-8")


def _form_field_selector(source: str) -> str:
    match = re.search(r"form\.querySelectorAll\(([^)]*)\)", source)
    assert match, "no form field selector found"
    return match.group(1)


def test_both_extractors_collect_custom_controls():
    """A control missing from EITHER is a control something downstream is blind to."""
    for name, source in (("observer", OBSERVER), ("dom_extractor", DOM_EXTRACTOR)):
        selector = _form_field_selector(source)
        assert "CUSTOM_CONTROL_SELECTOR" in selector, f"{name} collects native controls only"


def test_both_extractors_look_for_the_same_custom_controls():
    """Diverging selectors would make a field visible to one consumer and not the
    other — the exact shape of the bug this file exists to prevent."""
    def signals(source: str) -> set[str]:
        block = source[source.index("CUSTOM_CONTROL_SELECTOR = ["):]
        block = block[: block.index("].join")]
        return set(re.findall(r"'([^']+)'", block))

    assert signals(OBSERVER) == signals(DOM_EXTRACTOR)


def test_both_extractors_emit_the_field_semantics_signals():
    """`_infer_field_kind` reads these. Missing from the observer, `field_kind` was
    always None on the path the form workflow uses, so a lookup could not be
    recognised and the value generator invented a value for it."""
    for signal in ("role", "multiple", "list_attr", "aria_autocomplete"):
        assert f"{signal}:" in OBSERVER, f"observer drops {signal}"
        assert f"{signal}:" in DOM_EXTRACTOR, f"dom_extractor drops {signal}"


def test_record_identity_has_a_single_matcher():
    """The frontier and the cleanup planner answer "is this my record?" through the
    same function; two implementations gave two different answers."""
    frontier = (BACKEND / "app/agent/frontier.py").read_text(encoding="utf-8")
    cleanup = (BACKEND / "app/agent/cleanup_planner.py").read_text(encoding="utf-8")

    for name, source in (("frontier", frontier), ("cleanup_planner", cleanup)):
        assert "identity_matches_cells" in source, f"{name} does not use the shared matcher"


def test_filling_a_control_has_a_single_entry_point():
    """The executor's native path and the direct adapter both fill; a div-based
    chooser must be handled identically by both."""
    executor = (BACKEND / "app/browser/executor.py").read_text(encoding="utf-8")
    direct = (BACKEND / "app/browser/adapters/direct.py").read_text(encoding="utf-8")

    assert "fill_or_choose" in executor
    assert "fill_or_choose" in direct
