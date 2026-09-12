"""A form that keeps SUCCEEDING must still stop being restarted.

Live on OrangeHRM's Buzz newsfeed: GemmaQA posted 20 times in 44 actions before
the operator cancelled the run. Every submit was accepted. Read straight from
that run's activity log:

    seq= 481 inspect_form  el=form_055
    seq= 517 fill el_017 | seq= 535 click el_018      <- post 1
    seq= 553 fill el_017 | seq= 571 click el_018      <- post 2
    ...
    seq= 661 inspect_form  el=form_056                 <- SAME form, new id
    ...
    seq= 859 inspect_form  el=form_085                 <- SAME form, new id
    ...  20 fill+click pairs in total, every one ok=True

Two independent defects met, and either alone reopens the loop:

1. **Every guard counted failures only.** `failed_form_workflow_counts` gates
   restarting a form. A form whose submissions the application keeps ACCEPTING
   never touches that counter, so it stayed eligible forever. Nothing was wrong
   from the workflow's point of view — that was exactly the problem, and it is
   why none of the repeated-rejection work caught this.

2. **`form_id` is positional.** It comes from `nextId('form')`, a document-order
   counter. The Buzz post box sits BELOW the feed, so each accepted post
   inserted an entry above it and renumbered the form. Even a success counter
   keyed on `form_id` would have reset every cycle.

Defect 2 is the same shape as #167 (a nav item signed by a page fingerprint that
clicking it changed): never identify a thing by something the action on it
modifies.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.form_identity import form_signature  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.schemas import FormDescriptor, FormField  # noqa: E402
from app.utils.ids import new_id  # noqa: E402

BUZZ_URL = "https://opensource-demo.orangehrmlive.com/web/index.php/buzz/viewBuzz"


def _buzz_form(form_id: str) -> FormDescriptor:
    """The Buzz post box as the observer actually reported it: one bare
    textarea, no name, no label, no placeholder."""
    return FormDescriptor(
        form_id=form_id,
        fields=[FormField(element_id="el_017", field_type="textarea")],
        submit_element_id="el_018",
    )


def _memory() -> RunMemory:
    return RunMemory(run_id=new_id(), start_url=BUZZ_URL)


# ===========================================================================
# A -- identity survives the page changing underneath it
# ===========================================================================


def test_the_same_form_keeps_one_signature_across_renumbering():
    """form_055 -> form_056 -> form_085 were all the same post box."""
    signatures = {form_signature(_buzz_form(fid), BUZZ_URL)
                  for fid in ("form_055", "form_056", "form_085")}
    assert len(signatures) == 1


def test_the_form_id_itself_is_not_the_signature():
    sig = form_signature(_buzz_form("form_055"), BUZZ_URL)
    assert "form_055" not in sig


def test_different_forms_on_a_page_stay_distinct():
    other = FormDescriptor(
        form_id="form_055",  # deliberately the SAME positional id
        fields=[FormField(element_id="el_100", field_type="text", label="Username")],
        submit_element_id="el_101",
    )
    assert form_signature(_buzz_form("form_055"), BUZZ_URL) != form_signature(other, BUZZ_URL)


def test_the_same_form_on_a_different_page_is_a_different_form():
    assert form_signature(_buzz_form("form_055"), BUZZ_URL) != form_signature(
        _buzz_form("form_055"), "https://opensource-demo.orangehrmlive.com/web/index.php/pim/addEmployee"
    )


def test_a_query_string_does_not_split_one_form_in_two():
    """A list filtered to ?page=2 offers the same create form as ?page=1.
    Treating those as different forms would reopen the loop."""
    assert form_signature(_buzz_form("form_055"), f"{BUZZ_URL}?page=1") == form_signature(
        _buzz_form("form_055"), f"{BUZZ_URL}?page=2"
    )


def test_a_named_field_is_preferred_over_a_positional_one():
    """`name` and `label` are chosen by the application; `element_id` is
    assigned by the same counter that makes `form_id` untrustworthy."""
    a = FormDescriptor(
        form_id="form_001",
        fields=[FormField(element_id="el_017", field_type="text", name="title")],
    )
    b = FormDescriptor(
        form_id="form_009",
        fields=[FormField(element_id="el_444", field_type="text", name="title")],
    )
    assert form_signature(a, BUZZ_URL) == form_signature(b, BUZZ_URL)


# ===========================================================================
# B -- a finished form is a finished form, success or not
# ===========================================================================


class _Workflow:
    def __init__(self, signature: str, state: str):
        self.form_signature = signature
        self.state = state


def test_a_verified_workflow_marks_the_form_finished():
    memory = _memory()
    sig = form_signature(_buzz_form("form_055"), BUZZ_URL)
    memory.note_form_workflow_finished(_Workflow(sig, "verified"))

    assert memory.has_finished_form(sig)


def test_the_form_is_still_finished_after_it_is_renumbered():
    """The exact failure: the submit succeeded, the feed grew, the form became
    form_056, and the run treated it as a form it had never seen."""
    memory = _memory()
    memory.note_form_workflow_finished(
        _Workflow(form_signature(_buzz_form("form_055"), BUZZ_URL), "verified")
    )

    assert memory.has_finished_form(form_signature(_buzz_form("form_056"), BUZZ_URL))
    assert memory.has_finished_form(form_signature(_buzz_form("form_085"), BUZZ_URL))


def test_every_terminal_state_counts_as_finished():
    for state in ("verified", "failed", "blocked", "skipped", "submitted_unverified"):
        memory = _memory()
        memory.note_form_workflow_finished(_Workflow(f"form:{state}", state))
        assert memory.has_finished_form(f"form:{state}"), state


def test_a_workflow_without_a_signature_records_nothing():
    """Silently recording an empty key would mark EVERY form finished."""
    memory = _memory()
    memory.note_form_workflow_finished(_Workflow("", "verified"))

    assert memory.finished_form_signatures == set()
    assert not memory.has_finished_form("")


def test_an_untouched_form_is_not_finished():
    """An empty result must mean "not done", never "not measured"."""
    assert not _memory().has_finished_form(form_signature(_buzz_form("form_055"), BUZZ_URL))


# ===========================================================================
# C -- the frontier stops offering it
# ===========================================================================


def test_the_frontier_checks_the_signature_not_the_form_id():
    import inspect

    from app.agent.frontier import FrontierBuilder

    source = inspect.getsource(FrontierBuilder)
    assert "memory.has_finished_form(form_signature(form, page.url))" in source


def test_the_workflow_carries_a_signature_from_the_moment_it_starts():
    from app.agent.form_workflow import GenericFormWorkflow

    wf = GenericFormWorkflow.start(
        _buzz_form("form_055"),
        page_url=BUZZ_URL,
        heading="Buzz",
        interactive_elements=[],
        run_id="run_1",
    )
    if wf is None:  # purpose inference declined this form; nothing to assert
        return
    assert wf.form_signature == form_signature(_buzz_form("form_055"), BUZZ_URL)


def test_both_terminal_paths_record_the_finish():
    """A workflow can end in the controller (after a submit) or in the planner
    (when next_action forces a terminal state without producing an action).
    Only one of those recording it leaves the loop half-open."""
    import inspect

    from app.agent.controller import AgentController
    from app.agent.planner import Planner

    assert "note_form_workflow_finished" in inspect.getsource(AgentController.run)
    assert "note_form_workflow_finished" in inspect.getsource(Planner)


def test_the_identity_rule_has_exactly_one_implementation():
    """Four bugs this session came from a rule with two homes and only one on
    the path being measured."""
    root = BACKEND / "app"
    definitions = [
        f"{p.relative_to(root)}:{i}"
        for p in root.rglob("*.py")
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if line.startswith("def form_signature(")
    ]
    assert len(definitions) == 1, definitions
