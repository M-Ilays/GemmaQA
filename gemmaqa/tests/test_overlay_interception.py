"""A click that cannot land must say what is in the way — and must not be forced.

Measured in run a1f300a5, action 40 (OrangeHRM Buzz create):

    locator resolved to <button type="submit" data-gemmaqa-id="el_020">
    element is visible, enabled and stable
    <img class="orangehrm-photo-viewer-photo"/> from
    <div class="orangehrm-photo-carousel --web"> subtree intercepts pointer events
    retrying ... Timeout 15000ms exceeded

An image lightbox opened fifteen actions earlier was still on screen. The submit
was abandoned, 15s of a 40-action budget bought one sentence, and no record was
created.

FORCING WOULD HAVE BEEN THE WRONG FIX, and the distinction is the whole point:

  * a DECORATION is the control's own styling painted over it. `force` is right —
    the hit-target test is the only check that cannot pass, and the click does
    reach the control.
  * an OVERLAY is unrelated content above the control. `force` would click
    something no person could reach, and on a confirmation dialog the thing
    underneath might be "Yes, Delete!".

Three candidate discriminators were measured on the real pages, and the obvious
one is wrong:

  DOM containment              — DEAD. On OrangeHRM's styled checkbox the
                                 intercepting `<i>` is a SIBLING's child, so
                                 `el.contains(top) || top.contains(el)` is False
                                 for the decorative case too. A containment test
                                 would force clicks through modals.
  shared-ancestor size         — decoration: `label`, 432px² of a 1,296,000px²
                                 viewport (0.03%). Overlay: `body` (100%).
  a positioned stacking layer  — decoration: none at all. Overlay: necessarily
                                 one, because that is the only way CSS can paint
                                 content over unrelated content.

Live, after the fix: decoration forced and ticked; roleless Escape-proof overlay
refused in 3491ms instead of 15000ms, naming `div.probe-photo-carousel`;
OrangeHRM's sticky top bar and nav sidebar — both positioned — reported as zero
overlays.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.browser.custom_controls import (  # noqa: E402
    CLICK_TIMEOUT_MS,
    DECORATION_MAX_ANCESTOR_VIEWPORT_RATIO,
    INTERCEPTION_VERDICT_DECORATION,
    INTERCEPTION_VERDICT_NOT_BLOCKED,
    INTERCEPTION_VERDICT_OVERLAY,
    INTERCEPTION_VERDICT_UNKNOWN,
    BlockedByOverlayError,
    check_or_force,
    classify_interception,
    click_element,
)

VIEWPORT = 1_296_000  # 1440x900, the viewport every measurement below was taken in


# --- the facts, exactly as measured on the live pages ------------------------

def decoration_facts(**overrides):
    """OrangeHRM Add Candidate consent checkbox."""
    facts = {
        "measurable": True,
        "target": "input",
        "interceptor": "i.oxd-icon.bi-check",
        "sameNode": False,
        "targetContainsInterceptor": False,
        "interceptorContainsTarget": False,
        "sharedAncestor": "label",
        "sharedAncestorArea": 432,
        "positionedLayers": [],
        "viewportArea": VIEWPORT,
    }
    facts.update(overrides)
    return facts


def overlay_facts(**overrides):
    """A lightbox over the Buzz submit button."""
    facts = {
        "measurable": True,
        "target": "button.oxd-button",
        "interceptor": "div.orangehrm-photo-carousel",
        "sameNode": False,
        "targetContainsInterceptor": False,
        "interceptorContainsTarget": False,
        "sharedAncestor": "body",
        "sharedAncestorArea": VIEWPORT,
        "positionedLayers": [
            {"node": "div.orangehrm-photo-carousel", "position": "fixed",
             "z": "9999", "area": VIEWPORT},
        ],
        "viewportArea": VIEWPORT,
    }
    facts.update(overrides)
    return facts


# ===========================================================================
# A -- the decision, on measured facts
# ===========================================================================


def test_a_the_decoration_is_recognised():
    assert classify_interception(decoration_facts()).verdict == INTERCEPTION_VERDICT_DECORATION


def test_a_the_overlay_is_recognised():
    assert classify_interception(overlay_facts()).verdict == INTERCEPTION_VERDICT_OVERLAY


def test_containment_is_not_what_decides_it():
    """The design error this test exists to prevent. Both real cases have
    `domContains` False, so a containment test cannot separate them — it would
    call the modal a decoration and force straight through it."""
    decoration = decoration_facts()
    overlay = overlay_facts()
    assert decoration["targetContainsInterceptor"] is False
    assert overlay["targetContainsInterceptor"] is False
    assert decoration["interceptorContainsTarget"] is False
    assert overlay["interceptorContainsTarget"] is False

    assert classify_interception(decoration).is_decoration
    assert classify_interception(overlay).is_overlay


def test_a_stacking_layer_beats_a_small_shared_ancestor():
    """Order matters. A small overlay — a popover over one field — is still an
    overlay, and must not be forced just because it is nearby."""
    facts = overlay_facts(sharedAncestor="div.field", sharedAncestorArea=400)
    assert classify_interception(facts).is_overlay


def test_the_click_point_being_on_the_control_is_not_blocked():
    assert (
        classify_interception(decoration_facts(sameNode=True)).verdict
        == INTERCEPTION_VERDICT_NOT_BLOCKED
    )


def test_a_child_of_the_target_is_not_blocked():
    """`<button><span>Save</span></button>`: the click lands on the span, which
    is the button. Playwright is happy with this; it never reaches the fallback."""
    assert (
        classify_interception(decoration_facts(targetContainsInterceptor=True)).verdict
        == INTERCEPTION_VERDICT_NOT_BLOCKED
    )


def test_something_big_in_the_flow_is_unknown_not_a_decoration():
    """No stacking layer, but a page-sized shared ancestor. Not attributable to
    the control's styling, so it must not be forced."""
    verdict = classify_interception(
        decoration_facts(sharedAncestor="div.page", sharedAncestorArea=VIEWPORT)
    )
    assert verdict.verdict == INTERCEPTION_VERDICT_UNKNOWN
    assert not verdict.is_decoration


