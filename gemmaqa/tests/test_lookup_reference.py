"""A lookup field names a record; it is not a blank to invent a value for.

OrangeHRM's Admin -> Add User requires "Employee Name", which only accepts
employees that already exist in PIM. GemmaQA typed `GemmaQA_TEST_<run>_option`
into it, the form could never submit, and the real finding — *this form needs an
employee and there is not one* — stayed hidden behind an invalid value
resubmitted 27 times per run.

Detection from the DOM is impossible. Probed live, that input has no `role`, no
`aria-autocomplete`, no `list` and no `name` — only a placeholder. It is
byte-for-byte as ordinary as the Username box beside it, and matching its
placeholder text would be application-specific vocabulary.

Detection from BEHAVIOUR works, and these are the observations it rests on
(captured live, same form, same session):

    Employee Name + "GemmaQA_TEST_x9"  ->  role="listbox", 1 option
    Employee Name + "a"                ->  5 role="option" records
    Username      + "GemmaQA_TEST_x9"  ->  nothing appears

Telling a real record from a "nothing found" placeholder needs no vocabulary
either: a fabricated test token cannot match a stored record, so whatever the
list shows while that token is in the field IS the empty state.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app.browser import custom_controls  # noqa: E402
from app.browser.custom_controls import (  # noqa: E402
    UNSATISFIED_REFERENCE_MARKER,
    UnsatisfiedReferenceError,
    fill_or_choose,
    resolve_lookup,
)

TOKEN = "GemmaQA_TEST_ab12cd_option"

# What the live probe actually saw, in order. "Searching...." is the trap: it is
# the FIRST thing a non-matching value produces, and treating that instant as
# the empty state would let "No Records Found" through as a real record.
EMPTY_STATE_SEQUENCE = [["Searching...."], ["No Records Found"]]
REAL_RECORDS = ["A8DCo 4Ys 010Z", "Ranga Akunuri", "Timothy Lewis Amiano"]


@pytest.fixture(autouse=True)
def _fast_timings(monkeypatch):
    """The real budgets are sized for a page that answers in ~1.5s."""
    monkeypatch.setattr(custom_controls, "LOOKUP_FIRST_OPTION_TIMEOUT_MS", 20)
    monkeypatch.setattr(custom_controls, "LOOKUP_BASELINE_SETTLE_MS", 40)
    monkeypatch.setattr(custom_controls, "LOOKUP_PROBE_BUDGET_MS", 60)
    monkeypatch.setattr(custom_controls, "LOOKUP_POLL_MS", 5)


# ===========================================================================
# Fakes: a page whose option list depends on what is in the field
# ===========================================================================


class _OptionSet:
    def __init__(self, field):
        self._field = field

    @property
    def first(self):
        return self

    def nth(self, index):
        self._field.clicked_index = index
        self._field.clicked_text = self._field.current_options(advance=False)[index]
        return self

    async def wait_for(self, **_kw):
        # Peeks, like Playwright's does: waiting for a list to appear must not
        # consume the transient that appears first.
        if not self._field.current_options(advance=False):
            raise TimeoutError("no options")

    async def all_inner_texts(self):
        return self._field.current_options()

    async def click(self):
        return None


class _Page:
    def __init__(self, field):
        self._field = field

    def locator(self, _selector):
        return _OptionSet(self._field)

    async def wait_for_timeout(self, ms):
        await asyncio.sleep(ms / 1000)


class _Field:
    """An ordinary <input> whose page happens to answer typing with a list.

    `responses` maps what is typed to the option texts that appear. A list of
    lists is served one entry per read, so a transient can be reproduced.
    """

    def __init__(self, responses: dict[str, object], *, is_chooser=False):
        self._responses = responses
        self._is_chooser = is_chooser
        self.value = ""
        self.filled: list[str] = []
        self.clicked = 0
        self.clicked_index: int | None = None
        self.clicked_text: str | None = None
        self.page = _Page(self)
        self._reads: dict[str, int] = {}

    def current_options(self, *, advance: bool = True) -> list[str]:
        entry = self._responses.get(self.value, [])
        if entry and isinstance(entry[0], list):
            i = self._reads.get(self.value, 0)
            if advance:
                self._reads[self.value] = i + 1
            return list(entry[min(i, len(entry) - 1)])
        return list(entry)

    async def evaluate(self, script):
        # A real Locator runs the script it is given; `fill_or_choose` asks
        # "is this a file input?" before "is this a chooser?".
        if "'file'" in script:
            return False
        return "declared" if self._is_chooser else ""

    async def fill(self, value):
        self.value = value
        self.filled.append(value)

    async def click(self):
        self.clicked += 1


def _lookup_with_records() -> _Field:
    return _Field({TOKEN: EMPTY_STATE_SEQUENCE, "a": REAL_RECORDS})


def _resolve(field: _Field, value: str):
    """Resolution runs AFTER the ordinary fill, so reproduce that ordering.

    `field.filled` deliberately stays empty here, so the assertions below that
    it is empty still mean "never probed".
    """
    field.value = value
    return asyncio.run(resolve_lookup(field, value))


# ===========================================================================
# A -- an ordinary text box is left alone
# ===========================================================================


def test_a_text_box_that_offers_nothing_is_just_a_text_box():
    """The Username field beside the lookup. Nothing appeared when it was typed
    into, and nothing about it may change."""
    field = _Field({})
    outcome = _resolve(field, TOKEN)

    assert outcome.kind == "fill"
    assert outcome.chosen == ""
    assert field.filled == []  # never probed
    assert field.clicked_index is None


def test_an_operator_supplied_value_is_never_probed():
    """Credentials and operator data may legitimately match a stored record, so
    they cannot serve as the cannot-match baseline — and paying the probe on
    every ordinary fill would cost the run seconds it has better uses for."""
    field = _Field({"Admin": REAL_RECORDS})
    outcome = _resolve(field, "Admin")

    assert outcome.kind == "fill"
    assert field.filled == []


# ===========================================================================
# B -- a lookup with records is satisfied with a real one
# ===========================================================================


def test_a_real_record_is_chosen_instead_of_the_invented_value():
    field = _lookup_with_records()
    outcome = _resolve(field, TOKEN)

    assert outcome.kind == "lookup"
    assert outcome.chosen == REAL_RECORDS[0]
    assert outcome.probe == "a"
    assert field.clicked_text == REAL_RECORDS[0]


def test_the_transient_is_baselined_and_never_offered_as_a_record():
    """The trap the live probe exposed: at 39ms the list said "Searching....".
    Whatever a non-matching value produces at ANY moment is the empty state."""
    field = _lookup_with_records()
    outcome = _resolve(field, TOKEN)

    assert set(outcome.empty_state) == {"Searching....", "No Records Found"}
    assert outcome.chosen not in outcome.empty_state


def test_a_second_probe_is_tried_when_the_first_matches_nothing():
    """A lookup keyed on numbers matches neither letter of the alphabet."""
    field = _Field({TOKEN: EMPTY_STATE_SEQUENCE, "a": [], "1": ["1001 - Widget"]})
    outcome = _resolve(field, TOKEN)

    assert outcome.chosen == "1001 - Widget"
    assert outcome.probe == "1"


def test_probing_is_bounded():
    """Every probe costs a wait budget, so the list may not grow silently."""
    assert len(custom_controls.LOOKUP_PROBE_INPUTS) <= 3


# ===========================================================================
# C -- a lookup with no records is a finding, not a retry
# ===========================================================================


def test_a_lookup_offering_no_records_is_reported():
    field = _Field({TOKEN: EMPTY_STATE_SEQUENCE, "a": ["No Records Found"], "1": []})

    with pytest.raises(UnsatisfiedReferenceError) as excinfo:
        _resolve(field, TOKEN)

    assert UNSATISFIED_REFERENCE_MARKER in str(excinfo.value)
    assert "No Records Found" in excinfo.value.observed


def test_the_empty_state_is_quoted_back_so_the_report_can_show_it():
    field = _Field({TOKEN: [["Nothing here"]], "a": [], "1": []})

    with pytest.raises(UnsatisfiedReferenceError) as excinfo:
        _resolve(field, TOKEN)

    assert "Nothing here" in str(excinfo.value)


# ===========================================================================
# D -- the shared entry point behaves the same for both execution paths
# ===========================================================================


def test_fill_or_choose_resolves_a_lookup_after_filling_it():
    """A lookup IS a text box until something is typed into it, so this question
    can only be asked after the fill, never before."""
    field = _lookup_with_records()
    outcome = asyncio.run(fill_or_choose(field, TOKEN))

    assert field.filled[0] == TOKEN  # the ordinary fill still happened first
    assert outcome.kind == "lookup"
    assert outcome.chosen == REAL_RECORDS[0]


def test_fill_or_choose_leaves_a_plain_field_untouched_beyond_the_fill():
    field = _Field({})
    outcome = asyncio.run(fill_or_choose(field, TOKEN))

    assert field.filled == [TOKEN]
    assert outcome.kind == "fill"


def test_a_declared_chooser_still_takes_the_chooser_path():
    """#166 must not regress: an ARIA chooser is clicked open, not filled."""
    field = _Field({"": ["-- Select --", "Admin", "ESS"]}, is_chooser=True)
    outcome = asyncio.run(fill_or_choose(field, "ESS"))

    assert outcome.kind == "custom_choice"
    assert outcome.chosen == "ESS"
    assert field.filled == []


