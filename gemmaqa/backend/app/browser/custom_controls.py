"""Operating choosers that are built out of divs rather than <select>.

`fill()` types text into a control. A `<div role="combobox">` cannot receive
typed text, so filling one silently does nothing: on OrangeHRM's Admin -> Add
User form the agent "filled" User Role and Status, submitted, failed the
required-field validation, and repeated the whole cycle eight times.

A chooser has to be operated the way a person operates it — click to open, then
click an option — and its options do not exist in the DOM until it is open, so
they can only be read after that first click.

This lives in one module because BOTH execution paths need it (the native
Playwright path in `app.browser.executor` and `DirectBrowserAdapter.fill_target`).
Two copies of a rule is how the record-identity matcher ended up with two
different answers to the same question.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from app.safety.policies import TEST_DATA_PREFIX
from app.utils.logging import get_logger

logger = get_logger("browser.custom_controls")

# How long to wait for an opened chooser to render its options. Not a fixed
# settle delay: the wait ends the moment the first option is visible.
OPTION_WAIT_MS = 2000

# --- Lookup (autocomplete) resolution ----------------------------------------
#
# A lookup field is a TEXT input that only accepts values naming records the
# application already holds. Its DOM is identical to a free-text box — probed
# live on OrangeHRM's Admin -> Add User, the "Employee Name" input has no role,
# no aria-autocomplete, no list and no name, only a placeholder — so it cannot
# be told apart by inspection. Matching its placeholder text would be
# application-specific vocabulary, which the architecture forbids.
#
# It CAN be told apart by behaviour: typing into it makes an option list appear,
# and typing into an ordinary text box does not. Same form, same probe:
#
#   Employee Name + "GemmaQA_TEST_x9"  ->  role="listbox" with 1 option
#   Employee Name + "a"                ->  5 role="option" records
#   Username      + "GemmaQA_TEST_x9"  ->  nothing appears
#
# Telling a real record apart from a "no records" placeholder is the second
# half, and it is done without reading either one's words: a fabricated test
# token cannot match a stored record, so whatever the list shows while that
# token is in the field IS the application's empty state. Anything a different
# input produces that the token did not is a real record.
LOOKUP_FIRST_OPTION_TIMEOUT_MS = 700
# The empty state is not instantaneous — live capture saw "Searching...." at
# 39ms settling to "No Records Found" later. Every text shown during this window
# joins the baseline, so a transient cannot later be mistaken for a record.
LOOKUP_BASELINE_SETTLE_MS = 1800
# Real records arrived at 1.4-1.7s live, so a shorter budget would read as "no
# records exist" for an application that simply answers slowly.
LOOKUP_PROBE_BUDGET_MS = 2500
LOOKUP_POLL_MS = 150
# Two probes, not one: a lookup keyed on numbers matches neither letter. Bounded
# because each costs up to LOOKUP_PROBE_BUDGET_MS.
LOOKUP_PROBE_INPUTS = ("a", "1")

# Stable marker so the caller can recognise this failure without matching prose.
UNSATISFIED_REFERENCE_MARKER = "unsatisfied_record_reference"

# Text a chooser shows when nothing is chosen yet. It is rendered AS an option,
# and picking it leaves the field empty — which is indistinguishable from never
# having touched the control.
_PLACEHOLDER_WORDS = {"select", "select one", "choose", "none", ""}


async def custom_control_kind(locator: Any) -> str:
    """How confident are we that this is a chooser rather than a text field?

    Returns "declared" when ARIA says so, "focusable" when only a tabindex on a
    non-native element suggests it, and "" for an ordinary control.

    The distinction is what makes the fallback safe. OrangeHRM's pickers carry no
    ARIA whatsoever — they are `<div tabindex="0">` — so detection has to accept
    that weaker signal; but a focusable div could equally be a custom TEXT input,
    and guessing wrong there must not fail an action that a plain fill would have
    completed. A declared chooser failing is a real error; an inferred one failing
    just means it was never a chooser.
    """
    try:
        return str(
            await locator.evaluate(
                """(el) => {
                    const tag = el.tagName.toLowerCase();
                    if (['input', 'select', 'textarea'].includes(tag)) return '';
                    // A widget wrapping a real input is driven through that input.
                    if (el.querySelector('input, select, textarea')) return '';
                    const role = (el.getAttribute('role') || '').toLowerCase();
                    if (role === 'combobox' || role === 'listbox'
                        || el.getAttribute('aria-haspopup') === 'listbox') return 'declared';
                    if (el.hasAttribute('tabindex')) return 'focusable';
                    return '';
                }"""
            )
            or ""
        )
    except Exception:
        # Never let this decision break an ordinary fill.
        return ""


async def is_custom_choice_control(locator: Any) -> bool:
    return bool(await custom_control_kind(locator))


def _pick_option(texts: list[str], wanted: str) -> int:
    """Index of the option to click, or -1 if none is usable."""
    wanted_text = (wanted or "").strip().lower()
    if wanted_text:
        for index, text in enumerate(texts):
            if wanted_text in text.strip().lower():
                return index
    for index, text in enumerate(texts):
        cleaned = text.strip().strip("-").strip().lower()
        if cleaned not in _PLACEHOLDER_WORDS:
            return index
    return -1


async def choose_from_custom_control(locator: Any, wanted: str) -> str:
    """Open a custom chooser and pick an option. Returns the chosen text.

    Raises when the chooser does not open or offers nothing selectable — better a
    reported failure than a fill that quietly changes nothing and lets the run
    believe the field was completed.
    """
    await locator.click()
    options = locator.page.locator('[role="option"]:visible')
    try:
        await options.first.wait_for(state="visible", timeout=OPTION_WAIT_MS)
    except Exception as exc:
        raise ValueError("Custom choice control presented no options after being opened") from exc

    texts = await options.all_inner_texts()
    chosen = _pick_option(texts, wanted)
    if chosen < 0:
        raise ValueError(
            f"Custom choice control offered no selectable option (saw {texts[:5]!r})"
        )
    await options.nth(chosen).click()
    picked = texts[chosen].strip()
    logger.info("Chose %r from a custom control (wanted %r)", picked, wanted)
    return picked


class UnsatisfiedReferenceError(ValueError):
    """A lookup field names a record, and the application holds none to name.

    This is a finding, not a mishap: the form cannot be completed until the
    referenced record exists. Reporting it is what turns an endlessly rejected
    submit into a stated data prerequisite.
    """

    def __init__(self, message: str, *, observed: tuple[str, ...] = ()) -> None:
        super().__init__(f"{UNSATISFIED_REFERENCE_MARKER}: {message}")
        self.observed = observed


@dataclass
class FillOutcome:
    """What actually happened, since it is not always what was asked for."""

    kind: str = "fill"  # "fill" | "custom_choice" | "lookup"
    requested: str = ""
    chosen: str = ""
    probe: str = ""
    empty_state: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "requested": self.requested,
            "chosen": self.chosen,
            "probe": self.probe,
            "empty_state": list(self.empty_state),
        }


def _is_fabricated(value: str) -> bool:
    """Was this value invented by us, rather than supplied by the operator?

    Only a fabricated value licenses the empty-state baseline below, because
    only a fabricated value is guaranteed not to match a stored record. It also
    keeps the probe off every ordinary fill: credentials and operator-supplied
    data skip the whole path at zero cost.
    """
    return TEST_DATA_PREFIX in (value or "")


async def _visible_option_texts(page: Any) -> list[str]:
    try:
        return [t.strip() for t in await page.locator('[role="option"]:visible').all_inner_texts()]
    except Exception:
        return []


async def _collect_baseline(page: Any) -> set[str]:
    """Every option text shown while a non-matching value sits in the field."""
    seen: set[str] = set()
    deadline = time.monotonic() + LOOKUP_BASELINE_SETTLE_MS / 1000
    while time.monotonic() < deadline:
        seen.update(t for t in await _visible_option_texts(page) if t)
        await page.wait_for_timeout(LOOKUP_POLL_MS)
    return seen


async def _await_records(page: Any, baseline: set[str]) -> list[str]:
    """Poll until an option appears that the non-matching value never produced."""
    deadline = time.monotonic() + LOOKUP_PROBE_BUDGET_MS / 1000
    while time.monotonic() < deadline:
        texts = await _visible_option_texts(page)
        fresh = [t for t in texts if t and t not in baseline]
        if fresh:
            return fresh
        await page.wait_for_timeout(LOOKUP_POLL_MS)
    return []


async def resolve_lookup(locator: Any, typed_value: str) -> FillOutcome:
    """Decide whether a just-filled text box is really a lookup, and satisfy it.

    Returns a plain `fill` outcome when nothing suggests a lookup — the common
    case, and the cheap one. Raises `UnsatisfiedReferenceError` when the field
    IS a lookup and the application offers no record to reference.
    """
    outcome = FillOutcome(requested=typed_value)
    if not _is_fabricated(typed_value):
        return outcome

    page = locator.page
    try:
        await page.locator('[role="option"]:visible').first.wait_for(
            state="visible", timeout=LOOKUP_FIRST_OPTION_TIMEOUT_MS
        )
    except Exception:
        return outcome  # An ordinary text box: nothing offered anything to pick.

    baseline = await _collect_baseline(page)
    outcome.kind = "lookup"
    outcome.empty_state = tuple(sorted(baseline))
    logger.info(
        "Field behaves as a lookup; empty state for a non-matching value is %r",
        outcome.empty_state,
    )

    for probe in LOOKUP_PROBE_INPUTS:
        await locator.fill(probe)
        records = await _await_records(page, baseline)
        if not records:
            continue
        texts = await _visible_option_texts(page)
        try:
            index = texts.index(records[0])
        except ValueError:  # the list moved under us; re-read and take the first
            continue
        await page.locator('[role="option"]:visible').nth(index).click()
        outcome.chosen = records[0]
        outcome.probe = probe
        logger.info(
            "Lookup satisfied with an existing record %r (probe %r) instead of the "
            "invented value %r", outcome.chosen, probe, typed_value,
        )
        return outcome

    raise UnsatisfiedReferenceError(
        "this field only accepts records the application already holds, and it "
        f"offered none (it answered every input with {outcome.empty_state!r})",
        observed=outcome.empty_state,
    )


# A normal tick succeeds in well under a second. The point of shortening this
# from Playwright's 15s default is that the FAILING case is the common one on
# decorated checkboxes, and it is paid before the fallback can run.
CHECK_TIMEOUT_MS = 3000
# The same reasoning for clicks. A click that is going to be intercepted is
# intercepted on the first attempt; the remaining 12s of Playwright's default is
# spent re-confirming it. Measured cost of the default on one blocked submit:
# 15s of a 40-action budget, for no information.
CLICK_TIMEOUT_MS = 3000
# After Escape, before re-measuring. Overlays animate out.
OVERLAY_DISMISS_SETTLE_MS = 400

# A decoration and the control it decorates share a SMALL ancestor; an overlay
# and the control it covers share the page. Measured on OrangeHRM's styled
# checkbox: the shared ancestor is the 324px² `span.oxd-checkbox-input` inside a
# 1,296,000px² viewport — a ratio of 0.00025. An overlay's shared ancestor with
# its target is `<body>`, i.e. 1.0. Five percent sits far from both.
DECORATION_MAX_ANCESTOR_VIEWPORT_RATIO = 0.05

INTERCEPTION_VERDICT_NOT_BLOCKED = "not_blocked"
INTERCEPTION_VERDICT_DECORATION = "decoration"
INTERCEPTION_VERDICT_OVERLAY = "overlay"
INTERCEPTION_VERDICT_UNKNOWN = "unknown"


class BlockedByOverlayError(RuntimeError):
    """A control could not be clicked because something else is covering it.

    Distinct from a plain timeout because the remedy is different and because
    saying so is the whole point: a run that reports "Timeout 15000ms exceeded"
    has told the operator nothing, while one that reports which overlay was in
    the way has told them everything.
    """

    def __init__(self, interception: "Interception") -> None:
        self.interception = interception
        super().__init__(interception.describe())


@dataclass(frozen=True)
class Interception:
    """What is on top of the target, and what that means for how to click it."""

    verdict: str
    measurable: bool = True
    target: str = ""
    interceptor: str = ""
    overlay: str | None = None
    detail: str = ""

    @property
    def is_decoration(self) -> bool:
        return self.verdict == INTERCEPTION_VERDICT_DECORATION

    @property
    def is_overlay(self) -> bool:
        return self.verdict == INTERCEPTION_VERDICT_OVERLAY

    def describe(self) -> str:
        if self.verdict == INTERCEPTION_VERDICT_OVERLAY:
            return (
                f"{self.target or 'the control'} is covered by {self.overlay or 'an overlay'} "
                f"(the click would land on {self.interceptor or 'something else'})"
            )
        if self.verdict == INTERCEPTION_VERDICT_DECORATION:
            return f"{self.target or 'the control'} is decorated by {self.interceptor}"
        if not self.measurable:
            return f"could not measure what covers {self.target or 'the control'}: {self.detail}"
        return self.detail or self.verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "measurable": self.measurable,
            "target": self.target,
            "interceptor": self.interceptor,
            "overlay": self.overlay,
            "detail": self.detail,
        }


# Facts only. Every judgement is made in `classify_interception`, in Python,
# where it can be tested without a browser.
INTERCEPTION_JS = """
(el) => {
  const describe = (n) => {
    if (!n) return null;
    const cls = (n.className && typeof n.className === 'string')
      ? '.' + n.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
    return n.tagName.toLowerCase() + cls;
  };
  const area = (n) => {
    const r = n.getBoundingClientRect();
    return Math.max(0, Math.round(r.width * r.height));
  };
  const viewportArea = Math.max(1, Math.round(innerWidth * innerHeight));
  const rect = el.getBoundingClientRect();
  if (!rect.width || !rect.height)
    return {measurable: false, detail: 'the control has no size'};
  const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
  if (x < 0 || y < 0 || x > innerWidth || y > innerHeight)
    return {measurable: false, detail: 'the control is outside the viewport'};
  const top = document.elementFromPoint(x, y);
  if (!top) return {measurable: false, detail: 'nothing is at the click point'};

  const out = {
    measurable: true,
    target: describe(el),
    interceptor: describe(top),
    sameNode: top === el,
    targetContainsInterceptor: el.contains(top),
    interceptorContainsTarget: top.contains(el),
    viewportArea,
  };
  if (out.sameNode || out.targetContainsInterceptor) return out;

  // The smallest ancestor holding both. Small => one control and its styling.
  // Large => two unrelated things, one painted over the other.
  let shared = top.parentElement;
  while (shared && !shared.contains(el)) shared = shared.parentElement;
  out.sharedAncestor = describe(shared);
  out.sharedAncestorArea = shared ? area(shared) : viewportArea;

  // A stacking layer ABOVE the target is the only way CSS can paint unrelated
  // content over it, so its presence is the mechanism, not a heuristic.
  out.positionedLayers = [];
  for (let n = top; n && out.positionedLayers.length < 5; n = n.parentElement) {
    const s = getComputedStyle(n);
    const positioned = s.position === 'fixed'
      || ((s.position === 'absolute' || s.position === 'sticky') && s.zIndex !== 'auto');
    if (positioned && !n.contains(el)) {
      out.positionedLayers.push({node: describe(n), position: s.position,
                                 z: s.zIndex, area: area(n)});
    }
  }
  return out;
}
"""


def classify_interception(facts: dict[str, Any] | None) -> Interception:
    """Decide, from measured facts, whether forcing the click would be honest.

    Forcing requires POSITIVE evidence of a decoration. Anything else — an
    overlay, or facts that do not settle the question — must fail, because
    forcing skips the hit-target test and would click a control no person could
    reach. `check_or_force` used to force on any failure, which its own docstring
    already said was wrong for a control "covered by a real modal".
    """
    if not facts:
        return Interception(
            verdict=INTERCEPTION_VERDICT_UNKNOWN,
            measurable=False,
            detail="the page returned no measurement",
        )
    if not facts.get("measurable"):
        return Interception(
            verdict=INTERCEPTION_VERDICT_UNKNOWN,
            measurable=False,
            target=str(facts.get("target") or ""),
            detail=str(facts.get("detail") or "unmeasurable"),
        )

    target = str(facts.get("target") or "")
    interceptor = str(facts.get("interceptor") or "")

    if facts.get("sameNode") or facts.get("targetContainsInterceptor"):
        return Interception(
            verdict=INTERCEPTION_VERDICT_NOT_BLOCKED,
            target=target,
            interceptor=interceptor,
            detail="the click point is on the control itself",
        )

    layers = list(facts.get("positionedLayers") or [])
    if layers:
        outermost = layers[-1]
        return Interception(
            verdict=INTERCEPTION_VERDICT_OVERLAY,
            target=target,
            interceptor=interceptor,
            overlay=str(outermost.get("node") or "an overlay"),
            detail=(
                f"a {outermost.get('position')} layer (z-index {outermost.get('z')}) "
                f"covers the control"
            ),
        )

    viewport_area = max(1, int(facts.get("viewportArea") or 1))
    shared_area = int(facts.get("sharedAncestorArea") or viewport_area)
    if shared_area / viewport_area <= DECORATION_MAX_ANCESTOR_VIEWPORT_RATIO:
        return Interception(
            verdict=INTERCEPTION_VERDICT_DECORATION,
            target=target,
            interceptor=interceptor,
            detail=(
                f"the control and {interceptor} share {facts.get('sharedAncestor')}, "
                f"{shared_area}px² of a {viewport_area}px² viewport"
            ),
        )

    # An ancestor wrapping most of the page, but no stacking layer: an
    # in-flow element is sitting where the control is. Not attributable to
    # decoration, so not forceable.
    return Interception(
        verdict=INTERCEPTION_VERDICT_UNKNOWN,
        target=target,
        interceptor=interceptor,
        detail=(
            f"{interceptor} covers the control but shares only "
            f"{facts.get('sharedAncestor')} with it, and sits in no stacking layer"
        ),
    )


async def measure_interception(locator: Any) -> Interception:
    """What is at the target's click point, right now."""
    try:
        facts = await locator.evaluate(INTERCEPTION_JS)
    except Exception as exc:
        return Interception(
            verdict=INTERCEPTION_VERDICT_UNKNOWN,
            measurable=False,
            detail=f"measurement failed ({type(exc).__name__})",
        )
    return classify_interception(facts)


