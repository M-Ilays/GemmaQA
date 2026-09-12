"""Operating choosers built out of divs rather than <select>.

`fill()` types text into a control, and a `<div role="combobox">` cannot receive
typed text — so filling one did nothing at all, silently. On OrangeHRM's
Admin -> Add User form GemmaQA "filled" User Role and Status, submitted, failed
the required-field validation, and repeated the cycle eight times before the
frontier went dry.

A chooser has to be operated the way a person operates it: click to open, then
click an option. Its options do not exist in the DOM until it is open.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app.browser.custom_controls import (  # noqa: E402
    _pick_option,
    choose_from_custom_control,
    fill_or_choose,
    is_custom_choice_control,
)


# ===========================================================================
# A -- choosing which option to click
# ===========================================================================

# What OrangeHRM's User Role chooser renders once opened: its own placeholder
# first, then the real choices.
REAL_OPTIONS = ["-- Select --", "Admin", "ESS"]


def test_a_requested_value_is_matched():
    assert _pick_option(REAL_OPTIONS, "ESS") == 2


def test_matching_ignores_case_and_surrounding_text():
    assert _pick_option(["-- Select --", "Enabled", "Disabled"], "disabled") == 2


def test_the_placeholder_is_never_chosen():
    """It is rendered AS an option, and picking it leaves the field empty —
    indistinguishable from never having touched the control."""
    assert _pick_option(REAL_OPTIONS, "") == 1


def test_an_unmatched_request_falls_back_to_the_first_real_option():
    """Better a valid choice than none: the field is required, and the run needs
    the form to submit so the application's own validation can be tested."""
    assert _pick_option(REAL_OPTIONS, "Nonexistent Role") == 1


def test_a_chooser_offering_only_a_placeholder_yields_nothing():
    for placeholder in (["-- Select --"], ["Select"], ["choose"], [""], ["   "], ["----"]):
        assert _pick_option(placeholder, "") == -1, placeholder


def test_no_options_at_all_yields_nothing():
    assert _pick_option([], "Admin") == -1


def test_the_requested_value_wins_over_the_first_real_option():
    assert _pick_option(["-- Select --", "Admin", "ESS"], "ESS") == 2


# ===========================================================================
# B -- recognising a custom chooser
# ===========================================================================


class _FakeLocator:
    """Enough of a Playwright Locator to exercise the decision logic."""

    def __init__(self, *, evaluate_result=False, evaluate_raises=False):
        self._evaluate_result = evaluate_result
        self._evaluate_raises = evaluate_raises
        self.filled: list[str] = []
        self.clicked = 0
        # A real Locator always has one, and `fill_or_choose` now asks a filled
        # text box whether it was secretly a lookup (see test_lookup_reference).
        self.page = _FakePage(_FakeOptions([], visible=False))

    async def evaluate(self, script):
        if self._evaluate_raises:
            raise RuntimeError("detached")
        # A real Locator runs the script it is given. `fill_or_choose` asks two
        # different questions now — "is this a file input?" then "is this a
        # chooser?" — so a fake that answers both the same way makes every
        # chooser look like a file input.
        if "'file'" in script:
            return False
        return self._evaluate_result

    async def fill(self, value):
        self.filled.append(value)

    async def click(self):
        self.clicked += 1


def test_aria_declares_a_custom_chooser():
    assert asyncio.run(is_custom_choice_control(_FakeLocator(evaluate_result=True))) is True


def test_an_ordinary_input_is_not_a_custom_chooser():
    assert asyncio.run(is_custom_choice_control(_FakeLocator(evaluate_result=False))) is False


def test_a_detached_element_does_not_break_an_ordinary_fill():
    """This decision must never be the thing that fails an action."""
    assert asyncio.run(is_custom_choice_control(_FakeLocator(evaluate_raises=True))) is False


# ===========================================================================
# C -- the one entry point both execution paths share
# ===========================================================================


def test_an_ordinary_control_is_still_filled():
    locator = _FakeLocator(evaluate_result=False)
    asyncio.run(fill_or_choose(locator, "GemmaQA_TEST_value"))

    assert locator.filled == ["GemmaQA_TEST_value"]
    assert locator.clicked == 0


def test_both_execution_paths_go_through_the_same_helper():
    """Two copies of this rule is how the record-identity matcher ended up with
    two different answers to the same question."""
    import inspect

    from app.browser.adapters.direct import DirectPlaywrightAdapter
    from app.browser.executor import ActionExecutor

    assert "fill_or_choose" in inspect.getsource(ActionExecutor._perform_element_action)
    assert "fill_or_choose" in inspect.getsource(DirectPlaywrightAdapter.fill_target)


# ===========================================================================
# D -- opening the chooser, and failing honestly when it will not
# ===========================================================================


class _FakeOptions:
    def __init__(self, texts, *, visible=True):
        self._texts = texts
        self._visible = visible
        self.clicked_index: int | None = None

    @property
    def first(self):
        return self

    def nth(self, index):
        self.clicked_index = index
        return self

    async def wait_for(self, **_kw):
        if not self._visible:
            raise TimeoutError("no options")

    async def all_inner_texts(self):
        return list(self._texts)

    async def click(self):
        return None


class _FakePage:
    def __init__(self, options):
        self._options = options

    def locator(self, _selector):
        return self._options


class _ChooserLocator(_FakeLocator):
    def __init__(self, options):
        super().__init__(evaluate_result=True)
        self.page = _FakePage(options)


