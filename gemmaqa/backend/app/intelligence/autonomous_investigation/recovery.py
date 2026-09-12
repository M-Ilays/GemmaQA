"""Classifies a failed `ActionResult` into one of the named failure
classes and decides a bounded recovery action. Retries are always capped
(never infinite) and only ever attempted for failure classes that are
plausibly transient -- a session expiry or a genuinely missing element is
never blindly retried, it ends the scenario attempt instead.
"""

from __future__ import annotations

from app.intelligence.autonomous_investigation.schemas import FAILURE_CLASSES

_FAILURE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeout", ("timeout", "timed out")),
    ("missing_element", ("unknown element_id", "not visible", "is disabled", "could not resolve")),
    ("navigation_failure", ("net::err", "navigation", "failed to open", "err_")),
    ("unexpected_dialog", ("dialog", "beforeunload")),
    ("session_expiry", ("session", "unauthorized", "401", "login required")),
    ("api_failure", ("500", "502", "503", "api error", "server error")),
    ("network_interruption", ("network", "connection", "econnrefused", "econnreset")),
    ("stale_dom", ("stale", "detached", "element is not attached")),
    ("capability_unavailable", ("capability_unavailable",)),
)

# Transient by nature -- worth a bounded retry.
_RETRYABLE = frozenset({"timeout", "network_interruption", "stale_dom", "unexpected_dialog", "navigation_failure"})


def classify_failure(result) -> str:
    if result is None or result.success:
        return "unknown"
    text = f"{result.error or ''} {result.message or ''}".lower()
    for failure_class, patterns in _FAILURE_PATTERNS:
        if any(p in text for p in patterns):
            assert failure_class in FAILURE_CLASSES
            return failure_class
    return "unknown"


def decide_recovery(failure_class: str, *, attempt: int, max_retries: int = 2) -> str:
    """`attempt` is how many times THIS step has already failed. Returns one
    of RECOVERY_ACTIONS -- never retries past `max_retries`."""
    if attempt >= max_retries:
        return "abort_scenario"
    if failure_class in _RETRYABLE:
        return "retry_with_backoff" if attempt > 0 else "retry"
    if failure_class == "session_expiry":
        # Re-authentication is the normal exploration loop's job (the same
        # AuthenticationStrategy every other action already uses) -- an
        # investigation step never re-implements login, it just yields.
        return "abort_scenario"
    if failure_class in {"missing_element", "capability_unavailable"}:
        return "skip_step"
    return "skip_step"
