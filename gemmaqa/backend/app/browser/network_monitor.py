"""Network monitor with sensitive-data scrubbing.

Records two things, and the distinction matters downstream:

* `failures` — 4xx/5xx responses and transport failures. Reported as problems.
* `entries`  — the same failures PLUS successful state-changing requests, which
  are evidence that a submission was accepted rather than a problem.

Successful mutations were not recorded at all until it was measured that
`submission_outcome._succeeded_mutations` could therefore never match anything:
it looks for `failed=False` with a 2xx status, and this monitor only ever
produced `failed=True`.

It also tracks which mutating requests are STILL IN FLIGHT, because recording
them was not enough on its own: the observation that feeds the submission
classifier was being built before the application had answered, so the entry it
needed did not exist yet. See `ActionExecutor._await_submitted_write`.
"""

from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import Page, Request, Response

from app.schemas import NetworkEntry
from app.utils.logging import get_logger

logger = get_logger("browser.network")

# HTTP methods that change server state. Defined here, in the layer that sees the
# requests, and imported by `app.agent.submission_outcome` — one definition, so
# the recorder and the classifier cannot disagree about what counts as a write.
MUTATING_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# A request neither answered nor failed for this long is not "in flight" in any
# useful sense — it was aborted at page teardown, or its terminal event was lost.
# Without an upper bound a single such request would make every later submit wait
# out its whole budget.
INFLIGHT_MAX_AGE_S = 30.0

SENSITIVE_QUERY_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "apikey",
    "key",
    "secret",
    "password",
    "passwd",
    "pwd",
    "auth",
    "authorization",
    "session",
    "cookie",
    "jwt",
    "code",
    "client_secret",
}


def sanitize_network_url(url: str) -> str:
    """Strip sensitive query params; never keep credentials in netloc."""
    try:
        parts = urlsplit(url)
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        query_pairs = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            lower = key.lower()
            if lower in SENSITIVE_QUERY_KEYS or any(
                s in lower for s in ("token", "secret", "password", "auth")
            ):
                query_pairs.append((key, "REDACTED"))
            else:
                query_pairs.append((key, value))
        query = urlencode(query_pairs)
        return urlunsplit((parts.scheme, netloc, parts.path, query, ""))
    except Exception:
        return url.split("?")[0]


