"""Stop resubmitting a form the application has already refused.

A live OrangeHRM run submitted the same form with the same data 27-28 times,
received the same rejection each time, and spent ~60% of its action budget
learning nothing. The remaining budget never reached read, update, delete, or
any other module.

`max_attempts` did not catch it: that counts attempts within one workflow and is
3. The rule that does catch it is simpler and stronger — the SAME data refused in
the SAME way is not new information, so stop at the first repeat.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.form_workflow import GenericFormWorkflow  # noqa: E402
from app.agent.submission_outcome import (  # noqa: E402
    SUBMISSION_ACCEPTED,
    SUBMISSION_REJECTED_VALIDATION,
    SubmissionOutcome,
)


def _workflow(**values) -> GenericFormWorkflow:
    wf = GenericFormWorkflow(
        workflow_id="wf_1", form_id="form_121", run_id="run_1",
        page_url="https://app.example.com/addUser", purpose="safe_test_data_creation",
    )
    wf.planned_values = dict(values or {"el_1": "GemmaQA_TEST_a"})
    return wf


def _rejection(message="Employee Name is required", status=422) -> SubmissionOutcome:
    outcome = SubmissionOutcome()
    outcome.outcome = SUBMISSION_REJECTED_VALIDATION
    outcome.http_status = status
    outcome.messages = [message]
    return outcome


def test_the_same_rejection_with_the_same_data_blocks_the_form():
    wf = _workflow()
    wf.note_result(success=True, outcome=_rejection())
    assert wf.state != "blocked", "the first refusal is information; only the repeat is not"

    wf.note_result(success=True, outcome=_rejection())
    assert wf.state == "blocked"
    assert wf.last_error == "identical_rejection_with_unchanged_data"


def test_the_reason_says_what_the_application_objected_to():
    """`blocked` alone tells an operator nothing actionable."""
    wf = _workflow()
    wf.note_result(success=True, outcome=_rejection())
    wf.note_result(success=True, outcome=_rejection())

    assert "Employee Name is required" in wf.blocked_reason
    assert "Retrying cannot change the outcome" in wf.blocked_reason


def test_a_different_rejection_is_new_information_and_does_not_block():
    """The application telling us something new is progress, not repetition."""
    wf = _workflow()
    wf.note_result(success=True, outcome=_rejection("Employee Name is required"))
    wf.note_result(success=True, outcome=_rejection("Username already exists"))

    assert wf.state != "blocked"


def test_changed_data_is_a_genuine_retry_even_with_the_same_message():
    """The point is to stop repetition, not to stop retrying: a retry that
    actually changed a value deserves its chance."""
    wf = _workflow(el_1="GemmaQA_TEST_a")
    wf.note_result(success=True, outcome=_rejection())
    wf.planned_values["el_1"] = "conservative"
    wf.note_result(success=True, outcome=_rejection())

    assert wf.state != "blocked"


def test_a_status_change_alone_counts_as_new_information():
    wf = _workflow()
    wf.note_result(success=True, outcome=_rejection(status=422))
    wf.note_result(success=True, outcome=_rejection(status=500))

    assert wf.state != "blocked"


def test_acceptance_is_unaffected():
    wf = _workflow()
    accepted = SubmissionOutcome()
    accepted.outcome = SUBMISSION_ACCEPTED
    wf.note_result(success=True, outcome=accepted)

    assert wf.state == "verified"


def test_a_blocked_form_is_not_offered_again():
    """`blocked` is in the terminal set the frontier and controller both check,
    so the form is not restarted — the outer loop closes too."""
    import inspect

    from app.agent import controller as controller_module
    from app.agent import frontier as frontier_module

    controller_src = inspect.getsource(controller_module.AgentController.run)
    assert 'form_wf.state in {"failed", "blocked", "skipped"}' in controller_src
    assert "failed_form_workflow_counts[form_wf.form_id] += 1" in controller_src
    assert "failed_counts.get(form.form_id, 0) >= 1" in inspect.getsource(frontier_module)


def test_the_abandonment_reaches_the_activity_log():
    from app.reporting.activity_log import phase_for, summarize

    text = summarize("form_abandoned", {
        "form_id": "form_121", "attempts": 2,
        "reason": "The application refused the same data in the same way twice.",
        "unsatisfied_references": ["Employee Name"],
    })
    assert "form_121" in text
    assert "refused the same data" in text
    assert "Employee Name" in text
    assert phase_for("form_abandoned") == "verification"
