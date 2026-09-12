"""Form and authentication workflow lifecycle states."""

from __future__ import annotations

from enum import Enum


class FormLifecycle(str, Enum):
    DISCOVERED = "discovered"
    # CRUD surface discovery: a form's intent (search/filter/create/edit/
    # delete_confirmation/bulk_action/upload/settings/authentication/
    # unknown) has been classified — see app.perception.form_intent_classifier.
    # Always reached before INSPECTED; classification needs no browser action,
    # inspection (below) is the deliberate follow-up QA step.
    CLASSIFIED = "classified"
    INSPECTED = "inspected"
    # A form judged safe/relevant enough to attempt testing, but not yet
    # attempted this run — the explicit state between "we understand this
    # form" and "we tried it", so a form can sit here indefinitely with a
    # `not_tested_reason` explaining why it never advanced further.
    CANDIDATE_FOR_TESTING = "candidate_for_testing"
    DATA_PLAN_CREATED = "data_plan_created"
    POSITIVE_SUBMISSION_AVAILABLE = "positive_submission_available"
    POSITIVE_SUBMISSION_ATTEMPTED = "positive_submission_attempted"
    NEGATIVE_SUBMISSION_AVAILABLE = "negative_submission_available"
    SUBMITTED = "submitted"
    VALIDATION_OBSERVED = "validation_observed"
    # Alias asked for by the CRUD surface discovery task's lifecycle
    # ("tested"); SUCCEEDED remains the richer, pre-existing terminal state
    # both mean "a real submission attempt completed and was observed".
    TESTED = "submitted"
    SUCCEEDED = "succeeded"
    VERIFIED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    EXHAUSTED = "exhausted"


# Why a form never advanced to (or past) CANDIDATE_FOR_TESTING / TESTED —
# stored on `AppForm.not_tested_reason` so "0 forms tested" is always
# explainable rather than a silent gap.
NOT_TESTED_REASONS = frozenset(
    {
        "unsafe",
        "missing_data",
        "unknown_intent",
        "unsupported_control",
        "no_verification_strategy",
        "actor_unavailable",
        "write_disabled",
        "duplicate",
        "no_runtime_action",
        "other",
    }
)


class AuthWorkflowStatus(str, Enum):
    DETECTED = "detected"
    CREDENTIALS_REQUIRED = "credentials_required"
    READY = "ready"
    FILLING = "filling"
    SUBMITTED = "submitted"
    AUTHENTICATED = "authenticated"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    SESSION_EXPIRED = "session_expired"


# States that still leave an authentication form actionable
ACTIONABLE_FORM_STATES = frozenset(
    {
        FormLifecycle.DISCOVERED,
        FormLifecycle.INSPECTED,
        FormLifecycle.DATA_PLAN_CREATED,
        FormLifecycle.POSITIVE_SUBMISSION_AVAILABLE,
        FormLifecycle.NEGATIVE_SUBMISSION_AVAILABLE,
        FormLifecycle.FAILED,
        FormLifecycle.VALIDATION_OBSERVED,
    }
)

TERMINAL_FORM_STATES = frozenset(
    {
        FormLifecycle.SUCCEEDED,
        FormLifecycle.BLOCKED,
        FormLifecycle.EXHAUSTED,
    }
)
