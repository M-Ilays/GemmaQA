"""A submit must be observed AFTER the application answers it, not before.

Recording successful mutating requests made acceptance *representable*. It did
not make it *visible*: the observation that feeds `classify_submission` was
still built too early. Measured on OrangeHRM's Buzz save, click -> settle ->
observe:

    t+27.092s   after_state built
    t+27.457s   POST 200 /api/v2/buzz/posts     <- 365ms too late

so the classifier read an unchanged page, returned `unknown`, and a create that
plainly succeeded was recorded `submitted_unverified`. A/B on the same page and
session afterwards:

    await_write=False   settle held  426ms   outcome=unknown   (POST never seen)
    await_write=True    settle held 1553ms   outcome=accepted  signals=[mutating_request_succeeded_200]

`domcontentloaded` cannot stand in for this: on a single-page application the
submit causes no navigation, so it returns at once and reports a page that has
finished rendering but has not been answered.

No test here sleeps. The fake page's `wait_for_timeout` advances the scenario
instead, so the polling loop is driven deterministically.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.browser.executor import (  # noqa: E402
    SUBMIT_WRITE_COMPLETE_TIMEOUT_MS,
    SUBMIT_WRITE_START_TIMEOUT_MS,
    ActionExecutor,
)
from app.browser.network_monitor import (  # noqa: E402
    INFLIGHT_MAX_AGE_S,
    NetworkMonitor,
)

SAVE_URL = "https://app.test/api/v2/records"


class _Req:
    def __init__(self, method: str, url: str = SAVE_URL, resource_type: str = "xhr"):
        self.method = method
        self.url = url
        self.resource_type = resource_type
        self.failure = None


class _Resp:
    def __init__(self, status: int, request: _Req):
        self.status = status
        self.request = request


class _EventPage:
    """Captures the handlers `NetworkMonitor.attach` registers."""

    def __init__(self):
        self.handlers: dict[str, list] = {}

    def on(self, event: str, handler):
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event: str, payload):
        for handler in self.handlers.get(event, []):
            handler(payload)


def _attached() -> tuple[NetworkMonitor, _EventPage]:
    monitor, page = NetworkMonitor(), _EventPage()
    monitor.attach(page)
    return monitor, page


class _FakePage:
    """A page whose waits advance a scripted scenario rather than the clock.

    `on_wait(page)` is called on every `wait_for_timeout`, so a test can make a
    response arrive after a chosen number of polls. `waited_ms` is the total the
    production code asked to wait for — the thing worth asserting on.
    """

    url = "https://app.test/records"

    def __init__(self, on_wait=None):
        self.waited_ms = 0
        self.waits: list[int] = []
        self.load_states: list[str] = []
        self._on_wait = on_wait

    async def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms += ms
        self.waits.append(ms)
        if self._on_wait is not None:
            self._on_wait(self)

    async def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        self.load_states.append(state)


def _executor(monitor: NetworkMonitor | None, settle_ms: int = 400) -> ActionExecutor:
    return ActionExecutor("run", network_monitor=monitor, settle_ms=settle_ms)


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# A -- in-flight bookkeeping
# ===========================================================================


def test_a_mutating_request_is_in_flight_until_answered():
    monitor, page = _attached()
    page.fire("request", _Req("POST"))

    assert monitor.mutations_in_flight() == 1

    page.fire("response", _Resp(200, _Req("POST")))
    assert monitor.mutations_in_flight() == 0


def test_a_read_is_never_in_flight():
    """An SPA has GETs outstanding almost continuously. Counting them would make
    every submit wait for unrelated traffic."""
    monitor, page = _attached()
    for i in range(10):
        page.fire("request", _Req("GET", f"https://app.test/api/list/{i}"))

    assert monitor.mutations_in_flight() == 0


def test_a_transport_failure_also_settles_the_write():
    """A blocked or reset write never produces a response. If only responses
    cleared the counter, the next submit would wait out its whole budget."""
    monitor, page = _attached()
    page.fire("request", _Req("POST"))
    page.fire("requestfailed", _Req("POST"))

    assert monitor.mutations_in_flight() == 0
    assert monitor.mutation_responses == 1


def test_two_writes_to_the_same_endpoint_are_counted_separately():
    monitor, page = _attached()
    page.fire("request", _Req("POST"))
    page.fire("request", _Req("POST"))
    assert monitor.mutations_in_flight() == 2

    page.fire("response", _Resp(200, _Req("POST")))
    assert monitor.mutations_in_flight() == 1


def test_a_write_whose_terminal_event_never_arrives_ages_out():
    monitor, page = _attached()
    page.fire("request", _Req("POST"))
    assert monitor.mutations_in_flight() == 1

    # Backdate it past the cutoff — the same effect as a lost terminal event.
    monitor._inflight_mutations = [
        (start - INFLIGHT_MAX_AGE_S - 1, url) for start, url in monitor._inflight_mutations
    ]
    assert monitor.mutations_in_flight() == 0


def test_the_response_counter_only_ever_rises():
    monitor, page = _attached()
    page.fire("request", _Req("POST"))
    page.fire("response", _Resp(200, _Req("POST")))
    page.fire("response", _Resp(500, _Req("PUT", "https://app.test/other")))

    assert monitor.mutation_responses == 2


def test_clear_forgets_pending_writes_but_not_the_counter():
    """A caller holds a mark taken before `clear()`. Rewinding the counter would
    make a completed write look like one that never happened."""
    monitor, page = _attached()
    page.fire("request", _Req("POST"))
    page.fire("response", _Resp(200, _Req("POST")))
    page.fire("request", _Req("POST"))

    monitor.clear()

    assert monitor.mutations_in_flight() == 0
    assert monitor.mutation_responses == 1


def test_a_read_response_does_not_advance_the_write_counter():
    monitor, page = _attached()
    page.fire("response", _Resp(200, _Req("GET", "https://app.test/api/list")))

    assert monitor.mutation_responses == 0


# ===========================================================================
# B -- the settle waits for the answer
# ===========================================================================


def test_the_old_behaviour_is_unchanged_when_not_a_submit():
    """Every non-submit action must cost exactly what it cost before."""
    monitor, _page = _attached()
    fake = _FakePage()
    _run(_executor(monitor)._wait_settled(fake))

    assert fake.waited_ms == 400
    assert fake.load_states == ["domcontentloaded"]


def test_a_submit_that_starts_no_write_does_not_stall():
    """Client-side validation can block the submit entirely, and plenty of forms
    do not use XHR. Neither may pay the completion budget."""
    monitor, _page = _attached()
    fake = _FakePage()
    _run(_executor(monitor)._wait_settled(fake, await_write=True))

    assert fake.waited_ms == 400 + SUBMIT_WRITE_START_TIMEOUT_MS
    assert fake.waited_ms < 400 + SUBMIT_WRITE_COMPLETE_TIMEOUT_MS


def test_a_submit_waits_for_an_in_flight_write_and_stops_when_it_lands():
    """The measured case: the write is outstanding when the settle ends, and the
    response arrives shortly after."""
    monitor, page = _attached()
    page.fire("request", _Req("POST"))

    def land_after_three_polls(fake: _FakePage) -> None:
        if len(fake.waits) == 4:  # the settle itself is the first wait
            page.fire("response", _Resp(200, _Req("POST")))

    fake = _FakePage(on_wait=land_after_three_polls)
    _run(_executor(monitor)._wait_settled(fake, await_write=True))

    assert monitor.mutations_in_flight() == 0
    # Settle, then the poll that observes the in-flight write is skipped (it is
    # already in flight), then polls until it lands. Far short of the budget.
    assert fake.waited_ms < 400 + SUBMIT_WRITE_COMPLETE_TIMEOUT_MS
    assert fake.waited_ms > 400


def test_a_write_answered_during_the_settle_still_counts():
    """The mark is taken BEFORE the settle. A fast application answers while the
    settle is running, leaving nothing in flight and nothing about to start — and
    a mark taken afterwards would conclude no write ever happened and return
    immediately, which is right by luck, not by reasoning."""
    monitor, page = _attached()

    def answer_during_settle(fake: _FakePage) -> None:
        if len(fake.waits) == 1:
            page.fire("request", _Req("POST"))
            page.fire("response", _Resp(201, _Req("POST")))

    fake = _FakePage(on_wait=answer_during_settle)
    _run(_executor(monitor)._wait_settled(fake, await_write=True))

    # Settle plus at most one poll: the counter has already moved past the mark,
    # so the start loop breaks immediately and nothing is in flight.
    assert fake.waited_ms <= 400 + 100
    assert monitor.mutation_responses == 1


def test_a_write_that_is_never_answered_gives_up_on_the_budget():
    monitor, page = _attached()
    page.fire("request", _Req("POST"))

    fake = _FakePage()
    _run(_executor(monitor)._wait_settled(fake, await_write=True))

    assert monitor.mutations_in_flight() == 1, "still outstanding"
    assert fake.waited_ms == 400 + SUBMIT_WRITE_COMPLETE_TIMEOUT_MS


def test_an_auth_submit_keeps_its_longer_settle_and_networkidle():
    """The auth path already leant on a fixed longer settle for this problem. It
    must keep it, and gain the write wait."""
    monitor, _page = _attached()
    fake = _FakePage()
    _run(_executor(monitor)._wait_settled(fake, extra_ms=1600, await_write=True))

    assert fake.waits[0] == 400 + 1600
    assert fake.load_states == ["networkidle"]


def test_no_monitor_means_no_wait_rather_than_a_crash():
    """Adapter-backed runs may have no network monitor at all."""
    fake = _FakePage()
    _run(_executor(None)._wait_settled(fake, await_write=True))

    assert fake.waited_ms == 400


# ===========================================================================
# C -- the executor actually asks for it on a data submit
# ===========================================================================


def _settle_kwargs_for(metadata: dict | None) -> dict:
    """Drive the real element-action path and capture how it settles.

    Element resolution and the click itself are stubbed — they need a browser —
    but the branch under test is the executor's own, and it is reached exactly as
    production reaches it.
    """
    from app.schemas import ActionType, BrowserAction

    class _Locator:
        async def scroll_into_view_if_needed(self):
            return None

    monitor, _page = _attached()
    executor = _executor(monitor)
    captured: dict = {}

    async def fake_resolve(page, action, page_state):
        return _Locator(), None

    async def fake_validate(locator, element, action):
        return None

    async def fake_perform(locator, action):
        return None

    async def fake_settle(page, **kwargs):
        captured.update(kwargs)

    executor._resolve_element = fake_resolve
    executor._validate_element = fake_validate
    executor._perform_element_action = fake_perform
    executor._wait_settled = fake_settle

    action = BrowserAction(
        action=ActionType.CLICK, element_id="el_1", metadata=metadata or {}
    )
    _run(
        executor._execute_via_page(
            page=_FakePage(),
            action=action,
            page_state=None,
            before_screenshot=None,
            after_screenshot=None,
            capture_evidence=False,
        )
    )
    assert captured, "the element-action path did not reach the settle at all"
    return captured


def test_a_data_submit_asks_for_the_write_wait():
    """The regression this closes: `form_workflow_submit` took the short
    non-submit settle while `auth_submit` took the long one — one rule, two
    behaviours, and only the auth half worked."""
    assert _settle_kwargs_for({"form_workflow_submit": True})["await_write"] is True


# ===========================================================================
# C2 -- the ADAPTER path, which is the one production takes
# ===========================================================================


class _Adapter:
    """The parts of a BrowserAdapter the element-action branch touches."""

    name = "fake_adapter"

    def __init__(self, monitor: NetworkMonitor | None = None, on_wait=None):
        self.waits: list[int] = []
        self.clicked = 0
        self.stable_calls = 0
        self._monitor = monitor
        self._on_wait = on_wait

    @property
    def waited_ms(self) -> int:
        return sum(self.waits)

    async def resolve_target(self, _target):
        return None

    async def click_target(self, _target):
        self.clicked += 1

    async def get_current_url(self) -> str:
        return "https://app.test/records"

    async def get_console_events(self):
        return []

    async def get_network_events(self):
        return []

    async def wait(self, ms: int) -> None:
        self.waits.append(ms)
        if self._on_wait is not None:
            self._on_wait(self)

    async def wait_for_page_stable(self) -> None:
        self.stable_calls += 1

    def capabilities(self):
        from app.browser.adapters.base import AdapterCapabilities

        return AdapterCapabilities()


async def _run_adapter_click(metadata: dict | None, adapter: _Adapter, monitor):
    from app.schemas import ActionType, BrowserAction

    executor = ActionExecutor("run", network_monitor=monitor, settle_ms=400)
    action = BrowserAction(
        action=ActionType.CLICK, element_id="el_1", metadata=metadata or {}
    )
    await executor._execute_via_adapter(
        adapter=adapter,
        action=action,
        page_state=None,
        before_screenshot=None,
        after_screenshot=None,
        capture_evidence=False,
    )


def test_c2_the_adapter_path_waits_for_the_write_too():
    """The bug that survived the first fix. Production runs
    `browser_adapter: direct_playwright`, and that branch settled with a plain
    `adapter.wait(settle_ms)` that knew nothing about submits — so the write wait
    was wired into the path production does NOT take.

    Measured on run 5b4268e0: an Add Candidate submit executed in 1091ms, far too
    short to have waited for anything, and the create was recorded
    `submitted_unverified` all over again.
    """
    monitor, page = _attached()
    page.fire("request", _Req("POST"))

    def land_late(adapter: _Adapter) -> None:
        if len(adapter.waits) == 4:
            page.fire("response", _Resp(200, _Req("POST")))

    adapter = _Adapter(on_wait=land_late)
    _run(_run_adapter_click({"form_workflow_submit": True}, adapter, monitor))

    assert adapter.clicked == 1
    assert monitor.mutations_in_flight() == 0, "it waited for the answer"
    assert adapter.waited_ms > 400, "more than the bare settle"

    # And the entry is now in the window the submission classifier reads, which is
    # the whole point of waiting.
    from app.agent.submission_outcome import _succeeded_mutations
    from app.schemas import PageState

    state = PageState(page_id="p", url="https://app.test/records", title="x",
                      network_entries=monitor.snapshot_entries())
    assert _succeeded_mutations(state) == [(200, SAVE_URL)]


def test_c2_an_ordinary_adapter_click_still_costs_only_the_settle():
    monitor, _page = _attached()
    adapter = _Adapter()
    _run(_run_adapter_click({}, adapter, monitor))

    assert adapter.waits == [400]
    assert adapter.stable_calls == 1


def test_c2_an_adapter_auth_submit_keeps_its_longer_settle():
    monitor, _page = _attached()
    adapter = _Adapter()
    _run(_run_adapter_click({"auth_submit": True}, adapter, monitor))

    assert adapter.waits[0] == 400 + 1600


def test_c2_an_adapter_submit_that_starts_no_write_does_not_stall():
    monitor, _page = _attached()
    adapter = _Adapter()
    _run(_run_adapter_click({"form_workflow_submit": True}, adapter, monitor))

    assert adapter.waited_ms == 400 + SUBMIT_WRITE_START_TIMEOUT_MS


def test_c2_both_paths_decide_what_a_submit_is_the_same_way():
    """One rule, one home. `_submit_write_mark` is the only place that answers
    "is this a submission?", and both execution paths call it."""
    import inspect

    for method in (ActionExecutor._execute_via_adapter, ActionExecutor._execute_via_page):
        source = inspect.getsource(method)
        assert "_submit_write_mark" in source, method.__name__


def test_an_auth_submit_asks_for_it_too_and_keeps_its_extra_settle():
    kwargs = _settle_kwargs_for({"auth_submit": True})
    assert kwargs["await_write"] is True
    assert kwargs["extra_ms"] == 1600


def test_an_ordinary_click_does_not_ask_for_it():
    kwargs = _settle_kwargs_for(None)
    assert kwargs["await_write"] is False
    assert kwargs["extra_ms"] == 0


def test_a_non_submit_form_write_does_not_ask_for_it():
    """Filling a field is a write to the FORM, not a submission. Waiting for a
    network answer after every keystroke would cost the start budget each time."""
    kwargs = _settle_kwargs_for({"form_workflow_write": True})
    assert kwargs["await_write"] is False


def test_the_start_budget_is_shorter_than_the_completion_budget():
    """Deciding "no write is coming" must be cheap; waiting for one that is
    already in flight can afford to be patient."""
    assert SUBMIT_WRITE_START_TIMEOUT_MS < SUBMIT_WRITE_COMPLETE_TIMEOUT_MS


def test_the_inflight_cutoff_outlasts_the_completion_budget():
    """If a write could age out mid-wait, the loop would exit calling it answered
    when it never was."""
    assert INFLIGHT_MAX_AGE_S * 1000 > SUBMIT_WRITE_COMPLETE_TIMEOUT_MS


# ===========================================================================
# D -- the whole point, end to end through the classifier
# ===========================================================================


def test_a_save_that_lands_after_the_old_settle_now_classifies_accepted():
    """The OrangeHRM case reproduced without a browser: the write is in flight
    when the settle ends, so the old path observed nothing and the new path waits
    for the 200 that proves acceptance."""
    from app.agent.submission_outcome import SUBMISSION_ACCEPTED, classify_submission
    from app.schemas import FormDescriptor, PageState

    monitor, page = _attached()
    page.fire("request", _Req("POST"))

    def _state() -> PageState:
        return PageState(page_id="p", url="https://app.test/records", title="x",
                         forms=[FormDescriptor(form_id="form_1")],
                         network_entries=monitor.snapshot_entries())

    before = _state()

    def land_late(fake: _FakePage) -> None:
        if len(fake.waits) == 3:
            page.fire("response", _Resp(200, _Req("POST")))

    fake = _FakePage(on_wait=land_late)
    _run(_executor(monitor)._wait_settled(fake, await_write=True))

    after = _state()
    outcome = classify_submission(before_state=before, after_state=after,
                                  form_id="form_1", fields=[], click_succeeded=True)

    assert outcome.outcome == SUBMISSION_ACCEPTED
    assert outcome.signals == ["mutating_request_succeeded_200"]
    assert outcome.http_status == 200


def test_without_the_wait_the_same_save_is_unknown():
    """The contrast that makes the test above mean something.

    The form stays on screen in both states, as OrangeHRM's post box does — so
    there is no DOM side-effect to fall back on and the network entry is the only
    evidence there could be.
    """
    from app.agent.submission_outcome import SUBMISSION_UNKNOWN, classify_submission
    from app.schemas import FormDescriptor, PageState

    monitor, page = _attached()
    page.fire("request", _Req("POST"))

    def _state() -> PageState:
        return PageState(page_id="p", url="https://app.test/records", title="x",
                         forms=[FormDescriptor(form_id="form_1")],
                         network_entries=monitor.snapshot_entries())

    before = _state()
    fake = _FakePage()
    _run(_executor(monitor)._wait_settled(fake))  # old path: no write wait
    after = _state()

    outcome = classify_submission(before_state=before, after_state=after,
                                  form_id="form_1", fields=[], click_succeeded=True)

    assert outcome.outcome == SUBMISSION_UNKNOWN
    assert "still_on_form_without_visible_outcome" in outcome.signals


def test_a_rejected_save_is_still_reported_as_rejected():
    """The wait must not turn refusals into acceptances: it waits for an answer,
    it does not assume what the answer says."""
    from app.agent.submission_outcome import SUBMISSION_REJECTED_VALIDATION, classify_submission
    from app.schemas import PageState

    monitor, page = _attached()
    page.fire("request", _Req("POST"))
    before = PageState(page_id="p", url="https://app.test/records", title="x",
                       network_entries=monitor.snapshot_entries())

    def reject_late(fake: _FakePage) -> None:
        if len(fake.waits) == 3:
            page.fire("response", _Resp(422, _Req("POST")))

    fake = _FakePage(on_wait=reject_late)
    _run(_executor(monitor)._wait_settled(fake, await_write=True))

    after = PageState(page_id="p", url="https://app.test/records", title="x",
                      network_entries=monitor.snapshot_entries())
    outcome = classify_submission(before_state=before, after_state=after,
                                  form_id=None, fields=[], click_succeeded=True)

    assert outcome.outcome == SUBMISSION_REJECTED_VALIDATION
    assert outcome.http_status == 422


def test_waiting_does_not_start_the_clock_from_real_time():
    """Guard against a future rewrite that sleeps for real: the whole suite here
    must stay fast, and a test that passes by waiting 8 seconds is a test that
    will be deleted."""
    monitor, page = _attached()
    page.fire("request", _Req("POST"))
    fake = _FakePage()

    started = time.monotonic()
    _run(_executor(monitor)._wait_settled(fake, await_write=True))
    elapsed_ms = (time.monotonic() - started) * 1000

    assert fake.waited_ms == 400 + SUBMIT_WRITE_COMPLETE_TIMEOUT_MS
    assert elapsed_ms < 1000, "the production code asked to wait; the fake did not"