def test_the_decoration_threshold_holds_at_its_boundary():
    at = int(VIEWPORT * DECORATION_MAX_ANCESTOR_VIEWPORT_RATIO)
    assert classify_interception(decoration_facts(sharedAncestorArea=at)).is_decoration
    assert not classify_interception(decoration_facts(sharedAncestorArea=at + 1000)).is_decoration


def test_the_real_measurements_sit_far_from_the_threshold():
    """A threshold chosen between 0.03% and 100% is not a tuned constant."""
    assert 432 / VIEWPORT < DECORATION_MAX_ANCESTOR_VIEWPORT_RATIO / 10
    assert 1.0 > DECORATION_MAX_ANCESTOR_VIEWPORT_RATIO * 10


def test_an_unmeasurable_page_is_unknown_never_forceable():
    for facts in (
        None,
        {},
        {"measurable": False, "detail": "the control has no size"},
        {"measurable": False, "detail": "the control is outside the viewport"},
    ):
        verdict = classify_interception(facts)
        assert verdict.verdict == INTERCEPTION_VERDICT_UNKNOWN, facts
        assert not verdict.is_decoration
        assert not verdict.is_overlay


def test_the_overlay_is_named_in_words_a_person_can_act_on():
    described = classify_interception(overlay_facts()).describe()
    assert "div.orangehrm-photo-carousel" in described
    assert "button.oxd-button" in described


def test_the_outermost_layer_is_the_one_reported():
    """The interceptor may be an `<img>` inside a carousel inside a backdrop. The
    useful name is the backdrop, not the image."""
    facts = overlay_facts(
        interceptor="img.orangehrm-photo-viewer-photo",
        positionedLayers=[
            {"node": "div.orangehrm-photo-carousel", "position": "absolute", "z": "1", "area": 500_000},
            {"node": "div.oxd-full-screen-backdrop", "position": "fixed", "z": "9999", "area": VIEWPORT},
        ],
    )
    assert classify_interception(facts).overlay == "div.oxd-full-screen-backdrop"


def test_the_verdict_serialises_for_the_report():
    payload = classify_interception(overlay_facts()).to_dict()
    assert payload["verdict"] == INTERCEPTION_VERDICT_OVERLAY
    assert payload["overlay"] == "div.orangehrm-photo-carousel"


# ===========================================================================
# B -- what the click actually does about it
# ===========================================================================


class _Keyboard:
    def __init__(self, on_press=None):
        self.pressed: list[str] = []
        self._on_press = on_press

    async def press(self, key: str) -> None:
        self.pressed.append(key)
        if self._on_press is not None:
            self._on_press()


class _Page:
    def __init__(self, on_escape=None):
        self.keyboard = _Keyboard(on_escape)
        self.waits: list[int] = []

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


