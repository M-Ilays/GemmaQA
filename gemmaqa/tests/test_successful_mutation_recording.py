"""A successful save must be recordable, or acceptance can never be proven.

`submission_outcome._succeeded_mutations` looks for a network entry with
`failed=False` and a 2xx status — a POST/PUT/PATCH the application answered
successfully, which is the strongest possible evidence a submission was accepted.

`NetworkMonitor.on_response` returned early on any status below 400, so every
entry it produced had `failed=True`. **The gate could never fire.** Measured on
OrangeHRM's Buzz save:

    POST 200 /api/v2/buzz/posts     <- actually happened (raw Playwright listener)
    0 monitor entries recorded
    66 raw responses in the session -> 3 monitor entries, all failures

So every create that re-rendered its form in place was classified `unknown`,
`note_result` set `submitted_unverified`, no record was registered, and the
Temporary Record Registry had nothing to open, update or delete.

The fix records successful MUTATING requests only. Successful GETs are still
discarded: an SPA fires dozens per page and they would evict the one entry that
matters from the buffer.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.submission_outcome import (  # noqa: E402
    SUBMISSION_ACCEPTED,
    _failed_mutations,
    _succeeded_mutations,
)
from app.browser.network_monitor import MUTATING_HTTP_METHODS, NetworkMonitor  # noqa: E402
from app.schemas import NetworkEntry, PageState  # noqa: E402

SAVE_URL = "https://app.test/api/v2/buzz/posts"


class _Req:
    def __init__(self, method: str, url: str, resource_type: str = "xhr"):
        self.method = method
        self.url = url
        self.resource_type = resource_type
        self.failure = None


class _Resp:
    def __init__(self, status: int, request: _Req):
        self.status = status
        self.request = request


class _Page:
    """Captures the handlers `NetworkMonitor.attach` registers."""

    def __init__(self):
        self.handlers: dict[str, list] = {}

    def on(self, event: str, handler):
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event: str, payload):
        for handler in self.handlers.get(event, []):
            handler(payload)


def _attached() -> tuple[NetworkMonitor, _Page]:
    monitor, page = NetworkMonitor(), _Page()
    monitor.attach(page)
    return monitor, page


def _state(monitor: NetworkMonitor) -> PageState:
    """A PageState carrying exactly what the observer would hand downstream."""
    return PageState(page_id="p", url="https://app.test/x", title="x",
                     network_entries=monitor.snapshot_entries())


# ===========================================================================
# A -- the recording gap itself
# ===========================================================================


def test_a_successful_save_is_recorded():
    monitor, page = _attached()
    page.fire("response", _Resp(200, _Req("POST", SAVE_URL)))

    assert len(monitor.entries) == 1
    entry = monitor.entries[0]
    assert entry.failed is False
    assert entry.status == 200
    assert entry.method == "POST"


def test_the_classifier_can_now_see_acceptance():
    """The assertion that would have failed before: end to end from the response
    event to the classifier that reads it."""
    monitor, page = _attached()
    page.fire("response", _Resp(200, _Req("POST", SAVE_URL)))

    assert _succeeded_mutations(_state(monitor)) == [(200, SAVE_URL)]


def test_every_mutating_method_counts():
    for method in sorted(MUTATING_HTTP_METHODS):
        monitor, page = _attached()
        page.fire("response", _Resp(201, _Req(method, SAVE_URL)))
        assert len(monitor.entries) == 1, method


def test_successful_reads_are_still_discarded():
    """An SPA fires dozens of GETs per page. Recording them would evict the one
    entry that matters from a bounded buffer."""
    monitor, page = _attached()
    for i in range(50):
        page.fire("response", _Resp(200, _Req("GET", f"https://app.test/api/list/{i}")))

    assert monitor.entries == []


def test_a_redirect_is_not_counted_as_an_accepted_save():
    """A login POST answers 302. It is recorded, but `_succeeded_mutations`
    requires 2xx, so it must not read as an accepted data write."""
    monitor, page = _attached()
    page.fire("response", _Resp(302, _Req("POST", "https://app.test/auth/validate")))

    assert monitor.entries[0].failed is False
    assert _succeeded_mutations(_state(monitor)) == []


# ===========================================================================
# B -- failure reporting is unchanged
# ===========================================================================


def test_an_accepted_save_is_not_reported_as_a_network_failure():
    """`network_failures` drives report content. Every accepted save appearing
    there would read as a network problem on a healthy application."""
    monitor, page = _attached()
    page.fire("response", _Resp(200, _Req("POST", SAVE_URL)))

    assert monitor.failures == []
    assert monitor.snapshot() == []


def test_server_and_client_errors_are_still_failures():
    for status in (400, 422, 500, 503):
        monitor, page = _attached()
        page.fire("response", _Resp(status, _Req("POST", SAVE_URL)))
        assert monitor.entries[0].failed is True, status
        assert len(monitor.failures) == 1, status


def test_a_failed_mutation_is_still_detected():
    monitor, page = _attached()
    page.fire("response", _Resp(422, _Req("POST", SAVE_URL)))

    assert _failed_mutations(_state(monitor)) == [(422, SAVE_URL)]


def test_a_transport_failure_is_still_recorded():
    monitor, page = _attached()
    page.fire("requestfailed", _Req("GET", "https://app.test/api/thing"))

    assert monitor.entries[0].failed is True
    assert len(monitor.failures) == 1


def test_a_success_and_a_failure_coexist_correctly():
    monitor, page = _attached()
    page.fire("response", _Resp(200, _Req("POST", SAVE_URL)))
    page.fire("response", _Resp(500, _Req("POST", "https://app.test/api/other")))

    assert len(monitor.entries) == 2
    assert len(monitor.failures) == 1, "only the 500 is a failure"
    assert _succeeded_mutations(_state(monitor)) == [(200, SAVE_URL)]
    assert _failed_mutations(_state(monitor)) == [(500, "https://app.test/api/other")]


# ===========================================================================
# C -- the whole point: the submission is classified accepted
# ===========================================================================


def test_a_form_that_stays_on_screen_is_still_classified_accepted():
    """The case that produced `submitted_unverified`: the application saves and
    re-renders the same form, so there is no URL change and no vanished form —
    no UI side-effect at all. The 2xx POST is the only evidence, and it is now
    visible."""
    from app.agent.submission_outcome import classify_submission

    monitor, page = _attached()
    page.fire("response", _Resp(200, _Req("POST", SAVE_URL)))
    after = _state(monitor)
    before = PageState(page_id="p", url=after.url, title="x")

    outcome = classify_submission(
        before_state=before, after_state=after, form_id=None,
        fields=[], click_succeeded=True,
    )

    assert outcome.outcome == SUBMISSION_ACCEPTED
    assert any("mutating_request_succeeded" in s for s in outcome.signals)


def test_the_mutating_method_set_has_one_definition():
    """The recorder and the classifier must agree on what a write is. They now
    share the frozenset rather than each declaring their own."""
    from app.agent.submission_outcome import _MUTATING_METHODS

    assert _MUTATING_METHODS is MUTATING_HTTP_METHODS


def test_entries_carry_evidence_while_failures_carry_problems():
    """The invariant the fix rests on, stated once: a success may enter
    `entries` but never `failures`."""
    monitor, page = _attached()
    page.fire("response", _Resp(200, _Req("PUT", SAVE_URL)))
    page.fire("response", _Resp(404, _Req("GET", "https://app.test/missing")))

    assert [e.failed for e in monitor.entries] == [False, True]
    assert len(monitor.failures) == 1