def test_the_chooser_is_opened_before_its_options_are_read():
    options = _FakeOptions(REAL_OPTIONS)
    locator = _ChooserLocator(options)
    picked = asyncio.run(choose_from_custom_control(locator, "ESS"))

    assert locator.clicked == 1  # opened
    assert options.clicked_index == 2  # then the matching option
    assert picked == "ESS"
    assert locator.filled == []  # never typed into


def test_a_chooser_that_does_not_open_raises_rather_than_reporting_success():
    """A silent no-op is what let the run believe a required field was
    completed, submit, and loop on the rejection."""
    locator = _ChooserLocator(_FakeOptions(REAL_OPTIONS, visible=False))
    with pytest.raises(ValueError, match="presented no options"):
        asyncio.run(choose_from_custom_control(locator, "ESS"))


def test_a_chooser_with_nothing_selectable_raises():
    locator = _ChooserLocator(_FakeOptions(["-- Select --"]))
    with pytest.raises(ValueError, match="no selectable option"):
        asyncio.run(choose_from_custom_control(locator, ""))


# ===========================================================================
# E -- detection when the control declares nothing
#
# Confirmed against the live page: OrangeHRM's pickers carry NO ARIA at all.
# They are <div class="oxd-select-text-input" tabindex="0">-- Select --</div>.
# So detection has to accept a plain tabindex — and because a focusable div
# could equally be a custom TEXT field, an inferred guess must fall back to
# filling rather than failing an action a fill would have completed.
# ===========================================================================


class _KindLocator(_FakeLocator):
    def __init__(self, kind: str, options=None):
        super().__init__(evaluate_result=kind)
        self.page = _FakePage(options if options is not None else _FakeOptions([]))


def test_aria_gives_a_declared_chooser():
    from app.browser.custom_controls import custom_control_kind

    assert asyncio.run(custom_control_kind(_KindLocator("declared"))) == "declared"


def test_a_bare_tabindex_gives_an_inferred_chooser():
    from app.browser.custom_controls import custom_control_kind

    assert asyncio.run(custom_control_kind(_KindLocator("focusable"))) == "focusable"


def test_a_declared_chooser_that_offers_nothing_is_a_reported_failure():
    locator = _KindLocator("declared", _FakeOptions(REAL_OPTIONS, visible=False))
    with pytest.raises(ValueError):
        asyncio.run(fill_or_choose(locator, "ESS"))
    assert locator.filled == []


def test_an_inferred_chooser_that_offers_nothing_is_filled_instead():
    """A focusable div may simply be a custom text field; guessing wrong must not
    fail an action that a plain fill would have completed."""
    locator = _KindLocator("focusable", _FakeOptions([], visible=False))
    asyncio.run(fill_or_choose(locator, "GemmaQA_TEST_x"))

    assert locator.filled == ["GemmaQA_TEST_x"]


def test_an_inferred_chooser_that_does_open_is_chosen_from():
    options = _FakeOptions(REAL_OPTIONS)
    locator = _KindLocator("focusable", options)
    asyncio.run(fill_or_choose(locator, "ESS"))

    assert options.clicked_index == 2
    assert locator.filled == []


def test_the_extractor_looks_for_focusable_non_native_controls():
    """The selector is the whole fix: OrangeHRM declares no ARIA, so an
    ARIA-only selector found nothing and the two pickers stayed invisible."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "backend/app/perception/dom_extractor.py"
    text = source.read_text(encoding="utf-8")
    assert "[tabindex]:not(input):not(select):not(textarea):not(button):not(a)" in text


# ===========================================================================
# Native confirm() during authorized cleanup clicks
# ===========================================================================


class _FakeConfirmDialog:
    def __init__(self) -> None:
        self.accepted = False
        self.dismissed = False
        self.type = "confirm"
        self.message = "Are you sure you want to delete this contact?"

    async def accept(self) -> None:
        self.accepted = True

    async def dismiss(self) -> None:
        self.dismissed = True


class _FakeDialogPage:
    def __init__(self) -> None:
        self.handlers: list = []

    def on(self, event: str, handler) -> None:
        assert event == "dialog"
        self.handlers.append(handler)

    def remove_listener(self, event: str, handler) -> None:
        self.handlers = [h for h in self.handlers if h is not handler]


def test_accepting_native_dialogs_accepts_confirm_and_uninstalls():
    """Playwright's default is dismiss (Cancel). Contact List Delete Contact
    never removes the record unless confirm() is accepted."""
    from app.browser.custom_controls import accepting_native_dialogs

    page = _FakeDialogPage()
    dialog = _FakeConfirmDialog()

    async def _run() -> None:
        async with accepting_native_dialogs(page):
            assert len(page.handlers) == 1
            await page.handlers[0](dialog)
            assert dialog.accepted
            assert not dialog.dismissed
        assert page.handlers == []

    asyncio.run(_run())


def test_accepting_native_dialogs_is_a_no_op_without_a_page():
    from app.browser.custom_controls import accepting_native_dialogs

    async def _run() -> None:
        async with accepting_native_dialogs(None):
            pass

    asyncio.run(_run())


def test_both_executor_click_paths_accept_native_confirm_on_cleanup():
    """A rule about dialogs that lives on only one click path is how Contact
    List deletes succeeded-and-stayed on /contactDetails."""
    import inspect

    from app.browser.executor import ActionExecutor

    adapter_source = inspect.getsource(ActionExecutor._execute_via_adapter)
    native_source = inspect.getsource(ActionExecutor._perform_element_action)
    assert "_click_accepting_cleanup_dialog" in adapter_source
    assert "_click_accepting_cleanup_dialog" in native_source