class _Locator:
    """Records how it was clicked, and answers the measurement with given facts."""

    def __init__(self, facts, *, succeed_on: int = 0, page: _Page | None = None):
        self._facts = facts
        self._succeed_on = succeed_on  # attempt number that succeeds; 0 = never
        self.attempts: list[dict] = []
        self.page = page or _Page()

    def _record(self, **kwargs):
        self.attempts.append(kwargs)
        if self._succeed_on and len(self.attempts) >= self._succeed_on:
            return
        raise TimeoutError("Timeout 3000ms exceeded. subtree intercepts pointer events")

    async def click(self, timeout: int | None = None, force: bool = False) -> None:
        self._record(kind="click", timeout=timeout, force=force)

    async def check(self, timeout: int | None = None, force: bool = False) -> None:
        self._record(kind="check", timeout=timeout, force=force)

    async def uncheck(self, timeout: int | None = None, force: bool = False) -> None:
        self._record(kind="uncheck", timeout=timeout, force=force)

    async def evaluate(self, _script):
        facts = self._facts() if callable(self._facts) else self._facts
        return facts


def _run(coro):
    return asyncio.run(coro)


def test_b_a_click_that_works_is_never_measured():
    locator = _Locator(facts={"never": "asked"}, succeed_on=1)
    _run(click_element(locator))

    assert locator.attempts == [{"kind": "click", "timeout": CLICK_TIMEOUT_MS, "force": False}]


def test_b_the_first_attempt_does_not_pay_playwrights_15_second_default():
    locator = _Locator(facts=decoration_facts(), succeed_on=2)
    _run(click_element(locator))

    assert locator.attempts[0]["timeout"] == CLICK_TIMEOUT_MS
    assert CLICK_TIMEOUT_MS < 15000


def test_b_a_decoration_is_forced():
    locator = _Locator(facts=decoration_facts(), succeed_on=2)
    _run(click_element(locator))

    assert [a["force"] for a in locator.attempts] == [False, True]


def test_b_an_overlay_is_never_forced():
    """The safety-critical assertion. Forcing here would click a control behind a
    dialog whose own buttons may be destructive."""
    locator = _Locator(facts=overlay_facts())

    try:
        _run(click_element(locator))
        raise AssertionError("the click should not have succeeded")
    except BlockedByOverlayError:
        pass

    assert all(a["force"] is False for a in locator.attempts)


def test_b_an_overlay_is_escaped_once_and_the_click_retried():
    state = {"blocked": True}

    def clear():
        state["blocked"] = False

    page = _Page(on_escape=clear)
    locator = _Locator(
        facts=lambda: overlay_facts() if state["blocked"] else decoration_facts(sameNode=True),
        succeed_on=2,
        page=page,
    )
    _run(click_element(locator))

    assert page.keyboard.pressed == ["Escape"]
    assert [a["force"] for a in locator.attempts] == [False, False], "retried, not forced"


def test_b_an_overlay_that_survives_escape_reports_which_overlay():
    locator = _Locator(facts=overlay_facts())
    try:
        _run(click_element(locator))
        raise AssertionError("expected BlockedByOverlayError")
    except BlockedByOverlayError as exc:
        assert "div.orangehrm-photo-carousel" in str(exc)
        assert exc.interception.is_overlay
    assert locator.page.keyboard.pressed == ["Escape"], "it did try"


def test_b_an_unknown_failure_re_raises_the_original_error():
    """A disabled control, a detached node, a genuine timeout: unchanged
    behaviour, and specifically NOT forced."""
    locator = _Locator(facts=decoration_facts(sharedAncestorArea=VIEWPORT))
    try:
        _run(click_element(locator))
        raise AssertionError("expected the original failure")
    except BlockedByOverlayError:
        raise AssertionError("an unknown cause must not be reported as an overlay")
    except TimeoutError as exc:
        assert "Timeout 3000ms" in str(exc)

    assert all(a["force"] is False for a in locator.attempts)


def test_b_an_unmeasurable_page_re_raises_rather_than_forcing():
    locator = _Locator(facts=None)
    try:
        _run(click_element(locator))
        raise AssertionError("expected the original failure")
    except TimeoutError:
        pass
    assert all(a["force"] is False for a in locator.attempts)


def test_b_a_locator_with_no_page_cannot_press_escape_and_says_so():
    """Some adapters hand back locators with no page handle. It must fail as an
    overlay, not crash."""

    class _Pageless(_Locator):
        page = None

    locator = _Pageless(facts=overlay_facts())
    try:
        _run(click_element(locator))
        raise AssertionError("expected BlockedByOverlayError")
    except BlockedByOverlayError as exc:
        assert exc.interception.is_overlay