async def _dismiss_overlay(locator: Any) -> bool:
    """Press Escape and report whether the target became reachable.

    Escape only. The frontier's `close_dialog` candidate already settled this
    question for the whole codebase — "offer a safe, standard Escape-key close
    rather than guessing which contained control is the real close button" — and
    the reason is stronger inside the executor: an overlay's own buttons are
    unlabelled icons as often as not, and on a confirmation dialog they read
    "Yes, Delete!" as readily as "Cancel". Clicking one from here would perform a
    consequential action that never passed the safety validator.
    """
    page = getattr(locator, "page", None)
    keyboard = getattr(page, "keyboard", None)
    if keyboard is None:
        return False
    try:
        await keyboard.press("Escape")
        await page.wait_for_timeout(OVERLAY_DISMISS_SETTLE_MS)
    except Exception as exc:
        logger.debug("Escape did not reach the page: %s", type(exc).__name__)
        return False
    return not (await measure_interception(locator)).is_overlay


@asynccontextmanager
async def accepting_native_dialogs(page: Any | None):
    """Accept `window.confirm` / `alert` for the duration of one click.

    Playwright dismisses unhandled dialogs (Cancel). Contact List's Delete
    Contact uses `confirm('Are you sure you want to delete this contact?')`;
    dismissing it leaves the record on the page and the click reports success.
    Only the executor should enter this around an authorized cleanup click —
    never around an arbitrary action.
    """
    if page is None or not hasattr(page, "on"):
        yield
        return

    async def _accept(dialog: Any) -> None:
        kind = getattr(dialog, "type", "dialog")
        message = str(getattr(dialog, "message", "") or "")[:120]
        logger.info("Accepting native %s dialog for authorized cleanup: %s", kind, message)
        await dialog.accept()

    page.on("dialog", _accept)
    try:
        yield
    finally:
        remove = getattr(page, "remove_listener", None)
        if callable(remove):
            try:
                remove("dialog", _accept)
            except Exception:
                pass