def test_an_unsatisfied_reference_is_not_swallowed_by_the_chooser_fallback():
    """`fill_or_choose` catches ValueError to fall back to filling a focusable
    div. `UnsatisfiedReferenceError` IS a ValueError and must pass through."""
    import inspect

    source = inspect.getsource(custom_controls.fill_or_choose)
    assert "except UnsatisfiedReferenceError:" in source
    assert source.index("except UnsatisfiedReferenceError:") < source.index("except ValueError:")


# ===========================================================================
# E -- the form workflow turns it into a stated prerequisite
# ===========================================================================


class _Field_(dict):
    """A form field as the workflow sees it."""

    def __getattr__(self, name):
        return self.get(name)


def _workflow():
    from app.agent.form_workflow import GenericFormWorkflow

    wf = GenericFormWorkflow.__new__(GenericFormWorkflow)
    wf.form_id = "form_1"
    wf.fields = [_Field_(element_id="el_019", label="Employee Name")]
    wf.unsatisfied_references = []
    wf.state = "filling"
    wf.last_error = None
    wf.blocked_reason = ""
    wf.attempts = 0
    return wf


def test_the_form_is_blocked_rather_than_retried():
    """No amount of retrying creates the missing record. Spending a retry budget
    on it is exactly the loop this task exists to end."""
    wf = _workflow()
    wf.note_unsatisfied_reference("el_019", "offered ('No Records Found',)")

    assert wf.state == "blocked"
    assert wf.attempts == 0  # not an attempt — an impossibility
    assert wf.last_error == "unsatisfied_record_reference"


