"""Closed-vocabulary "why wasn't this executed" classification for reporting.

Translates the intelligence pipeline's own internal vocabularies —
`app.intelligence.autonomous_investigation.eligibility_classifier.
ELIGIBILITY_STATUSES` and `...schemas.STOP_REASONS` — into the flat reason
set a report reader actually needs. Reporting-only: this module never
decides whether something CAN execute (that's `eligibility_classifier`),
only how to LABEL why it didn't, given the classification already made
elsewhere. "Do not report only `not_run`" is enforced by every call site
always returning one of `BLOCKED_REASONS`, never `None`/empty.
"""

from __future__ import annotations

BLOCKED_REASONS = frozenset(
    {
        "autonomous_mode_disabled",
        "provider_incapable",
        "controlled_writes_disabled",
        "unsafe",
        "missing_actor",
        "missing_data",
        "unsupported_control",
        "missing_verification",
        "dependency_unresolved",
        "stale",
        "duplicate",
        "planner_unavailable",
        "budget_exhausted",
        "other",
    }
)

# app.intelligence.autonomous_investigation.eligibility_classifier.ELIGIBILITY_STATUSES
# that mean "not blocked" -- excluded from blocked/unexecuted reporting entirely
# unless the scenario is STILL unexecuted despite being eligible (handled by the
# stop-reason fallback below).
_EXECUTABLE_ELIGIBILITY_STATUSES = frozenset(
    {"executable", "executable_with_controlled_writes", "read_only_executable"}
)

_ELIGIBILITY_TO_REASON = {
    "blocked_by_missing_actor": "missing_actor",
    "blocked_by_missing_state": "dependency_unresolved",
    "blocked_by_missing_test_data": "missing_data",
    "blocked_by_unsupported_control": "unsupported_control",
    "blocked_by_missing_verification": "missing_verification",
    "blocked_by_safety_policy": "unsafe",
    "blocked_by_configuration": "controlled_writes_disabled",
    "duplicate": "duplicate",
    "stale": "stale",
}

# app.intelligence.autonomous_investigation.schemas.STOP_REASONS -- used as a
# fallback ONLY for a scenario that WAS eligible/executable but never got a
# turn before the run stopped (never a substitute for a real eligibility
# classification when one exists).
_STOP_REASON_TO_REASON = {
    "budget_exceeded": "budget_exhausted",
    "max_actions_reached": "budget_exhausted",
    "time_exceeded": "budget_exhausted",
    "planner_unavailable": "planner_unavailable",
    "provider_unavailable": "provider_incapable",
    "safety_violation": "unsafe",
}


def classify_scenario_blocked_reason(
    *,
    eligibility_status: str | None,
    autonomous_investigation_enabled: bool,
    provider_capable: bool,
    stop_reason: str | None = None,
) -> str:
    """Reason a Scenario Planning scenario has not reached a terminal
    execution outcome. Precedence: a run-level condition that blocks EVERY
    scenario uniformly (autonomous mode off, provider incapable) is checked
    first, since it explains the whole run, not just this one scenario;
    then the scenario's own eligibility classification; then the run's stop
    reason as a last-resort attribution for an eligible-but-never-reached
    scenario; `"other"` only when none of the above apply."""
    if not autonomous_investigation_enabled:
        return "autonomous_mode_disabled"
    if not provider_capable:
        return "provider_incapable"
    if eligibility_status and eligibility_status not in _EXECUTABLE_ELIGIBILITY_STATUSES:
        mapped = _ELIGIBILITY_TO_REASON.get(eligibility_status)
        if mapped:
            return mapped
    if stop_reason:
        mapped = _STOP_REASON_TO_REASON.get(stop_reason)
        if mapped:
            return mapped
    return "other"


def classify_legacy_scenario_reason(*, budget_exhausted: bool, stop_reason: str | None = None) -> str:
    """Reason a legacy `app.agent.tester.Tester`-generated `TestScenario`
    stayed `not_run` — this generator has no eligibility classification of
    its own (see `docs/QA_REPORTING_UPGRADE.md`), so attribution is coarser
    and intentionally says so via `"other"` rather than fabricating false
    precision."""
    if budget_exhausted:
        return "budget_exhausted"
    if stop_reason:
        mapped = _STOP_REASON_TO_REASON.get(stop_reason)
        if mapped:
            return mapped
    return "other"