async def click_element(locator: Any) -> None:
    """Click a control, and when something is in the way, say what.

    Measured on OrangeHRM's Buzz create (run a1f300a5, action 40): an image
    lightbox opened by an earlier action was still on screen, and Playwright
    reported

        element is visible, enabled and stable
        <img class="orangehrm-photo-viewer-photo"/> from
        <div class="orangehrm-photo-carousel --web"> subtree intercepts pointer events
        ... Timeout 15000ms exceeded

    then the create was abandoned. Fifteen seconds bought one useless sentence.

    The order here is deliberate. Try normally, so a click that works keeps every
    actionability guarantee. Then MEASURE what is on top — `elementFromPoint` at
    the click point names the interceptor directly, so nothing depends on parsing
    Playwright's error text, which varies by version. Only then decide:

      decoration -> force, because the hit-target test is the one check that
                    cannot be satisfied and the click does reach the control
      overlay    -> Escape once and retry; if it persists, fail SAYING SO
      unknown    -> fail, unchanged. Absence of evidence is not evidence of
                    decoration.
    """
    try:
        await locator.click(timeout=CLICK_TIMEOUT_MS)
        return
    except Exception as exc:
        first_attempt = exc

    interception = await measure_interception(locator)

    if interception.is_decoration:
        logger.info("Click intercepted by decoration (%s); forcing", interception.describe())
        await locator.click(force=True)
        return

    if interception.is_overlay:
        logger.info("Click blocked: %s; pressing Escape", interception.describe())
        if await _dismiss_overlay(locator):
            logger.info("Overlay dismissed; retrying the click")
            await locator.click(timeout=CLICK_TIMEOUT_MS)
            return
        raise BlockedByOverlayError(interception)

    logger.info(
        "Click failed (%s) and the page does not show it as intercepted: %s",
        type(first_attempt).__name__, interception.describe(),
    )
    raise first_attempt


