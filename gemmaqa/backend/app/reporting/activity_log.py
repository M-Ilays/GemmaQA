"""The durable, human-readable record of what a run actually did, second by second.

Three things already described a run and none of them answered "what is it doing
right now, and what took so long?":

  * `GEMMAQA_EXPLORATION_TRACE` produces excellent structured decision data --
    to stdout, only when an environment variable is set, and never anywhere the
    operator looks.
  * The WebSocket timeline lives in browser memory, caps at 250 events, renders
    truncated raw JSON, and is gone on refresh.
  * `memory.actions` and the final report describe the run only once it is over.

None of them recorded DURATION, so "GemmaQA hung" and "GemmaQA spent nine
seconds waiting for a slow page" were indistinguishable.

This module is the missing piece: one append-only JSONL file per run, written
through `AgentController.emit` (already the single funnel every event passes
through, so capture costs no new call sites), with an elapsed clock, a phase, a
one-line human summary, and the sanitized structured detail behind it.

Sanitization is NOT re-implemented here -- `app.utils.sanitization.sanitize_dict`
is the project's existing boundary for that and stays the only one.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.browser.pacing import pacing_activity_line
from app.reporting.live_action import format_live_action_line
from app.utils.logging import get_logger
from app.utils.sanitization import sanitize_dict

logger = get_logger("reporting.activity")

# A run can emit thousands of records; a reader asking for "the log" must not be
# handed an unbounded response. Writing stays unbounded (the file is the
# archive) -- only reads are paged.
DEFAULT_READ_LIMIT = 500
MAX_READ_LIMIT = 5000

# Beyond this a single record is detail nobody reads inline and payload the
# WebSocket has to carry. The full object is already in the run's other
# artifacts (report JSON, evidence, model-context events).
MAX_DETAIL_CHARS = 4000

ACTIVITY_LOG_FILENAME = "activity.jsonl"


# ---------------------------------------------------------------------------
# Phases -- which part of the loop an event belongs to
# ---------------------------------------------------------------------------

# Deliberately coarse and closed. The value of a phase is that an operator can
# see "it spent 40% of the run observing" at a glance; a phase per module would
# answer nothing.
PHASES = frozenset(
    {
        "startup",
        "observation",
        "understanding",
        "planning",
        "execution",
        "verification",
        "intelligence",
        "cleanup",
        "reporting",
        "lifecycle",
    }
)

_EVENT_PHASES: dict[str, str] = {
    "run_started": "startup",
    "browser_opened": "startup",
    "phase_changed": "lifecycle",
    "state_changed": "lifecycle",
    "state_compared": "verification",
    "authentication_evaluated": "verification",
    "action_started": "execution",
    "action_finished": "execution",
    "documentation_updated": "reporting",
    "page_observed": "observation",
    "navigated": "observation",
    "observation_reobserved": "observation",
    "understanding_assessed": "understanding",
    "action_planning": "planning",
    "action_planned": "planning",
    "safety_decision": "planning",
    "action_blocked": "planning",
    "form_abandoned": "verification",
    "ai_failure": "planning",
    "action_executed": "execution",
    "action_failed": "execution",
    "submission_classified": "verification",
    "bug_found": "verification",
    "tests_proposed": "intelligence",
    "record_lifecycle": "cleanup",
    "cleanup_step": "cleanup",
    "run_stopping": "reporting",
    "run_stopped": "reporting",
    "run_completed": "reporting",
    "run_failed": "reporting",
    "run_cancelled": "reporting",
    "run_paused": "lifecycle",
    "run_resumed": "lifecycle",
    "execution_pacing": "execution",
    "report_generated": "reporting",
}


def phase_for(event: str) -> str:
    return _EVENT_PHASES.get(event, "lifecycle")


# ---------------------------------------------------------------------------
# Human summaries -- the line an operator actually reads
# ---------------------------------------------------------------------------


def _s(payload: dict[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key)
    return default if value is None else str(value)


def summarize(event: str, payload: dict[str, Any]) -> str:
    """One plain sentence describing this event.

    The point of the whole feature: `{"action":"click","element_id":"el_008",...}`
    truncated at 160 characters is not something a person reads to follow a run.
    Unknown events fall back to their own name rather than being dropped, so a
    new event type degrades to "less readable" and never to "invisible".
    """
    p = payload or {}
    if event == "page_observed":
        elements = p.get("elements")
        where = _s(p, "title") or _s(p, "url", "a page")
        count = f" ({elements} interactive elements)" if elements is not None else ""
        return f"Observed {where}{count}"
    if event == "navigated":
        return f"Navigated to {_s(p, 'url', 'a new page')}"
    if event == "observation_reobserved":
        return (
            f"Observed again — the page was still {_s(p, 'state', 'settling')} "
            f"(pass {_s(p, 'observation_pass', '?')})"
        )
    if event == "understanding_assessed":
        return (
            f"Judged the page {_s(p, 'state', 'unknown')} "
            f"(readiness {_s(p, 'readiness', '?')}, decision {_s(p, 'decision', '?')})"
        )
    if event == "action_planning":
        return "Deciding what to do next"
    if event == "action_planned":
        live = _s(p, "live_action_line")
        if live:
            return live
        target = _s(p, "element_id")
        on = f" on {target}" if target else ""
        return f"Chose to {_s(p, 'action', 'act')}{on} — {_s(p, 'reason', 'no reason given')}"
    if event == "action_executed":
        # Historical payloads (and existing tests) have no live_* keys. Keep the
        # original sentence so stored logs and the activity-log tests stay valid.
        # New emitters add live_action_line / action_label — those use the
        # operator-facing CLICK / FAILED lines.
        if p.get("success") is False and (p.get("live_action_line") or p.get("action_label")):
            return format_live_action_line(
                _s(p, "action"),
                _s(p, "action_label") or _s(p, "element_id") or "control",
                outcome="failed",
                reason=_s(p, "error") or None,
            )
        live = _s(p, "live_action_line")
        if live and p.get("success") is not False:
            return live
        outcome = "succeeded" if p.get("success") else "failed"
        target = _s(p, "element_id")
        on = f" on {target}" if target else ""
        return f"{_s(p, 'action', 'Action').capitalize()}{on} {outcome}"
    if event == "action_failed":
        return format_live_action_line(
            _s(p, "action"),
            _s(p, "action_label") or _s(p, "element_id") or "control",
            outcome="failed",
            reason=_s(p, "error") or _s(p, "reason") or None,
        )
    if event == "safety_decision":
        # Keys come from `SafetyAudit.to_payload()`: `safety_decision`,
        # `proposed_action`, `block_reason`. Guessing `allowed`/`action`/`reason`
        # made every passing safety check render as "BLOCKED", which is both
        # wrong and alarming — the summariser must read the payload the emitter
        # actually sends.
        what = _s(p, "proposed_action") or _s(p, "action", "the action")
        decision = _s(p, "safety_decision", "unknown")
        if decision == "allow":
            return f"Safety check passed for {what}"
        reason = _s(p, "block_reason") or _s(p, "reason") or "no reason given"
        verb = "BLOCKED" if decision == "block" else decision
        return f"Safety check {verb} {what} — {reason}"
    if event == "form_abandoned":
        refs = p.get("unsatisfied_references") or []
        missing = f" (needs a record for: {', '.join(str(r) for r in refs)})" if refs else ""
        return (
            f"Gave up on form {_s(p, 'form_id', '?')} after {_s(p, 'attempts', '?')} "
            f"attempt(s) — {_s(p, 'reason', 'no reason recorded')}{missing}"
        )
    if event == "action_blocked":
        return format_live_action_line(
            _s(p, "action"),
            _s(p, "action_label") or _s(p, "element_id") or _s(p, "action", "the action"),
            outcome="blocked",
            reason=_s(p, "reason") or _s(p, "block_reason") or None,
        )
    if event == "submission_classified":
        return f"The application {_s(p, 'outcome', 'responded')} to the submission of {_s(p, 'form_id', 'a form')}"
    if event == "bug_found":
        return f"Recorded a {_s(p, 'severity', 'possible')} issue: {_s(p, 'title', 'untitled')}"
    if event == "record_lifecycle":
        return f"Test record {_s(p, 'temporary_record_id')[:8]} is now {_s(p, 'state', 'unknown')}"
    if event == "cleanup_step":
        allowed = "allowed" if p.get("allowed", True) else f"refused ({_s(p, 'reason')})"
        return f"Cleanup step {_s(p, 'step', '?')} {allowed}"
    if event == "ai_failure":
        return f"The reasoning provider failed — {_s(p, 'reason', 'no detail')}"
    if event == "phase_changed":
        return f"Phase: {_s(p, 'phase', 'unknown')} — {_s(p, 'message', '')}".rstrip(" —")
    if event == "state_changed":
        # `state`, not `status` — reading the wrong key made the single most
        # frequent event in a run (300 of 1151 records on the first live capture)
        # render as "Run state: unknown" three hundred times.
        return f"{_s(p, 'message') or _s(p, 'state', 'State changed')}"
    if event == "action_started":
        live = _s(p, "live_action_line")
        if live:
            return live
        return format_live_action_line(
            _s(p, "action"),
            _s(p, "action_label") or _s(p, "element_id") or "control",
        )
    if event == "action_finished":
        return f"Finished {_s(p, 'action', 'the action')}"
    if event == "run_started":
        return f"Started testing {_s(p, 'url', 'the target')} with the {_s(p, 'provider', 'configured')} provider"
    if event == "browser_opened":
        mode = "headless" if p.get("headless") else "headed"
        return f"Opened a {mode} browser via {_s(p, 'adapter', 'the configured adapter')}"
    if event == "authentication_evaluated":
        return f"Authentication: {_s(p, 'auth_status', 'evaluated')}"
    if event == "state_compared":
        return "Compared the page against the state before the action"
    if event == "documentation_updated":
        return "Updated the run documentation"
    if event in {"run_stopping", "run_stopped"}:
        return f"Stopping — {_s(p, 'reason', 'no reason recorded')}"
    if event == "run_completed":
        return f"Run complete — {_s(p, 'message', '')}".rstrip(" —")
    if event == "run_failed":
        return f"Run failed — {_s(p, 'error', 'no detail')}"
    if event == "run_cancelled":
        return "Run cancelled"
    if event == "run_paused":
        return "Run paused — time budget frozen until Continue"
    if event == "run_resumed":
        return "Run continued"
    if event == "execution_pacing":
        return pacing_activity_line(
            float(p.get("execution_speed") or 1),
            float(p.get("action_pause") or 0),
        )
    if event == "tests_proposed":
        return f"Proposed {_s(p, 'count', 'some')} test scenario(s) for this page"
    if event == "phase_timing":
        return f"{_s(p, 'phase', 'A phase').capitalize()} took {_s(p, 'duration_ms', '?')} ms"
    return event.replace("_", " ")


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class ActivityLog:
    """Append-only JSONL activity log for one run.

    Every write is best-effort: an activity log that can abort a QA run is worse
    than no activity log, so all IO failures are swallowed after one warning.
    Thread-locked because the controller and its engines are async but the file
    handle is not.
    """

    def __init__(self, run_id: str, *, root: Path | None = None) -> None:
        self.run_id = run_id
        if root is None:
            from app.config import get_settings

            root = get_settings().run_evidence_dir(run_id)
        self.root = Path(root)
        self.path = self.root / ACTIVITY_LOG_FILENAME
        self._lock = threading.Lock()
        self._seq = 0
        self._started_monotonic = time.monotonic()
        self._warned = False
        self._phase_totals: dict[str, float] = {}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception:  # pragma: no cover - defensive
            self._warn("could not create the run evidence directory")

    # -- writing ------------------------------------------------------------

    def record(
        self,
        event: str,
        payload: dict[str, Any] | None = None,
        *,
        phase: str | None = None,
        duration_ms: float | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        """Append one record and return it (so callers can also ship it live)."""
        detail = sanitize_dict(dict(payload or {}))
        encoded = json.dumps(detail, default=str)
        if len(encoded) > MAX_DETAIL_CHARS:
            detail = {
                "truncated": True,
                "original_chars": len(encoded),
                "preview": encoded[:MAX_DETAIL_CHARS],
            }

        resolved_phase = phase if phase in PHASES else phase_for(event)
        with self._lock:
            self._seq += 1
            record = {
                "seq": self._seq,
                "run_id": self.run_id,
                "at": (at or datetime.now(timezone.utc)).isoformat(),
                "elapsed_ms": int((time.monotonic() - self._started_monotonic) * 1000),
                "phase": resolved_phase,
                "event": event,
                "summary": summarize(event, payload or {}),
                "detail": detail,
            }
            if duration_ms is not None:
                record["duration_ms"] = int(duration_ms)
                self._phase_totals[resolved_phase] = (
                    self._phase_totals.get(resolved_phase, 0.0) + float(duration_ms)
                )
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, default=str) + "\n")
            except Exception:
                self._warn("could not append to the activity log")
        return record

    def time_budget(self) -> dict[str, int]:
        """Milliseconds spent per phase, from the spans that were measured.

        This is what answers "what was it doing all that time?" — the question a
        list of events, however complete, cannot answer on its own.
        """
        with self._lock:
            return {phase: int(total) for phase, total in sorted(self._phase_totals.items())}

    def _warn(self, what: str) -> None:
        if not self._warned:
            self._warned = True
            logger.warning("Activity log unavailable for run %s: %s", self.run_id, what)

    # -- reading ------------------------------------------------------------

    @classmethod
    def read(
        cls,
        run_id: str,
        *,
        since_seq: int = 0,
        limit: int = DEFAULT_READ_LIMIT,
        phase: str | None = None,
        root: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Records after `since_seq`, oldest first, capped.

        `since_seq` makes the UI able to poll for only what it has not seen,
        which is what lets an operator reload the page mid-run and still have the
        whole history.
        """
        limit = max(1, min(int(limit), MAX_READ_LIMIT))
        out: list[dict[str, Any]] = []
        for record in cls.iter_records(run_id, root=root):
            if int(record.get("seq", 0)) <= since_seq:
                continue
            if phase and record.get("phase") != phase:
                continue
            out.append(record)
            if len(out) >= limit:
                break
        return out

    @classmethod
    def iter_records(cls, run_id: str, *, root: Path | None = None) -> Iterator[dict[str, Any]]:
        if root is None:
            from app.config import get_settings

            root = get_settings().run_evidence_dir(run_id)
        path = Path(root) / ACTIVITY_LOG_FILENAME
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        # A torn final line while the run is still writing is
                        # normal, not corruption — skip it and keep reading.
                        continue
        except OSError:
            return
