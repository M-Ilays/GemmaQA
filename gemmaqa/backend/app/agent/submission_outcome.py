"""Did the application ACCEPT the submission, or merely receive the click?

Found by a live run against a real contact-management application: GemmaQA
filled a create form, clicked Submit, the server answered `400 Bad Request`,
and the run recorded the create as successful — because `note_result()` was
told `success=True`, meaning "the click executed". It then clicked Cancel,
re-opened the same form, and prepared to submit the identical rejected data
again, until the action budget ran out. Read, update, and delete were never
reached.

The mistake is a category error: *the click succeeding* and *the application
accepting the data* are different facts, and only the second one means the
workflow made progress.

This module supplies the second fact from evidence the run already collects.
It is deliberately application-neutral — it knows nothing about any particular
backend, field name, or error format. It reasons from four generic signals:

  * a failed mutating request appearing after the action (4xx vs 5xx),
  * whether the submitted form is still on screen,
  * whether the location changed,
  * whether new alert/error text appeared.

Field-level attribution is BEST EFFORT and clearly marked as such: browsers do
not hand us response bodies, so when an application renders its validation
message on the page we can match it against the form's own field labels, and
when it does not, we say we do not know rather than guessing. What GemmaQA does
with "rejected but unattributed" is retry conservatively, not retry identically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.utils.logging import get_logger
from app.utils.sanitization import sanitize_text

logger = get_logger("agent.submission_outcome")

# Outcome vocabulary. Closed on purpose: every consumer switches on these.
SUBMISSION_ACCEPTED = "accepted"
SUBMISSION_REJECTED_VALIDATION = "rejected_validation"
SUBMISSION_REJECTED_SERVER = "rejected_server"
SUBMISSION_UNKNOWN = "unknown"

SUBMISSION_OUTCOMES = frozenset(
    {SUBMISSION_ACCEPTED, SUBMISSION_REJECTED_VALIDATION, SUBMISSION_REJECTED_SERVER, SUBMISSION_UNKNOWN}
)

# Requests that can change server state. A failed GET during a submit is noise
# (an analytics beacon, a missing icon); a failed POST is the submit itself.
# One definition, in the layer that records the requests, so the recorder and
# this classifier cannot disagree about what counts as a write.
from app.browser.network_monitor import MUTATING_HTTP_METHODS as _MUTATING_METHODS

_STATUS_IN_TEXT = re.compile(r"\b([1-5]\d{2})\b")
_METHOD_IN_TEXT = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b", re.IGNORECASE)

# Words that make free page text a validation complaint rather than ordinary
# copy. Generic across languages-as-written-in-English UIs; absence of a match
# never asserts success, it only fails to add evidence.
_VALIDATION_WORDS = (
    "invalid",
    "required",
    "must be",
    "must not",
    "cannot be",
    "can't be",
    "is not valid",
    "too long",
    "too short",
    "maximum",
    "minimum",
    "already exists",
    "error",
    "failed",
    "not allowed",
)


@dataclass
class SubmissionOutcome:
    """What the application did with the submitted data."""

    outcome: str = SUBMISSION_UNKNOWN
    # Field element_ids the application appears to be complaining about.
    # Empty is common and honest: most backends do not surface this to a browser.
    rejected_element_ids: list[str] = field(default_factory=list)
    # Human-readable, sanitized evidence for the report.
    messages: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    http_status: int | None = None

    @property
    def rejected(self) -> bool:
        return self.outcome in {SUBMISSION_REJECTED_VALIDATION, SUBMISSION_REJECTED_SERVER}

    @property
    def is_application_defect_candidate(self) -> bool:
        """A 5xx is the application failing; a 4xx is the application refusing.

        Only the first is a defect on the application's side by itself. A 4xx
        means the data was unacceptable — which is a defect only if the data was
        actually valid, and GemmaQA cannot claim that until a conservative retry
        has also been refused.
        """
        return self.outcome == SUBMISSION_REJECTED_SERVER

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "http_status": self.http_status,
            "rejected_element_ids": list(self.rejected_element_ids),
            "messages": list(self.messages)[:5],
            "signals": list(self.signals),
        }


def _succeeded_mutations(state: Any) -> list[tuple[int, str]]:
    """State-changing requests the application ANSWERED SUCCESSFULLY.

    The mirror of `_failed_mutations`, and the evidence that was missing.
    Acceptance used to be inferred only from the form disappearing or the URL
    changing — both UI side-effects, and both absent on an application that
    re-renders the same form in place after saving. So a create that genuinely
    succeeded was classified `unknown`, the record was never registered, and the
    run went on to fill a third form instead of using the one it had just made.

    A 2xx on a POST/PUT/PATCH is the application itself saying it accepted the
    data. That is stronger evidence than anything the DOM can offer.

    Only structured `network_entries` are read: the `network_failures` fallback
    that `_failed_mutations` uses lists failures only, so there is no
    corresponding text source for successes to parse.
    """
    found: list[tuple[int, str]] = []
    for entry in getattr(state, "network_entries", None) or []:
        method = str(getattr(entry, "method", "") or "").upper()
        status = getattr(entry, "status", None)
        if method not in _MUTATING_METHODS:
            continue
        if bool(getattr(entry, "failed", False)):
            continue
        if isinstance(status, int) and 200 <= status < 300:
            found.append((status, str(getattr(entry, "url", ""))))
    return found


def _failed_mutations(state: Any) -> list[tuple[int | None, str]]:
    """Failed state-changing requests visible on this observation."""
    found: list[tuple[int | None, str]] = []

    for entry in getattr(state, "network_entries", None) or []:
        method = str(getattr(entry, "method", "") or "").upper()
        status = getattr(entry, "status", None)
        failed = bool(getattr(entry, "failed", False))
        if method not in _MUTATING_METHODS:
            continue
        if failed or (isinstance(status, int) and status >= 400):
            found.append((status if isinstance(status, int) else None, str(getattr(entry, "url", ""))))

    if found:
        return found

    # Fall back to the pre-formatted strings when structured entries are absent
    # (older observations, or an adapter that only reports summaries).
    for line in getattr(state, "network_failures", None) or []:
        text = str(line)
        method_match = _METHOD_IN_TEXT.search(text)
        if method_match and method_match.group(1).upper() not in _MUTATING_METHODS:
            continue
        status_match = _STATUS_IN_TEXT.search(text)
        status = int(status_match.group(1)) if status_match else None
        if status is None or status >= 400:
            found.append((status, text))
    return found


def _new_alert_texts(before: Any, after: Any) -> list[str]:
    previous = {str(a).strip() for a in (getattr(before, "alerts", None) or [])}
    out: list[str] = []
    for alert in getattr(after, "alerts", None) or []:
        text = str(alert).strip()
        if text and text not in previous:
            out.append(text)
    return out


def _looks_like_validation_text(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _VALIDATION_WORDS)


def _form_present(state: Any, form_id: str | None) -> bool:
    if not form_id:
        return False
    return any(getattr(f, "form_id", None) == form_id for f in (getattr(state, "forms", None) or []))


def attribute_fields(messages: list[str], fields: list[Any]) -> list[str]:
    """Best-effort: which of THIS form's fields is the application complaining about?

    Matches each field's own label/name tokens against the message text. Returns
    element_ids. Deliberately conservative — a field is only named when a
    distinctive token of its label appears, so "Email is invalid" attributes to
    the email field while a generic "Please check your input" attributes to none.
    """
    if not messages:
        return []
    blob = " ".join(messages).lower()
    hits: list[str] = []
    for f in fields:
        element_id = getattr(f, "element_id", None)
        if not element_id:
            continue
        tokens: set[str] = set()
        for source in (getattr(f, "label", None), getattr(f, "name", None), getattr(f, "accessible_name", None)):
            if not source:
                continue
            for token in re.split(r"[^a-z0-9]+", str(source).lower()):
                # Two-letter tokens ("of", "or") match everything and mean nothing.
                if len(token) > 3:
                    tokens.add(token)
        if tokens and any(token in blob for token in tokens):
            hits.append(str(element_id))
    return hits


def classify_submission(
    *,
    before_state: Any,
    after_state: Any,
    form_id: str | None = None,
    fields: list[Any] | None = None,
    click_succeeded: bool = True,
) -> SubmissionOutcome:
    """Decide what the application did with a form submission.

    Evidence only. When the signals genuinely do not distinguish acceptance from
    rejection the result is `unknown`, which callers must treat as "not proven
    accepted" rather than as either outcome.
    """
    outcome = SubmissionOutcome()

    if not click_succeeded:
        outcome.outcome = SUBMISSION_UNKNOWN
        outcome.signals.append("submit_click_did_not_execute")
        return outcome

    # Compare by COUNT per (status, url), not by URL identity: a retry hits the
    # same endpoint as the attempt before it, so "this URL already failed once"
    # would hide every failure after the first. Observed live — a second
    # rejected submit to the same endpoint was classified `unknown`, and the
    # workflow treated a refusal as merely unproven.
    before_counts: dict[tuple[int | None, str], int] = {}
    for status, url in _failed_mutations(before_state):
        key = (status, str(url))
        before_counts[key] = before_counts.get(key, 0) + 1

    new_failures: list[tuple[int | None, str]] = []
    seen_counts: dict[tuple[int | None, str], int] = {}
    for status, url in _failed_mutations(after_state):
        key = (status, str(url))
        seen_counts[key] = seen_counts.get(key, 0) + 1
        if seen_counts[key] > before_counts.get(key, 0):
            new_failures.append((status, url))

    # Counted per (status, url) exactly as failures are, and for the same reason:
    # a retry hits the same endpoint as the attempt before it, so "this URL already
    # succeeded once" would hide the success that actually belongs to THIS submit.
    before_success_counts: dict[tuple[int, str], int] = {}
    for status, url in _succeeded_mutations(before_state):
        key = (status, str(url))
        before_success_counts[key] = before_success_counts.get(key, 0) + 1

    new_successes: list[tuple[int, str]] = []
    seen_success_counts: dict[tuple[int, str], int] = {}
    for status, url in _succeeded_mutations(after_state):
        key = (status, str(url))
        seen_success_counts[key] = seen_success_counts.get(key, 0) + 1
        if seen_success_counts[key] > before_success_counts.get(key, 0):
            new_successes.append((status, url))

    alerts = _new_alert_texts(before_state, after_state)
    validation_alerts = [a for a in alerts if _looks_like_validation_text(a)]
    still_on_form = _form_present(after_state, form_id)
    url_changed = str(getattr(before_state, "url", "")) != str(getattr(after_state, "url", ""))

    server_error = next((s for s, _u in new_failures if isinstance(s, int) and s >= 500), None)
    client_error = next((s for s, _u in new_failures if isinstance(s, int) and 400 <= s < 500), None)

    if server_error is not None:
        outcome.outcome = SUBMISSION_REJECTED_SERVER
        outcome.http_status = server_error
        outcome.signals.append(f"failed_mutating_request_{server_error}")
    elif client_error is not None:
        outcome.outcome = SUBMISSION_REJECTED_VALIDATION
        outcome.http_status = client_error
        outcome.signals.append(f"failed_mutating_request_{client_error}")
    elif validation_alerts:
        outcome.outcome = SUBMISSION_REJECTED_VALIDATION
        outcome.signals.append("validation_message_appeared")
    elif new_failures:
        # A mutating request failed with no readable status (blocked, aborted,
        # connection reset). Not attributable to the data, so not "validation".
        outcome.outcome = SUBMISSION_REJECTED_SERVER
        outcome.signals.append("mutating_request_failed_without_status")
    elif new_successes:
        # The application answered a state-changing request with 2xx. It said yes.
        # Checked BEFORE the DOM heuristics because those describe what the page
        # did, while this describes what the APPLICATION did — and an app that
        # re-renders the same form after saving produces no DOM signal at all,
        # which is how genuine creates were being recorded as `unknown`.
        # Ordered AFTER the failure branches: if anything failed, that matters more.
        status = new_successes[0][0]
        outcome.outcome = SUBMISSION_ACCEPTED
        outcome.http_status = status
        outcome.signals.append(f"mutating_request_succeeded_{status}")
    elif url_changed and not still_on_form:
        outcome.outcome = SUBMISSION_ACCEPTED
        outcome.signals.append("navigated_away_from_form")
    elif not still_on_form:
        outcome.outcome = SUBMISSION_ACCEPTED
        outcome.signals.append("form_no_longer_present")
    else:
        # Still sitting on the same form with nothing to show for it. Common for
        # client-side validation that renders no recognisable message, so this
        # is suspicious but not proven either way.
        outcome.outcome = SUBMISSION_UNKNOWN
        outcome.signals.append("still_on_form_without_visible_outcome")

    outcome.messages = [sanitize_text(a)[:240] for a in (validation_alerts or alerts)][:5]
    if outcome.rejected:
        outcome.rejected_element_ids = attribute_fields(outcome.messages, list(fields or []))
        if outcome.rejected_element_ids:
            outcome.signals.append("field_attributed_from_page_text")
        else:
            outcome.signals.append("rejection_not_attributable_to_a_specific_field")

    logger.info(
        "Submission outcome=%s status=%s signals=%s attributed=%d",
        outcome.outcome,
        outcome.http_status,
        ",".join(outcome.signals),
        len(outcome.rejected_element_ids),
    )
    return outcome
