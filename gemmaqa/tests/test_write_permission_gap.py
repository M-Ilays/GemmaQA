"""A run must say when POLICY, not the application, ended its work.

A live run against a record-management application stopped with
`all_safe_candidates_exhausted` after 27 of its 480 allowed actions. The reason
was that every remaining candidate was a write and the write flags were off --
but nothing in the message, the report, or the stop summary said so, so the run
read as "GemmaQA gave up" rather than "GemmaQA was not permitted". The operator
had to open the stored config JSON to find out.

"There was nothing left to do" and "there was plenty left to do and I was not
allowed to" are different findings, and only one of them is the operator's to
fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.auth_strategy import AuthenticationStrategy  # noqa: E402
from app.agent.frontier import FrontierBuilder  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.schemas import (  # noqa: E402
    FormDescriptor,
    FormField,
    InteractiveElement,
    PageState,
    RunConfiguration,
)


def _memory() -> RunMemory:
    return RunMemory(
        run_id="run_1",
        start_url="https://app.example.com/records",
        configuration=RunConfiguration(),
    )


def _page(*, with_create_control: bool = True, with_form: bool = False) -> PageState:
    elements = []
    if with_create_control:
        elements.append(
            InteractiveElement(
                element_id="el_002", tag="button", type="button",
                visible_text="Add a New Contact", selector="#add",
            )
        )
    forms = []
    if with_form:
        forms.append(
            FormDescriptor(
                form_id="form_015",
                submit_element_id="el_013",
                fields=[FormField(name="first", field_type="text", label="First Name", element_id="el_003")],
            )
        )
    return PageState(
        page_id="page_1",
        url="https://app.example.com/records",
        title="Records",
        state_fingerprint="fp_1",
        interactive_elements=elements,
        forms=forms,
    )


def _authenticated_builder() -> FrontierBuilder:
    auth = AuthenticationStrategy()
    auth.authenticated = True
    return FrontierBuilder(auth)


# ===========================================================================
# A -- the frontier records what policy withheld
# ===========================================================================


def test_a_creation_control_refused_by_policy_is_recorded():
    memory = _memory()
    _authenticated_builder().build(_page(), allow_safe_test_data=False, memory=memory)

    gap = memory.write_permission_gap()
    assert gap["missing_permissions"] == ["allow_safe_test_data_creation"]
    assert "creating a record" in gap["blocked_capabilities"]
    assert "Add a New Contact" in str(gap["blocked"][0]["examples"])


def test_a_fillable_form_refused_by_policy_is_recorded():
    memory = _memory()
    _authenticated_builder().build(
        _page(with_create_control=False, with_form=True), allow_safe_test_data=False, memory=memory
    )

    gap = memory.write_permission_gap()
    assert "filling and submitting a form" in gap["blocked_capabilities"]


def test_nothing_is_recorded_when_the_permission_is_granted():
    """The signal must mean "refused", not merely "a write existed"."""
    memory = _memory()
    _authenticated_builder().build(_page(with_form=True), allow_safe_test_data=True, memory=memory)

    assert memory.write_permission_gap() == {}


def test_nothing_is_recorded_when_the_application_offers_no_write():
    """An empty gap must not be produced by a read-only application -- otherwise
    every run would claim it was held back."""
    memory = _memory()
    page = PageState(
        page_id="page_2",
        url="https://app.example.com/about", title="About", state_fingerprint="fp_2",
        interactive_elements=[
            InteractiveElement(
                element_id="el_002", tag="a", type="link",
                visible_text="Documentation", selector="#docs",
            )
        ],
    )
    _authenticated_builder().build(page, allow_safe_test_data=False, memory=memory)

    assert memory.write_permission_gap() == {}


def test_nothing_is_recorded_before_authentication():
    """Pre-auth there is no session to write with, so policy is not what is
    holding the run back."""
    memory = _memory()
    FrontierBuilder(AuthenticationStrategy()).build(_page(), allow_safe_test_data=False, memory=memory)

    assert memory.write_permission_gap() == {}


def test_granting_the_permission_still_produces_the_candidate():
    """The recording must be a pure addition -- the candidate the frontier used
    to emit has to still be emitted."""
    memory = _memory()
    candidates = _authenticated_builder().build(_page(), allow_safe_test_data=True, memory=memory)

    assert any(c.candidate_type == "safe_test_data_create" for c in candidates)


def test_the_frontier_works_without_a_memory():
    """Auth-only call sites pass no memory; recording must not require one."""
    candidates = _authenticated_builder().build(_page(), allow_safe_test_data=False)
    assert all(c.candidate_type != "safe_test_data_create" for c in candidates)


# ===========================================================================
# B -- the recording itself
# ===========================================================================


def test_repeat_observations_accumulate_rather_than_duplicate():
    memory = _memory()
    for _ in range(4):
        memory.note_write_candidate_suppressed(
            required_flag="allow_safe_test_data_creation", capability="creating a record", evidence="Add"
        )

    gap = memory.write_permission_gap()
    assert len(gap["blocked"]) == 1
    assert gap["blocked"][0]["occurrences"] == 4
    assert gap["blocked"][0]["capabilities"] == ["creating a record"]


def test_examples_are_bounded():
    memory = _memory()
    for i in range(20):
        memory.note_write_candidate_suppressed(
            required_flag="allow_safe_test_data_creation", capability="creating a record", evidence=f"control {i}"
        )
    assert len(memory.write_permission_gap()["blocked"][0]["examples"]) <= 5


def test_several_flags_are_reported_together_most_frequent_first():
    memory = _memory()
    memory.note_write_candidate_suppressed(
        required_flag="allow_destructive_actions", capability="removing test records", evidence="1 record"
    )
    for _ in range(3):
        memory.note_write_candidate_suppressed(
            required_flag="allow_safe_test_data_creation", capability="creating a record", evidence="Add"
        )

    gap = memory.write_permission_gap()
    assert gap["missing_permissions"] == ["allow_safe_test_data_creation", "allow_destructive_actions"]


def test_the_explanation_names_the_capability_and_the_flag_to_change():
    memory = _memory()
    memory.note_write_candidate_suppressed(
        required_flag="allow_safe_test_data_creation", capability="creating a record", evidence="Add"
    )
    explanation = memory.write_permission_gap()["explanation"]
    assert "creating a record" in explanation
    assert "allow_safe_test_data_creation" in explanation


def test_an_unlimited_run_reports_an_empty_gap_not_a_missing_measurement():
    assert _memory().write_permission_gap() == {}


# ===========================================================================
# C -- it reaches the report the operator actually reads
# ===========================================================================


def _report(memory: RunMemory):
    from app.reporting.report_builder import ReportBuilder

    return ReportBuilder().build(memory)


def _blocked_memory() -> RunMemory:
    memory = _memory()
    memory.stop_reason = "all_safe_candidates_exhausted"
    memory.note_write_candidate_suppressed(
        required_flag="allow_safe_test_data_creation",
        capability="creating a record",
        evidence="Add a New Contact",
    )
    return memory


def test_the_final_report_carries_the_gap():
    report = _report(_blocked_memory())
    assert report.write_permission_gap["missing_permissions"] == ["allow_safe_test_data_creation"]


def test_the_stop_summary_leads_with_the_permission_gap():
    summary = _report(_blocked_memory()).stop_summary

    assert summary["what_prevented_execution"][0] == "write_permissions_not_granted"
    assert "allow_safe_test_data_creation" in summary["recommended_configuration_changes"][0]


def test_a_run_that_policy_did_not_limit_says_nothing_about_permissions():
    memory = _memory()
    memory.stop_reason = "exploration_complete"
    report = _report(memory)

    assert report.write_permission_gap == {}
    assert "write_permissions_not_granted" not in report.stop_summary["what_prevented_execution"]


def test_the_rendered_stop_section_states_the_blocker():
    markdown = " ".join(_report(_blocked_memory()).sections_markdown.values())

    assert "Blocked by configuration" in markdown
    assert "allow_safe_test_data_creation" in markdown