def test_the_field_is_named_by_its_label_in_the_report():
    wf = _workflow()
    wf.note_unsatisfied_reference("el_019")

    assert wf.unsatisfied_references == ["Employee Name"]
    assert "Employee Name" in wf.blocked_reason
    assert "until such a record exists" in wf.blocked_reason


def test_an_unlabelled_field_falls_back_to_its_element_id():
    wf = _workflow()
    wf.fields = [_Field_(element_id="el_019", label="")]
    wf.note_unsatisfied_reference("el_019")

    assert wf.unsatisfied_references == ["el_019"]


def test_the_same_field_is_recorded_once():
    wf = _workflow()
    wf.note_unsatisfied_reference("el_019")
    wf.note_unsatisfied_reference("el_019")

    assert wf.unsatisfied_references == ["Employee Name"]


# ===========================================================================
# F -- the reason reaches the operator
# ===========================================================================


def test_the_controller_recognises_the_marker_rather_than_matching_prose():
    import inspect

    from app.agent.controller import AgentController

    source = inspect.getsource(AgentController.run)
    assert "UNSATISFIED_REFERENCE_MARKER in (result.error or \"\")" in source
    assert "note_unsatisfied_reference" in source


def test_the_marker_has_exactly_one_definition():
    """Two copies of a rule with only one on the measured path is how four bugs
    got written this session."""
    import subprocess

    root = Path(__file__).resolve().parents[1] / "backend" / "app"
    hits = [
        line for line in subprocess.run(
            ["git", "grep", "-n", "UNSATISFIED_REFERENCE_MARKER ="],
            cwd=root, capture_output=True, text=True,
        ).stdout.splitlines() if line.strip()
    ]
    # Not a git repo in this checkout; fall back to a filesystem scan.
    if not hits:
        hits = [
            f"{p}:{i}"
            for p in root.rglob("*.py")
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if line.startswith("UNSATISFIED_REFERENCE_MARKER =")
        ]
    assert len(hits) == 1, hits