async def check_or_force(locator: Any, checked: bool = True) -> None:
    """Tick a checkbox, including one whose own styling swallows the click.

    A decorated checkbox is not hidden — it is covered. Live on OrangeHRM's Add
    Candidate consent box, Playwright reported:

        element is visible, enabled and stable
        <i class="oxd-icon bi-check oxd-checkbox-input-icon"> from
        <span class="oxd-checkbox-input"> subtree intercepts pointer events

    The real `<input type="checkbox">` passes every actionability check and then
    the decoration painted on top of it receives the click instead, so
    `check()` retries until it times out. `force` skips the hit-target test,
    which is the one that cannot be satisfied here.

    Try normally first — the ordinary path keeps its full safety checking. The
    retry now forces only where the page shows a DECORATION. This used to force
    on any failure at all, contradicting the promise made in this docstring that
    a control "covered by a real modal should still fail rather than be forced":
    measured on that same consent box, the interceptor is a sibling's child, so
    no containment test separates it from a modal — only the shared-ancestor size
    and the absence of a stacking layer do.
    """
    try:
        if checked:
            await locator.check(timeout=CHECK_TIMEOUT_MS)
        else:
            await locator.uncheck(timeout=CHECK_TIMEOUT_MS)
        return
    except Exception as exc:
        first_attempt = exc

    interception = await measure_interception(locator)

    if interception.is_overlay:
        logger.info("Checkbox blocked: %s; pressing Escape", interception.describe())
        if await _dismiss_overlay(locator):
            if checked:
                await locator.check(timeout=CHECK_TIMEOUT_MS)
            else:
                await locator.uncheck(timeout=CHECK_TIMEOUT_MS)
            return
        raise BlockedByOverlayError(interception)

    if not interception.is_decoration:
        logger.info(
            "Checkbox did not accept a normal click (%s) and the page does not "
            "show a decoration: %s", type(first_attempt).__name__, interception.describe(),
        )
        raise first_attempt

    logger.info(
        "Checkbox is covered by its own styling (%s); retrying without the "
        "hit-target check", interception.describe(),
    )
    if checked:
        await locator.check(force=True)
    else:
        await locator.uncheck(force=True)