class NetworkMonitor:
    """Track failed/4xx/5xx HTTP requests during exploration (safe metadata only)."""

    def __init__(self, max_entries: int = 200) -> None:
        self.max_entries = max_entries
        self.entries: list[NetworkEntry] = []
        self.failures: list[str] = []
        self._attached_pages: set[int] = set()
        self._request_start: dict[str, float] = {}
        # Mutating requests that have been sent but not yet answered, as
        # (monotonic start, url). A list rather than a set because the same
        # endpoint can legitimately be in flight twice.
        self._inflight_mutations: list[tuple[float, str]] = []
        # Monotonically increasing count of mutating requests that have reached a
        # terminal state. A caller marks it, acts, and compares — which is how it
        # can tell "a write completed while I was waiting" from "there was never
        # a write", without needing to identify the specific request.
        self.mutation_responses = 0

    def attach(self, page: Page) -> None:
        page_id = id(page)
        if page_id in self._attached_pages:
            return

        def on_request(request: Request) -> None:
            try:
                self._request_start[request.url] = datetime.utcnow().timestamp()
                if request.method.upper() in MUTATING_HTTP_METHODS:
                    self._inflight_mutations.append((time.monotonic(), request.url))
            except Exception:
                pass

        def on_response(response: Response) -> None:
            try:
                status = response.status
                req = response.request
                self._settle_mutation(req)
                if status < 400:
                    # A SUCCESSFUL state-changing request is the application
                    # saying it accepted the data, and it is the strongest
                    # evidence a submission was accepted — stronger than any DOM
                    # side-effect. Everything under 400 used to be discarded
                    # here, which made `submission_outcome._succeeded_mutations`
                    # unsatisfiable: it looks for `failed=False` with a 2xx
                    # status, and this monitor could only ever produce
                    # `failed=True`. Measured on OrangeHRM's Buzz save:
                    # `POST 200 /api/v2/buzz/posts` happened, 0 entries were
                    # recorded, and the create was classified `unknown`.
                    #
                    # Only mutating methods are kept. Recording successful GETs
                    # would flood a 200-entry buffer on an SPA and evict the one
                    # entry that matters, and nothing reads them.
                    if req.method.upper() not in MUTATING_HTTP_METHODS:
                        return
                    self._store(
                        NetworkEntry(
                            method=req.method,
                            url=sanitize_network_url(req.url),
                            status=status,
                            resource_type=req.resource_type,
                            failed=False,
                        ),
                        # NOT a failure: `network_failures` must keep listing
                        # only things that went wrong, or every accepted save
                        # would surface in reports as a network problem.
                        is_failure=False,
                    )
                    return
                started = self._request_start.pop(req.url, None)
                timing_ms = None
                if started is not None:
                    timing_ms = (datetime.utcnow().timestamp() - started) * 1000.0
                entry = NetworkEntry(
                    method=req.method,
                    url=sanitize_network_url(req.url),
                    status=status,
                    resource_type=req.resource_type,
                    failed=True,
                    timing_ms=timing_ms,
                )
                self._store(entry)
            except Exception as exc:
                logger.debug("network on_response error: %s", exc)

        def on_request_failed(request: Request) -> None:
            try:
                self._settle_mutation(request)
                started = self._request_start.pop(request.url, None)
                timing_ms = None
                if started is not None:
                    timing_ms = (datetime.utcnow().timestamp() - started) * 1000.0
                entry = NetworkEntry(
                    method=request.method,
                    url=sanitize_network_url(request.url),
                    status=None,
                    resource_type=request.resource_type,
                    failed=True,
                    failure_text=(request.failure or "failed")[:200],
                    timing_ms=timing_ms,
                )
                self._store(entry)
            except Exception as exc:
                logger.debug("network on_request_failed error: %s", exc)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        self._attached_pages.add(page_id)

    def _settle_mutation(self, request: Request) -> None:
        """A mutating request reached a terminal state: answered, or failed."""
        try:
            if request.method.upper() not in MUTATING_HTTP_METHODS:
                return
        except Exception:
            return
        url = getattr(request, "url", None)
        for index, (_started, pending_url) in enumerate(self._inflight_mutations):
            if pending_url == url:
                self._inflight_mutations.pop(index)
                break
        self.mutation_responses += 1

    def mutations_in_flight(self) -> int:
        """How many state-changing requests are still waiting to be answered.

        Requests older than `INFLIGHT_MAX_AGE_S` are dropped rather than counted:
        a lost terminal event must not make every later submit wait for a request
        that will never land.
        """
        cutoff = time.monotonic() - INFLIGHT_MAX_AGE_S
        self._inflight_mutations = [p for p in self._inflight_mutations if p[0] >= cutoff]
        return len(self._inflight_mutations)

    def _store(self, entry: NetworkEntry, *, is_failure: bool = True) -> None:
        self.entries.append(entry)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries :]
        if not is_failure:
            # `entries` carries evidence; `failures` carries problems. A accepted
            # save belongs in the first and not the second.
            logger.debug("Mutating request accepted: %s %s %s", entry.status, entry.method, entry.url)
            return
        label = f"{entry.status or 'FAIL'} {entry.method} {entry.url}"
        if entry.failure_text:
            label = f"{label} ({entry.failure_text})"
        self.failures.append(label)
        if len(self.failures) > self.max_entries:
            self.failures = self.failures[-self.max_entries :]
        logger.debug("Network issue: %s", label[:200])

    def snapshot(self) -> list[str]:
        return list(self.failures[-20:])

    def snapshot_entries(self) -> list[NetworkEntry]:
        return list(self.entries[-20:])

    def errors_since(self, index: int) -> list[str]:
        return list(self.failures[index:])

    def mark(self) -> int:
        return len(self.failures)

    def clear(self) -> None:
        self.entries.clear()
        self.failures.clear()
        self._request_start.clear()
        self._inflight_mutations.clear()
        # `mutation_responses` is NOT reset: callers hold marks taken before the
        # clear, and rewinding the counter would make a completed write look like
        # one that never happened.