def test_a_blocked_form_is_announced_from_both_places_it_can_block():
    """A form can now be abandoned at a fill as well as at a submit, and only
    the submit path used to say anything."""
    import inspect

    from app.agent.controller import AgentController

    source = inspect.getsource(AgentController.run)
    assert source.count("_emit_form_abandoned") == 2
    assert "form_abandoned" in inspect.getsource(AgentController._emit_form_abandoned)


# ===========================================================================
# G -- the prerequisite survives into the final report
# ===========================================================================
#
# The workflow that discovered it is discarded the moment it blocks, so
# "detected" and "reported" are two different things. Before this, the finding
# reached the activity log and stopped there — a report that says only "form not
# tested" hides the one fact the operator can act on.


def _memory_with_prerequisite():
    from app.agent.memory import RunMemory
    from app.utils.ids import new_id

    memory = RunMemory(run_id=new_id(), start_url="https://example.test/")
    memory.note_unsatisfied_references("form_1", ["Employee Name"])
    return memory


def test_memory_keeps_the_prerequisite_after_the_workflow_is_gone():
    memory = _memory_with_prerequisite()
    assert memory.unsatisfied_form_references == {"form_1": ["Employee Name"]}


def test_the_same_reference_is_not_recorded_twice():
    memory = _memory_with_prerequisite()
    memory.note_unsatisfied_references("form_1", ["Employee Name", "Employee Name"])
    assert memory.unsatisfied_form_references["form_1"] == ["Employee Name"]


def test_nothing_is_recorded_when_there_is_nothing_to_record():
    from app.agent.memory import RunMemory
    from app.utils.ids import new_id

    memory = RunMemory(run_id=new_id(), start_url="https://example.test/")
    memory.note_unsatisfied_references("form_1", [])
    assert memory.unsatisfied_form_references == {}


def test_the_report_states_the_prerequisite_as_a_limitation():
    from app.reporting.report_builder import ReportBuilder

    lines = ReportBuilder()._known_limitations(_memory_with_prerequisite())
    hit = [ln for ln in lines if "Employee Name" in ln]
    assert hit, lines
    assert "does not currently hold" in hit[0]


def test_the_report_recommends_creating_the_missing_record():
    from app.reporting.report_builder import ReportBuilder

    memory = _memory_with_prerequisite()
    tips = ReportBuilder()._recommended_next(memory, memory.coverage())
    assert any("Employee Name" in t and "Create a record" in t for t in tips), tips


def test_a_run_with_no_unmet_prerequisite_says_nothing_about_one():
    """An empty result must mean "none", never "not measured"."""
    from app.agent.memory import RunMemory
    from app.reporting.report_builder import ReportBuilder
    from app.utils.ids import new_id

    memory = RunMemory(run_id=new_id(), start_url="https://example.test/")
    lines = ReportBuilder()._known_limitations(memory)
    assert not [ln for ln in lines if "does not currently hold" in ln]