# ===========================================================================
# C -- the checkbox rule, which used to force through anything
# ===========================================================================


def test_c_a_decorated_checkbox_is_still_forced():
    locator = _Locator(facts=decoration_facts(), succeed_on=2)
    _run(check_or_force(locator, True))

    assert [(a["kind"], a["force"]) for a in locator.attempts] == [
        ("check", False), ("check", True),
    ]


def test_c_unchecking_a_decorated_checkbox_is_still_forced():
    locator = _Locator(facts=decoration_facts(), succeed_on=2)
    _run(check_or_force(locator, False))

    assert [(a["kind"], a["force"]) for a in locator.attempts] == [
        ("uncheck", False), ("uncheck", True),
    ]


def test_c_a_checkbox_behind_an_overlay_is_no_longer_forced():
    """The pre-existing bug. `check_or_force` forced on ANY failure, while its own
    docstring promised that a control "covered by a real modal should still fail
    rather than be forced". Confirmed live: before this change the tick went
    through a full-viewport overlay; now it is refused."""
    locator = _Locator(facts=overlay_facts())
    try:
        _run(check_or_force(locator, True))
        raise AssertionError("expected BlockedByOverlayError")
    except BlockedByOverlayError:
        pass

    assert all(a["force"] is False for a in locator.attempts)


def test_c_a_checkbox_behind_a_dismissable_overlay_is_retried_not_forced():
    state = {"blocked": True}
    page = _Page(on_escape=lambda: state.__setitem__("blocked", False))
    locator = _Locator(
        facts=lambda: overlay_facts() if state["blocked"] else decoration_facts(sameNode=True),
        succeed_on=2,
        page=page,
    )
    _run(check_or_force(locator, True))

    assert [a["force"] for a in locator.attempts] == [False, False]


def test_c_an_unknown_checkbox_failure_re_raises():
    locator = _Locator(facts=decoration_facts(sharedAncestorArea=VIEWPORT))
    try:
        _run(check_or_force(locator, True))
        raise AssertionError("expected the original failure")
    except TimeoutError:
        pass
    assert all(a["force"] is False for a in locator.attempts)


# ===========================================================================
# D -- one rule, one home
# ===========================================================================


def test_d_every_click_path_goes_through_the_same_rule():
    """Both executor click branches and the adapter. `fill_or_choose` is shared
    between these same two paths for the same reason: a rule about how to operate
    a control that has two implementations ends up with two answers."""
    import inspect

    from app.browser.adapters.direct import DirectPlaywrightAdapter
    from app.browser.executor import ActionExecutor

    executor_source = inspect.getsource(ActionExecutor._perform_element_action)
    assert executor_source.count("click_element(locator)") == 2, "CLICK and OPEN_TAB"
    assert "locator.click()" not in executor_source

    adapter_source = inspect.getsource(DirectPlaywrightAdapter.click_target)
    assert "click_element(locator)" in adapter_source
    assert "locator.click()" not in adapter_source


def test_d_the_overlay_detector_reaches_the_frontier_through_perception_only():
    """Mechanism-detected overlays go into the CANONICAL model, which drives
    `close_dialog`. They must not go into `PageState.dialogs`, which is what the
    model is told about the page — a nav sidebar described there as a dialog would
    manufacture bug reports."""
    from app.browser.observer import OBSERVE_SCRIPT
    from app.perception.dom_extractor import RAW_EXTRACTION_SCRIPT

    assert "stacking_layer" in RAW_EXTRACTION_SCRIPT
    assert "stacking_layer" not in OBSERVE_SCRIPT


def test_d_a_mechanism_detected_overlay_is_reported_less_confidently():
    """It is a measured overlay, not a declared one. The frontier's Escape is
    cheap, but the confidence should say which kind of evidence it rests on."""
    from app.perception.dialog_extractor import extract_dialogs

    class _Raw:
        dialogs = [
            {"dom_id": "el_1", "role": "dialog", "text": "Are you sure?",
             "detected_by": "aria"},
            {"dom_id": "el_2", "role": None, "text": "photo",
             "detected_by": "stacking_layer"},
        ]

    aria, mechanism = extract_dialogs(_Raw())
    assert aria.confidence.value == 1.0
    assert mechanism.confidence.value < aria.confidence.value
    assert mechanism.dialog_type == "modal"
    assert mechanism.is_open, "close_dialog only offers Escape for open dialogs"
    assert mechanism.element_id == "el_2", "and it needs an element_id to act on"
