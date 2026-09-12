"""Generic safe form workflow (Phase 7): classification, data planning, step
sequencing, and dispatch through the unified frontier/goal system."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.auth_strategy import AuthenticationStrategy  # noqa: E402
from app.agent.form_workflow import FORM_WORKFLOW_STATES, GenericFormWorkflow, infer_form_purpose  # noqa: E402
from app.agent.frontier import FrontierBuilder  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.agent.planner import Planner  # noqa: E402
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.schemas import ActionType, FormDescriptor, FormField, InteractiveElement, PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402


def _checkout_form() -> FormDescriptor:
    return FormDescriptor(
        form_id="checkout_form",
        fields=[
            FormField(element_id="el_first", label="First Name", field_type="text", required=True),
            FormField(element_id="el_last", label="Last Name", field_type="text", required=True),
            FormField(element_id="el_zip", label="Zip Code", field_type="text", required=True),
        ],
        submit_element_id="el_continue",
    )


def _customer_form() -> FormDescriptor:
    return FormDescriptor(
        form_id="customer_form",
        fields=[
            FormField(element_id="el_name", label="Customer Name", field_type="text", required=True),
            FormField(element_id="el_plan", label="Plan", field_type="select", options=["Basic", "Pro"]),
            FormField(element_id="el_active", label="Active", field_type="checkbox"),
        ],
        submit_element_id="el_save",
    )


def _checkout_form_with_mis_detected_submit() -> tuple[FormDescriptor, list[InteractiveElement]]:
    """Mirrors a real bug found live on SauceDemo: the browser layer's own
    submit-detection heuristic recorded submit_element_id="el_cancel" (a Cancel
    button) instead of the real submit control, a separate <input type="submit"
    value="Continue">. Filling the form then correctly clicking "Cancel" would
    silently discard the whole checkout instead of advancing it."""
    form = FormDescriptor(
        form_id="checkout_form",
        fields=[
            FormField(element_id="el_first", label="First Name", field_type="text", required=True),
            FormField(element_id="el_last", label="Last Name", field_type="text", required=True),
            FormField(element_id="el_zip", label="Zip Code", field_type="text", required=True),
        ],
        submit_element_id="el_cancel",
    )
    elements = [
        InteractiveElement(
            element_id="el_cancel",
            tag="button",
            role="button",
            category="button",
            accessible_name="Cancel",
            text="Cancel",
            is_visible=True,
            is_enabled=True,
        ),
        InteractiveElement(
            element_id="el_continue",
            tag="input",
            input_type="submit",
            category="input",
            current_value="Continue",
            is_visible=True,
            is_enabled=True,
        ),
    ]
    return form, elements


def _delete_form() -> FormDescriptor:
    return FormDescriptor(
        form_id="delete_form",
        fields=[FormField(element_id="el_confirm", label="Confirm", field_type="text")],
        submit_element_id="el_delete_btn",
    )


def _delete_form_submit_element() -> InteractiveElement:
    # The submit control's real label lives on the page's interactive elements, not
    # on FormDescriptor.fields (that's just the input fields).
    return InteractiveElement(
        element_id="el_delete_btn",
        tag="button",
        role="button",
        category="button",
        accessible_name="Delete Account",
        text="Delete Account",
        is_visible=True,
        is_enabled=True,
    )


def _page_with_form(form: FormDescriptor, url: str = "https://example.com/checkout") -> PageState:
    return PageState(page_id=new_id(), url=url, title="Checkout", headings=["Checkout"], forms=[form])


def test_infer_form_purpose_checkout():
    assert infer_form_purpose(_checkout_form(), page_url="https://example.com/checkout-step-one.html") == "checkout_information"


def test_infer_form_purpose_generic_create():
    assert infer_form_purpose(_customer_form(), page_url="https://example.com/customers/new") == "safe_test_data_creation"


def test_start_refuses_form_with_disallowed_submit_label():
    wf = GenericFormWorkflow.start(
        _delete_form(),
        page_url="https://example.com/account",
        interactive_elements=[_delete_form_submit_element()],
    )
    assert wf is None


def test_step_budget_forces_failure_if_the_same_field_never_stops_being_offered():
    """Regression: a live ServiceFlow run got stuck refilling the same field (el_008)
    for 30+ iterations straight — total_steps must bound the workflow to its own
    field count plus a little retry slack, regardless of why it isn't converging
    (fields never actually reach filled_element_ids, a stale page re-render, etc.),
    so it can never hold up an entire run indefinitely."""
    form = _checkout_form()
    wf = GenericFormWorkflow.start(form, page_url="https://example.com/checkout-step-one.html")
    assert wf is not None

    # Simulate a step that never gets marked filled (e.g. because the real page kept
    # re-rendering with the same id but the workflow's own bookkeeping never caught
    # up) by repeatedly clearing filled_element_ids after every step.
    for _ in range(50):
        action = wf.next_action()
        if wf.state == "failed":
            break
        wf.filled_element_ids.clear()

    assert wf.state == "failed"
    assert wf.last_error == "step_budget_exceeded"
    assert wf.total_steps <= len(form.fields) + wf.max_attempts + 3


def test_continue_form_workflow_clears_active_workflow_when_step_budget_exceeded():
    memory = _memory()
    page = _page_with_form(_checkout_form(), url="https://example.com/checkout-step-one.html")
    planner = Planner(MockGemmaProvider())
    context = {"allow_safe_test_data_creation": True}

    inspect_action = planner._select_auth_candidate(page, memory, context)
    memory.auth_strategy.note_form_inspected(inspect_action.element_id)
    planner._select_auth_candidate(page, memory, context)  # starts the workflow
    assert memory.active_form_workflow is not None

    wf = memory.active_form_workflow
    wf.total_steps = len(wf.fields) + wf.max_attempts + 10  # already over budget
    action = planner._candidate_to_action(
        _fake_continue_candidate(wf.workflow_id), page, memory, memory.auth_strategy, context
    )
    assert action is None
    assert memory.active_form_workflow is None


def _fake_continue_candidate(workflow_id: str):
    from app.agent.frontier import FrontierCandidate

    return FrontierCandidate(
        candidate_id=f"continue_form_{workflow_id}",
        candidate_type="continue_form_workflow",
        action="continue_form_workflow",
        workflow_id=workflow_id,
    )


def test_full_lifecycle_fills_all_fields_then_submits_then_verifies():
    form = _checkout_form()
    wf = GenericFormWorkflow.start(form, page_url="https://example.com/checkout-step-one.html")
    assert wf is not None
    assert wf.state == "classified"

    seen_elements = []
    action = wf.next_action()
    while action is not None and action.action != ActionType.CLICK:
        seen_elements.append(action.element_id)
        assert action.action == ActionType.FILL
        assert action.value  # a real, non-empty positive value was planned
        action = wf.next_action()

    assert set(seen_elements) == {"el_first", "el_last", "el_zip"}
    assert action is not None
    assert action.action == ActionType.CLICK
    assert action.element_id == "el_continue"
    assert wf.state == "submitted"

    wf.note_result(success=True)
    assert wf.state == "verified"


def test_workflow_does_not_submit_via_a_mis_detected_cancel_button():
    form, elements = _checkout_form_with_mis_detected_submit()
    wf = GenericFormWorkflow.start(form, page_url="https://example.com/checkout-step-one.html", interactive_elements=elements)
    assert wf is not None
    assert wf.submit_element_id == "el_continue", (
        "must prefer the real <input type=submit> over a recorded submit_element_id "
        "that resolves to a 'Cancel' button"
    )

    action = wf.next_action()
    while action is not None and action.action != ActionType.CLICK:
        action = wf.next_action()
    assert action is not None
    assert action.element_id == "el_continue"
    assert action.element_id != "el_cancel"


def test_choice_and_toggle_fields_get_real_values_not_generic_text():
    form = _customer_form()
    wf = GenericFormWorkflow.start(form, page_url="https://example.com/customers/new")
    wf.plan_data()
    assert wf.planned_values["el_plan"] == "Basic"
    assert wf.planned_values["el_active"] == "true"


def test_failed_submission_retries_then_gives_up_after_max_attempts():
    wf = GenericFormWorkflow.start(_checkout_form(), page_url="https://example.com/checkout-step-one.html")
    for _ in range(wf.max_attempts):
        wf.note_result(success=False)
    assert wf.state == "failed"


def test_all_states_are_within_canonical_vocabulary():
    wf = GenericFormWorkflow.start(_checkout_form(), page_url="https://example.com/checkout-step-one.html")
    assert wf.state in FORM_WORKFLOW_STATES
    wf.plan_data()
    assert wf.state in FORM_WORKFLOW_STATES


def _memory() -> RunMemory:
    memory = RunMemory(run_id=new_id(), start_url="https://example.com/")
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = True
    memory.remaining_action_budget = 30
    return memory


def test_frontier_offers_start_form_workflow_candidate_when_authenticated():
    memory = _memory()
    page = _page_with_form(_customer_form(), url="https://example.com/customers/new")
    cands = FrontierBuilder(memory.auth_strategy).build(
        page, allow_safe_test_data=True, memory=memory
    )
    assert any(c.candidate_type == "start_form_workflow" for c in cands)


def test_frontier_does_not_offer_start_form_workflow_without_the_safety_flag():
    memory = _memory()
    page = _page_with_form(_customer_form(), url="https://example.com/customers/new")
    cands = FrontierBuilder(memory.auth_strategy).build(
        page, allow_safe_test_data=False, memory=memory
    )
    assert not any(c.candidate_type == "start_form_workflow" for c in cands)


def test_planner_starts_and_continues_workflow_through_full_dispatch():
    memory = _memory()
    page = _page_with_form(_checkout_form(), url="https://example.com/checkout-step-one.html")
    planner = Planner(MockGemmaProvider())
    context = {"allow_safe_test_data_creation": True}

    # A freshly-discovered form is inspected (priority 20) before the safe-fill
    # workflow (priority 26) ever starts on it — record structure first, then fill.
    inspect_action = planner._select_auth_candidate(page, memory, context)
    assert inspect_action is not None
    assert inspect_action.action == ActionType.INSPECT_FORM
    memory.auth_strategy.note_form_inspected(inspect_action.element_id)

    action1 = planner._select_auth_candidate(page, memory, context)
    assert action1 is not None
    assert action1.action == ActionType.FILL
    assert memory.active_form_workflow is not None

    action2 = planner._select_auth_candidate(page, memory, context)
    assert action2 is not None
    assert action2.element_id != action1.element_id


def test_active_workflow_goal_merges_start_and_continue_under_one_goal():
    memory = _memory()
    page = _page_with_form(_checkout_form(), url="https://example.com/checkout-step-one.html")
    planner = Planner(MockGemmaProvider())
    context = {"allow_safe_test_data_creation": True}

    inspect_action = planner._select_auth_candidate(page, memory, context)
    memory.auth_strategy.note_form_inspected(inspect_action.element_id)

    planner._select_auth_candidate(page, memory, context)  # starts the workflow
    continue_goals_after_start = [g for g in memory.goals if g.goal_type == "continue_workflow"]
    assert len(continue_goals_after_start) == 1

    planner._select_auth_candidate(page, memory, context)  # continues it
    continue_goals_after_continue = [g for g in memory.goals if g.goal_type == "continue_workflow"]
    assert len(continue_goals_after_continue) == 1
    assert continue_goals_after_continue[0].goal_id == continue_goals_after_start[0].goal_id


def test_failed_form_is_not_retried_after_its_workflow_fails():
    """Regression: a live ServiceFlow run never escaped its Settings page because a
    form that could never actually converge got a brand-new workflow started for it
    immediately after each individually-bounded failure — an outer infinite
    start -> fail -> restart loop. Once a form's workflow has failed/blocked once,
    start_form_workflow must stop being offered for that same form_id."""
    memory = _memory()
    form = _checkout_form()
    page = _page_with_form(form, url="https://example.com/checkout-step-one.html")

    cands_before = FrontierBuilder(memory.auth_strategy).build(page, allow_safe_test_data=True, memory=memory)
    assert any(c.candidate_type == "start_form_workflow" for c in cands_before)

    memory.failed_form_workflow_counts[form.form_id] += 1
    cands_after = FrontierBuilder(memory.auth_strategy).build(page, allow_safe_test_data=True, memory=memory)
    assert not any(c.candidate_type == "start_form_workflow" for c in cands_after)


def test_controller_style_failure_records_form_id_for_no_retry():
    """Mirrors controller.py's post-action bookkeeping: a submit that keeps failing
    until max_attempts must mark the form_id so it isn't retried."""
    memory = _memory()
    form = _checkout_form()
    wf = GenericFormWorkflow.start(form, page_url="https://example.com/checkout-step-one.html")
    for _ in range(wf.max_attempts):
        wf.note_result(success=False)
    assert wf.state == "failed"

    if wf.state in {"failed", "blocked", "skipped"}:
        memory.failed_form_workflow_counts[wf.form_id] += 1
    assert memory.failed_form_workflow_counts[form.form_id] == 1