async def _is_file_input(locator: Any) -> bool:
    try:
        return bool(
            await locator.evaluate(
                "(el) => el.tagName.toLowerCase() === 'input' && el.type === 'file'"
            )
        )
    except Exception:
        return False


async def fill_or_choose(locator: Any, value: str) -> FillOutcome:
    """Fill a normal control, or operate a custom chooser. One entry point so
    both execution paths behave identically."""
    # A file input cannot be typed into, and applications almost always hide the
    # real <input type="file"> behind a styled "Browse" button — so `fill()`
    # spends its full 15s visibility timeout and then fails. Live on OrangeHRM's
    # Add Candidate: `fill(...) on <input type="file" class="oxd-file-input">`
    # -> "element is not visible", 15s burnt, Resume left empty.
    #
    # `set_input_files` is the verb Playwright provides for this, and it works on
    # a hidden input by design. The generator already produces a real path for
    # the `file_upload` semantic type; only the verb was wrong.
    if await _is_file_input(locator):
        await locator.set_input_files(value)
        logger.info("Uploaded %r to a file input", value)
        return FillOutcome(kind="file_upload", requested=value, chosen=value)
    kind = await custom_control_kind(locator)
    if not kind:
        await locator.fill(value)
        # A lookup is a text box until it is typed into, so this can only be
        # asked after the fill, never before it.
        return await resolve_lookup(locator, value)
    try:
        chosen = await choose_from_custom_control(locator, value)
        return FillOutcome(kind="custom_choice", requested=value, chosen=chosen)
    except UnsatisfiedReferenceError:
        raise
    except ValueError:
        if kind == "declared":
            # It told us it was a chooser and then offered nothing. That is a
            # real failure and must be reported, not papered over with a fill
            # that would quietly change nothing.
            raise
        # Only inferred from a tabindex, so it may well be a custom text field.
        logger.info("Focusable element offered no options; filling it instead")
        await locator.fill(value)
        return await resolve_lookup(locator, value)
