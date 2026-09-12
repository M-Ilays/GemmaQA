"""The record lifecycle: created -> verified -> updated -> deleted.

Every gap here was found by running GemmaQA against a live record-management
application. The registry defined the whole lifecycle and enforced its
transitions, but nothing ever advanced a record past `verified`, because three
separate components assumed a record's update/delete controls live in the
COLLECTION that lists it. On that application — and on most — they live on the
record's own detail page.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.cleanup_planner import plan_cleanup_action  # noqa: E402
from app.agent.form_workflow import GenericFormWorkflow, infer_form_purpose  # noqa: E402
from app.agent.temporary_record_registry import CleanupPlan, TemporaryRecordRegistry  # noqa: E402
from app.intelligence.crud_discovery import CRUDDiscoveryEngine  # noqa: E402
from app.intelligence.crud_discovery.crud_candidate_builder import CRUDCandidateBuilder  # noqa: E402
from app.safety.policies import CLEANUP_RISK_CLASS  # noqa: E402
from app.perception.models import (  # noqa: E402
    ActionSemantics,
    CanonicalPageModel,
    CollectionAction,
    CollectionRow,
    DialogDescriptor,
    HeadingDescriptor,
    RecordCollection,
    TextBlockDescriptor,
)
from app.schemas import FormDescriptor, FormField  # noqa: E402

IDENTITY = "gemmaqa_test_ab12_9@example.com"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _detail_model(*, with_delete: bool = True, identity: str | None = IDENTITY) -> CanonicalPageModel:
    """A record-detail state: no collection, per-record controls present."""
    semantics = [ActionSemantics(element_id="el_002", semantic_action="edit")]
    if with_delete:
        semantics.append(ActionSemantics(element_id="el_003", semantic_action="delete"))
    blocks = []
    if identity:
        blocks.append(TextBlockDescriptor(stable_id="t1", text=f"Email: {identity}"))
    return CanonicalPageModel(
        url="https://app.example.com/recordDetails",
        state_fingerprint="fp_detail",
        headings=[HeadingDescriptor(stable_id="h1", text="Record Details")],
        text_blocks=blocks,
        action_semantics=semantics,
    )


def _list_model() -> CanonicalPageModel:
    return CanonicalPageModel(
        url="https://app.example.com/records",
        state_fingerprint="fp_list",
        collections=[
            RecordCollection(
                stable_id="table_003",
                element_id="table_003",
                collection_type="native_table",
                row_count=1,
                visible_rows=[
                    CollectionRow(stable_id="row_0", element_id="el_008", row_index=0, cell_values=["Ada", IDENTITY])
                ],
                global_actions=[
                    CollectionAction(stable_id="el_002", element_id="el_002", scope="global", semantic_action="add")
                ],
            )
        ],
    )


def _prefilled_form() -> FormDescriptor:
    return FormDescriptor(
        form_id="form_015",
        submit_element_id="el_013",
        fields=[
            FormField(name=None, field_type="text", label="* First Name:", element_id="el_002", current_value="Ada"),
            FormField(name=None, field_type="text", label="* Last Name:", element_id="el_003", current_value="Lovelace"),
            FormField(name=None, field_type="text", label="Email:", element_id="el_005", current_value=IDENTITY),
            FormField(name=None, field_type="text", label="City:", element_id="el_009", current_value="Springfield"),
        ],
    )


def _empty_form() -> FormDescriptor:
    return FormDescriptor(
        form_id="form_015",
        submit_element_id="el_013",
        fields=[
            FormField(name=None, field_type="text", label="* First Name:", element_id="el_002"),
            FormField(name=None, field_type="text", label="* Last Name:", element_id="el_003"),
            FormField(name=None, field_type="text", label="Email:", element_id="el_005"),
            FormField(name=None, field_type="text", label="City:", element_id="el_009"),
        ],
    )


def _registry(state: str = "verified") -> TemporaryRecordRegistry:
    registry = TemporaryRecordRegistry("run_1")
    entry = registry.register_created(record_type="email", generated_identity=IDENTITY)
    if state != "created":
        registry.mark_verified(entry.temporary_record_id, evidence=["list_row"])
    return registry


# ===========================================================================
# A — update and delete become DISCOVERED from a record-detail state
# ===========================================================================


def test_detail_state_update_and_delete_controls_are_discovered():
    """They were invisible: every CRUD hypothesis required the control to hang
    off a collection, so a detail page's Edit/Delete produced nothing."""
    hypotheses = CRUDCandidateBuilder().build_entry_points(_detail_model(), current_actor_term="current session")
    by_operation = {h.operation: h for h in hypotheses}

    assert "edit" in by_operation
    assert "delete" in by_operation
    assert by_operation["edit"].source_state == "record_detail"
    assert by_operation["edit"].required_controls == ["el_002"]
    assert by_operation["delete"].required_controls == ["el_003"]


def test_a_detail_state_control_is_only_an_entry_point():
    """A control existing is not proof the operation works — the same discipline
    the collection path already followed."""
    for h in CRUDCandidateBuilder().build_entry_points(_detail_model()):
        assert h.status == "entry_point"
        assert h.confidence <= 0.3


def test_a_detail_state_delete_is_classified_destructive():
    delete = next(h for h in CRUDCandidateBuilder().build_entry_points(_detail_model()) if h.operation == "delete")
    assert delete.safety_classification == "destructive"
    assert delete.cleanup_possibility == "not_possible"


def test_a_list_state_produces_no_detail_hypotheses():
    """A page WITH a collection is a list; the collection path owns it."""
    hypotheses = CRUDCandidateBuilder().build_entry_points(_list_model())
    assert all(h.source_state != "record_detail" for h in hypotheses)


def test_clicking_edit_and_getting_a_form_makes_the_hypothesis_supported():
    after = _detail_model()
    after.forms = [_prefilled_form()]
    hypotheses = CRUDCandidateBuilder().build_fulfilled(
        _detail_model(), after, executed_element_id="el_002", action_succeeded=True
    )
    assert len(hypotheses) == 1
    assert hypotheses[0].operation == "edit"
    assert hypotheses[0].status == "supported"
    assert hypotheses[0].required_form_id == "form_015"
    kinds = {e.source_kind for e in hypotheses[0].evidence}
    assert "field_prepopulation" in kinds  # the form arrived prefilled


def test_clicking_edit_with_no_form_appearing_is_not_supported():
    hypotheses = CRUDCandidateBuilder().build_fulfilled(
        _detail_model(), _detail_model(), executed_element_id="el_002", action_succeeded=True
    )
    assert hypotheses == []


def test_clicking_delete_is_never_advanced_past_entry_point_by_the_click_alone():
    """The commissioned constraint: a delete needs a confirmation dialog or an
    observed row-count decrease, never control evidence alone."""
    hypotheses = CRUDCandidateBuilder().build_fulfilled(
        _detail_model(), _detail_model(), executed_element_id="el_003", action_succeeded=True
    )
    assert hypotheses == []


def test_the_engine_registers_detail_state_operations():
    engine = CRUDDiscoveryEngine()
    after = _detail_model()
    after.forms = [_prefilled_form()]
    engine.observe(
        before_model=_detail_model(), after_model=after,
        executed_element_id="el_002", action_succeeded=True,
    )
    operations = {h.operation for h in engine.registry.records.values()}
    assert "edit" in operations
    assert "delete" in operations


# ===========================================================================
# B — a prefilled form is an update, not a create
# ===========================================================================


def test_a_prefilled_form_is_classified_as_an_update():
    assert infer_form_purpose(_prefilled_form(), page_url="https://app.example.com/edit") == (
        "safe_test_data_update"
    )


def test_an_empty_form_is_still_a_create():
    assert infer_form_purpose(_empty_form(), page_url="https://app.example.com/add") == (
        "safe_test_data_creation"
    )


def test_one_defaulted_field_does_not_make_a_create_form_an_update():
    form = _empty_form()
    form.fields[3].current_value = "Springfield"  # a remembered city
    assert infer_form_purpose(form) == "safe_test_data_creation"


def test_an_update_changes_exactly_one_field():
    """Overwriting every field answers a weaker question and costs one action
    per field — the difference between 2 actions and 12 on a wide form."""
    wf = GenericFormWorkflow.start(_prefilled_form(), page_url="https://app.example.com/edit", run_id="run")
    assert wf is not None
    wf.plan_data()
    assert wf.purpose == "safe_test_data_update"
    assert len(wf.planned_values) == 1
    assert wf.updated_field_element_id in wf.planned_values


def test_the_changed_field_records_a_real_before_and_after():
    wf = GenericFormWorkflow.start(_prefilled_form(), page_url="https://app.example.com/edit", run_id="run")
    wf.plan_data()
    assert wf.updated_field_before_value
    assert wf.updated_field_after_value
    assert wf.updated_field_after_value != wf.updated_field_before_value


def test_an_update_never_changes_an_identity_or_format_constrained_field():
    """Changing a unique-constrained email tests uniqueness, not updating; and a
    broken format is not an update failure."""
    wf = GenericFormWorkflow.start(_prefilled_form(), page_url="https://app.example.com/edit", run_id="run")
    wf.plan_data()
    assert wf.updated_field_element_id != "el_005"  # the email field
    assert IDENTITY not in wf.planned_values.values()


def test_an_update_with_nothing_safely_changeable_is_blocked_not_guessed():
    form = FormDescriptor(
        form_id="form_x",
        submit_element_id="el_9",
        fields=[
            FormField(name=None, field_type="text", label="Email:", element_id="el_1", current_value=IDENTITY),
            FormField(name=None, field_type="text", label="Phone:", element_id="el_2", current_value="5550100"),
        ],
    )
    wf = GenericFormWorkflow.start(form, page_url="https://app.example.com/edit", run_id="run")
    wf.plan_data()
    assert wf.state == "blocked"
    assert wf.last_error == "no_safely_updatable_field"


def test_an_update_costs_two_actions():
    wf = GenericFormWorkflow.start(_prefilled_form(), page_url="https://app.example.com/edit", run_id="run")
    actions = []
    while True:
        action = wf.next_action()
        if action is None:
            break
        actions.append(action.action.value)
        if action.action.value == "click":
            break
    assert actions == ["fill", "click"]


# ===========================================================================
# C — cleanup can find a delete control on a detail page
# ===========================================================================


def test_cleanup_uses_a_detail_page_delete_control():
    """Cleanup previously required a collection row action, so it could never
    advance on an application that puts delete on the record's own page."""
    registry = _registry()
    entry = next(iter(registry.entries.values()))
    action = plan_cleanup_action(entry, canonical_model=_detail_model())
    assert action is not None
    assert action.element_id == "el_003"
    assert action.metadata["cleanup_step"] == "delete_control"
    assert action.metadata["risk_class"] == CLEANUP_RISK_CLASS
    assert action.metadata["cleanup_temporary_record_id"] == entry.temporary_record_id


def test_cleanup_refuses_a_detail_page_that_does_not_show_this_record():
    """THE safety guard: a detail page's single delete button proves nothing
    about WHICH record is open, so the record's identity must be visible."""
    registry = _registry()
    entry = next(iter(registry.entries.values()))
    assert plan_cleanup_action(entry, canonical_model=_detail_model(identity=None)) is None


def test_cleanup_refuses_a_detail_page_showing_a_different_record():
    registry = _registry()
    entry = next(iter(registry.entries.values()))
    other = _detail_model(identity="someone_else@example.com")
    assert plan_cleanup_action(entry, canonical_model=other) is None


def test_cleanup_prefers_the_collection_row_control_when_one_exists():
    """A row-level delete is record-scoped by construction, so it wins."""
    model = _list_model()
    model.collections[0].row_actions = [
        CollectionAction(
            stable_id="el_del", element_id="el_del", scope="row", row_id="row_0", semantic_action="delete"
        )
    ]
    registry = _registry()
    entry = next(iter(registry.entries.values()))
    action = plan_cleanup_action(entry, canonical_model=model)
    assert action is not None
    assert action.element_id == "el_del"


def test_cleanup_on_a_list_page_without_a_row_delete_stays_pending():
    registry = _registry()
    entry = next(iter(registry.entries.values()))
    assert plan_cleanup_action(entry, canonical_model=_list_model()) is None


def test_cleanup_finds_detail_delete_when_fields_look_like_a_collection():
    """Contact List /contactDetails stacks labeled fields in similar <p> tags.
    Perception extracts that as div_row_group. Treating ANY collection as a
    list state hid Delete Contact, so run 55588f75 screenshotted and FINISH'd
    with the record still on the page."""
    registry = _registry()
    entry = next(iter(registry.entries.values()))
    model = _detail_model()
    model.collections = [
        RecordCollection(
            stable_id="collection_001",
            element_id="collection_001",
            collection_type="div_row_group",
            row_count=4,
            visible_rows=[
                CollectionRow(
                    stable_id="row_0",
                    element_id="el_010",
                    row_index=0,
                    cell_values=["First Name:", "Ada"],
                ),
                CollectionRow(
                    stable_id="row_1",
                    element_id="el_011",
                    row_index=1,
                    cell_values=["Email:", IDENTITY],
                ),
            ],
        )
    ]
    action = plan_cleanup_action(entry, canonical_model=model)
    assert action is not None
    assert action.element_id == "el_003"
    assert action.metadata["cleanup_step"] == "delete_control"


def test_a_detail_delete_is_still_marked_high_risk_for_the_safety_gate():
    from app.schemas import RiskLevel

    registry = _registry()
    entry = next(iter(registry.entries.values()))
    action = plan_cleanup_action(entry, canonical_model=_detail_model())
    assert action.risk == RiskLevel.HIGH


@pytest.mark.asyncio
async def test_planner_clicks_delete_instead_of_waiting_on_gemma():
    """Run cda6bc09 finished edit, then spent ~120s in rank_goals + generate_action
    and FINISH, so Delete Contact never appeared in the activity log."""
    from app.agent.auth_strategy import AuthenticationStrategy
    from app.agent.memory import RunMemory
    from app.agent.planner import Planner
    from app.gemma.mock_provider import MockGemmaProvider
    from app.schemas import ActionType, PageState, RunConfiguration
    from app.utils.ids import new_id

    class BoomGemma(MockGemmaProvider):
        async def rank_goals(self, *args, **kwargs):
            raise AssertionError("rank_goals must not run when cleanup is ready")

        async def generate_action(self, *args, **kwargs):
            raise AssertionError("generate_action must not run when cleanup is ready")

    memory = RunMemory(run_id=new_id(), start_url="https://app.example.com/")
    memory.configuration = RunConfiguration(allow_destructive_actions=True)
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = True
    memory.temporary_record_registry = _registry()
    entry = next(iter(memory.temporary_record_registry.entries.values()))
    memory.temporary_record_registry.mark_updated(
        entry.temporary_record_id,
        field_key="firstName",
        before_value="Jordan",
        after_value="Riley",
        verified=True,
        evidence=["edit_submitted"],
    )
    memory.canonical_page_model = _detail_model()
    page = PageState(
        page_id=new_id(),
        url="https://app.example.com/recordDetails",
        title="",
        state_fingerprint="fp_detail",
        interactive_elements=[],
    )
    planner = Planner(BoomGemma())
    action = await planner.next_action(
        page_state=page,
        previous_actions=[],
        unexplored=[],
        context={"allow_destructive_actions": True, "safe_mode": False, "authorized_domain": "app.example.com"},
        memory=memory,
    )
    assert action.action == ActionType.CLICK
    assert action.element_id == "el_003"
    assert (action.metadata or {}).get("cleanup_step") == "delete_control"


@pytest.mark.asyncio
async def test_planner_finishes_without_gemma_when_delete_retries_are_exhausted():
    """Run 1efb5323 clicked Delete three times (native confirm dismissed), then
    waited ~122s in generate_action because the record was
    `manual_cleanup_required` and no longer counted as pending cleanup."""
    from app.agent.auth_strategy import AuthenticationStrategy
    from app.agent.memory import RunMemory
    from app.agent.planner import Planner
    from app.agent.temporary_record_registry import MAX_CLEANUP_ATTEMPTS
    from app.gemma.mock_provider import MockGemmaProvider
    from app.schemas import ActionType, PageState, RunConfiguration
    from app.utils.ids import new_id

    class BoomGemma(MockGemmaProvider):
        async def rank_goals(self, *args, **kwargs):
            raise AssertionError("rank_goals must not run after cleanup is exhausted")

        async def generate_action(self, *args, **kwargs):
            raise AssertionError("generate_action must not run after cleanup is exhausted")

    memory = RunMemory(run_id=new_id(), start_url="https://app.example.com/")
    memory.configuration = RunConfiguration(allow_destructive_actions=True)
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = True
    memory.temporary_record_registry = _registry()
    entry = next(iter(memory.temporary_record_registry.entries.values()))
    memory.temporary_record_registry.mark_updated(
        entry.temporary_record_id,
        field_key="firstName",
        before_value="Jordan",
        after_value="Riley",
        verified=True,
        evidence=["edit_submitted"],
    )
    for _ in range(MAX_CLEANUP_ATTEMPTS):
        memory.temporary_record_registry.mark_cleanup_failed(
            entry.temporary_record_id,
            error="Delete click did not remove the record from the page",
        )
    assert entry.current_state == "manual_cleanup_required"
    memory.canonical_page_model = _detail_model()
    page = PageState(
        page_id=new_id(),
        url="https://app.example.com/recordDetails",
        title="",
        state_fingerprint="fp_detail",
        interactive_elements=[],
    )
    planner = Planner(BoomGemma())
    action = await planner.next_action(
        page_state=page,
        previous_actions=[],
        unexplored=[],
        context={"allow_destructive_actions": True, "safe_mode": False, "authorized_domain": "app.example.com"},
        memory=memory,
    )
    assert action.action == ActionType.FINISH
    assert (action.metadata or {}).get("stop_reason_code") == "exploration_complete"


def test_only_authorized_cleanup_clicks_accept_the_native_confirm():
    """Contact List Delete Contact uses window.confirm. Playwright dismisses
    unhandled dialogs, which is why three 'successful' clicks left the record
    on /contactDetails (run 1efb5323). Accept that confirm only for cleanup."""
    from app.browser.executor import cleanup_click_accepts_native_dialog

    assert cleanup_click_accepts_native_dialog(
        _high_risk_click(
            risk_class=CLEANUP_RISK_CLASS,
            cleanup_temporary_record_id="tr_1",
            cleanup_step="delete_control",
        )
    )
    assert cleanup_click_accepts_native_dialog(
        _high_risk_click(
            risk_class=CLEANUP_RISK_CLASS,
            cleanup_temporary_record_id="tr_1",
            cleanup_step="confirm_delete",
        )
    )
    assert not cleanup_click_accepts_native_dialog(_high_risk_click())
    assert not cleanup_click_accepts_native_dialog(
        _high_risk_click(
            risk_class=CLEANUP_RISK_CLASS,
            cleanup_temporary_record_id="tr_1",
            cleanup_step="open_record",
        )
    )


def test_live_cleanup_waits_until_the_record_was_updated():
    """Run 51b62f6c opened the new contact and deleted it before Edit Contact."""
    from app.agent.auth_strategy import AuthenticationStrategy
    from app.agent.memory import RunMemory
    from app.agent.planner import Planner
    from app.gemma.mock_provider import MockGemmaProvider
    from app.schemas import PageState, RunConfiguration
    from app.utils.ids import new_id

    memory = RunMemory(run_id=new_id(), start_url="https://app.example.com/")
    memory.configuration = RunConfiguration(allow_destructive_actions=True)
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = True
    memory.temporary_record_registry = _registry()
    memory.canonical_page_model = _detail_model()
    page = PageState(
        page_id=new_id(),
        url="https://app.example.com/recordDetails",
        title="",
        state_fingerprint="fp_detail",
        interactive_elements=[],
    )
    planner = Planner(MockGemmaProvider())
    ctx = {"allow_destructive_actions": True, "safe_mode": False, "authorized_domain": "app.example.com"}
    assert planner.next_cleanup_action(page, memory, ctx) is None

    entry = next(iter(memory.temporary_record_registry.entries.values()))
    memory.temporary_record_registry.mark_updated(
        entry.temporary_record_id,
        field_key="firstName",
        before_value="Jordan",
        after_value="Riley",
        verified=True,
        evidence=["edit_submitted"],
    )
    action = planner.next_cleanup_action(page, memory, ctx)
    assert action is not None
    assert action.element_id == "el_003"


def test_delete_click_that_stays_on_the_record_page_is_not_counted():
    from app.agent.controller import AgentController
    from app.schemas import PageState
    from app.utils.ids import new_id

    entry = type("E", (), {"generated_identity": IDENTITY})()
    still_there = PageState(
        page_id=new_id(),
        url="https://thinking-tester-contact-list.herokuapp.com/contactDetails",
        title="",
        visible_text_summary=f"Email: {IDENTITY}",
    )
    gone = PageState(
        page_id=new_id(),
        url="https://thinking-tester-contact-list.herokuapp.com/contactList",
        title="My Contacts",
        visible_text_summary="No contacts",
    )
    assert AgentController._record_still_visible_after_delete(entry, still_there) is True
    assert AgentController._record_still_visible_after_delete(entry, gone) is False


def test_delete_click_that_lands_on_the_empty_list_confirms_absence():
    """Run b6509a34 deleted the contact (empty list, no table) but left the
    registry at `deleted` because absence required a collection. Coverage
    then showed delete 0 and the run logged in again."""
    from types import SimpleNamespace

    from app.agent.controller import AgentController
    from app.schemas import ActionCategory, ActionType, BrowserAction, PageState, RiskLevel
    from app.utils.ids import new_id

    registry = TemporaryRecordRegistry("run_1")
    entry = registry.register_created(
        record_type="email",
        generated_identity=IDENTITY,
        list_url="https://thinking-tester-contact-list.herokuapp.com/contactList",
    )
    registry.mark_verified(entry.temporary_record_id, evidence=["list_row"])
    registry.mark_updated(
        entry.temporary_record_id,
        field_key="firstName",
        before_value="Sam",
        after_value="Taylor",
        verified=True,
        evidence=["edit"],
    )
    action = BrowserAction(
        action=ActionType.CLICK,
        element_id="el_003",
        reason="Delete GemmaQA temporary test record",
        expected_result="The record is removed.",
        risk=RiskLevel.HIGH,
        category=ActionCategory.EXPLORATION,
        metadata={
            "cleanup_temporary_record_id": entry.temporary_record_id,
            "cleanup_step": "delete_control",
            "risk_class": CLEANUP_RISK_CLASS,
        },
    )
    after = PageState(
        page_id=new_id(),
        url="https://thinking-tester-contact-list.herokuapp.com/contactList",
        title="My Contacts",
        visible_text_summary="Add a New Contact Logout",
    )
    ctrl = AgentController.__new__(AgentController)
    ctrl.memory = SimpleNamespace(temporary_record_registry=registry)
    ctrl._advance_cleanup_from_live_action(action, after)
    assert entry.current_state == "absence_verified"
    assert entry.cleanup_result.succeeded is True


@pytest.mark.asyncio
async def test_planner_finishes_instead_of_logging_in_after_cleanup():
    """After delete, run b6509a34 clicked Logout then Submit on the login form."""
    from app.agent.auth_strategy import AuthenticationStrategy
    from app.agent.memory import RunMemory
    from app.agent.planner import Planner
    from app.gemma.mock_provider import MockGemmaProvider
    from app.schemas import ActionType, InteractiveElement, PageState, RunConfiguration
    from app.utils.ids import new_id

    class BoomGemma(MockGemmaProvider):
        async def rank_goals(self, *args, **kwargs):
            raise AssertionError("rank_goals must not run after cleanup")

        async def generate_action(self, *args, **kwargs):
            raise AssertionError("generate_action must not run after cleanup")

    registry = TemporaryRecordRegistry("run_1")
    entry = registry.register_created(
        record_type="email",
        generated_identity=IDENTITY,
        list_url="https://thinking-tester-contact-list.herokuapp.com/contactList",
    )
    registry.mark_verified(entry.temporary_record_id, evidence=["list_row"])
    registry.mark_updated(
        entry.temporary_record_id,
        field_key="firstName",
        before_value="Sam",
        after_value="Taylor",
        verified=True,
        evidence=["edit"],
    )
    registry.request_cleanup(
        entry.temporary_record_id, plan=CleanupPlan(delete_control_element_id="el_003")
    )
    registry.mark_delete_action_validated(entry.temporary_record_id)
    registry.mark_deleted(entry.temporary_record_id)
    registry.mark_absence_verified(entry.temporary_record_id)

    memory = RunMemory(run_id=new_id(), start_url="https://thinking-tester-contact-list.herokuapp.com/")
    memory.configuration = RunConfiguration(allow_destructive_actions=True)
    memory.auth_strategy = AuthenticationStrategy()
    memory.auth_strategy.authenticated = True
    memory.temporary_record_registry = registry
    page = PageState(
        page_id=new_id(),
        url="https://thinking-tester-contact-list.herokuapp.com/contactList",
        title="My Contacts",
        state_fingerprint="fp_empty_list",
        interactive_elements=[
            InteractiveElement(
                element_id="el_001",
                tag="button",
                accessible_name="Logout",
                text="Logout",
                is_visible=True,
                is_enabled=True,
            ),
        ],
    )
    planner = Planner(BoomGemma())
    action = await planner.next_action(
        page_state=page,
        previous_actions=[],
        unexplored=[],
        context={
            "allow_destructive_actions": True,
            "safe_mode": False,
            "authorized_domain": "thinking-tester-contact-list.herokuapp.com",
        },
        memory=memory,
    )
    assert action.action == ActionType.FINISH
    assert "logout" not in (action.reason or "").lower()


# ===========================================================================
# D — the lifecycle actually advances
# ===========================================================================


# ===========================================================================
# C2 — cleanup can walk BACK to the record
# ===========================================================================

LIST_URL = "https://app.example.com/records"


def _registry_with_list_url(state: str = "verified") -> TemporaryRecordRegistry:
    registry = TemporaryRecordRegistry("run_1")
    entry = registry.register_created(
        record_type="email", generated_identity=IDENTITY, list_url=LIST_URL
    )
    if state != "created":
        registry.mark_verified(entry.temporary_record_id, evidence=["list_row"])
    return registry


def _activatable_list_model() -> CanonicalPageModel:
    model = _list_model()
    model.collections[0].visible_rows[0].is_activatable = True
    model.collections[0].visible_rows[0].activation_basis = "structural_hypothesis"
    return model


def test_cleanup_navigates_back_to_the_list_when_the_run_ended_elsewhere():
    """The reason delete was discovered every run and performed in none: cleanup
    runs at the END, when the browser is somewhere unrelated."""
    from app.agent.cleanup_planner import plan_cleanup_navigation
    from app.schemas import ActionType

    entry = next(iter(_registry_with_list_url().entries.values()))
    action = plan_cleanup_navigation(
        entry, canonical_model=_detail_model(), current_url="https://app.example.com/addRecord"
    )
    assert action is not None
    assert action.action == ActionType.OPEN_URL
    assert action.url == LIST_URL
    assert action.metadata["cleanup_step"] == "navigate_to_list"


def test_cleanup_opens_the_row_matching_this_record_once_on_the_list():
    from app.agent.cleanup_planner import plan_cleanup_navigation
    from app.schemas import ActionType

    entry = next(iter(_registry_with_list_url().entries.values()))
    action = plan_cleanup_navigation(
        entry, canonical_model=_activatable_list_model(), current_url=LIST_URL
    )
    assert action is not None
    assert action.action == ActionType.CLICK
    assert action.element_id == "el_008"
    assert action.metadata["cleanup_step"] == "open_record"


def test_cleanup_never_opens_a_row_belonging_to_another_record():
    """Identity-matched, so this can never open 'whatever record is first'."""
    from app.agent.cleanup_planner import plan_cleanup_navigation

    model = _activatable_list_model()
    model.collections[0].visible_rows[0].cell_values = ["Someone", "other@example.com"]
    entry = next(iter(_registry_with_list_url().entries.values()))
    assert plan_cleanup_navigation(entry, canonical_model=model, current_url=LIST_URL) is None


def test_cleanup_navigation_is_read_only():
    from app.schemas import RiskLevel

    from app.agent.cleanup_planner import plan_cleanup_navigation

    entry = next(iter(_registry_with_list_url().entries.values()))
    for model, url in ((_detail_model(), "https://app.example.com/x"), (_activatable_list_model(), LIST_URL)):
        action = plan_cleanup_navigation(entry, canonical_model=model, current_url=url)
        assert action is not None
        assert action.risk == RiskLevel.LOW  # getting back to a record deletes nothing


def test_cleanup_does_not_navigate_when_already_on_the_list_with_no_matching_row():
    from app.agent.cleanup_planner import plan_cleanup_navigation

    entry = next(iter(_registry_with_list_url().entries.values()))
    # A list whose row is not activatable: nothing to click, so stay pending.
    assert plan_cleanup_navigation(entry, canonical_model=_list_model(), current_url=LIST_URL) is None


def test_cleanup_does_not_navigate_for_a_record_past_deletion():
    from app.agent.cleanup_planner import plan_cleanup_navigation

    registry = _registry_with_list_url()
    entry = next(iter(registry.entries.values()))
    registry.request_cleanup(entry.temporary_record_id, plan=CleanupPlan(delete_control_element_id="el_003"))
    assert plan_cleanup_navigation(entry, canonical_model=_detail_model(), current_url="https://x/") is None


def test_a_record_with_no_known_list_url_is_not_navigated_to_blindly():
    from app.agent.cleanup_planner import plan_cleanup_navigation

    entry = next(iter(_registry().entries.values()))  # registered without list_url
    assert plan_cleanup_navigation(entry, canonical_model=_list_model(), current_url="https://x/") is None


def test_only_the_delete_control_advances_the_lifecycle():
    """A navigation step must not mark the record cleanup_requested against a
    row or a URL instead of a delete control."""
    import inspect

    from app.agent import controller as controller_module

    source = inspect.getsource(controller_module.AgentController._run_cleanup_pass)
    assert 'step == "delete_control"' in source


def test_the_cleanup_pass_is_bounded():
    from app.agent.controller import (
        CLEANUP_MAX_NAVIGATIONS_PER_RECORD,
        CLEANUP_MAX_STEPS_PER_RECORD,
    )

    assert CLEANUP_MAX_NAVIGATIONS_PER_RECORD < CLEANUP_MAX_STEPS_PER_RECORD
    # list -> open record -> delete -> confirm must fit.
    assert CLEANUP_MAX_STEPS_PER_RECORD >= 4


def test_the_record_registration_captures_where_the_record_was_seen():
    import inspect

    from app.agent import controller as controller_module

    source = inspect.getsource(controller_module.AgentController._verify_and_register_temporary_record)
    assert "list_url=after_state.url" in source


def test_the_registry_advances_verified_to_updated():
    registry = _registry()
    entry = next(iter(registry.entries.values()))
    assert entry.current_state == "verified"

    registry.mark_updated(
        entry.temporary_record_id,
        field_key="el_002", before_value="Ada", after_value="Alex",
        verified=True, evidence=["updated_value_visible_after_submit"],
    )
    assert entry.current_state == "updated"
    assert entry.update_history[0].before_value == "Ada"
    assert entry.update_history[0].after_value == "Alex"


def test_an_updated_record_is_still_a_cleanup_candidate():
    registry = _registry()
    entry = next(iter(registry.entries.values()))
    registry.mark_updated(
        entry.temporary_record_id, field_key="el_002", before_value="Ada",
        after_value="Alex", verified=True, evidence=[],
    )
    assert entry in registry.records_pending_cleanup()
    assert plan_cleanup_action(entry, canonical_model=_detail_model()) is not None


def test_cleanup_is_refused_for_a_record_another_run_created():
    """The registry's own run-scoping rule — GemmaQA may only delete what it made."""
    registry = _registry()
    entry = next(iter(registry.entries.values()))
    assert registry.eligible_for_cleanup(entry.temporary_record_id, current_run_id="run_1") is True
    assert registry.eligible_for_cleanup(entry.temporary_record_id, current_run_id="a_different_run") is False


def test_an_update_is_offered_only_while_an_owned_record_awaits_one():
    """Observed live: after an accepted update the edit form was still offered,
    so the run edited the same record three times in six actions."""
    from app.agent.frontier import _has_updatable_owned_record

    class _M:
        temporary_record_registry = None

    memory = _M()
    assert _has_updatable_owned_record(memory) is False  # nothing owned

    registry = _registry()
    memory.temporary_record_registry = registry
    assert _has_updatable_owned_record(memory) is True  # one verified record

    entry = next(iter(registry.entries.values()))
    registry.mark_updated(
        entry.temporary_record_id, field_key="el_002", before_value="Ada",
        after_value="Alex", verified=True, evidence=[],
    )
    assert _has_updatable_owned_record(memory) is False  # already updated


def test_an_unowned_prefilled_form_is_never_updated():
    """GemmaQA only edits records it created — pre-existing application data is
    left alone."""
    from app.agent.frontier import _has_updatable_owned_record

    class _M:
        temporary_record_registry = TemporaryRecordRegistry("run_1")  # empty

    assert _has_updatable_owned_record(_M()) is False


def test_the_controller_hook_only_advances_on_an_update_workflow():
    """A create workflow must not be mistaken for an update."""
    import inspect

    from app.agent import controller as controller_module

    source = inspect.getsource(controller_module.AgentController._advance_record_lifecycle_on_update)
    assert 'form_wf.purpose != "safe_test_data_update"' in source


# ===========================================================================
# C3 — one identity-matching rule, shared by the frontier and cleanup
#
# The frontier matched a created record's row case-insensitively and
# bidirectionally; the cleanup planner used a plain case-sensitive `in`. So the
# frontier opened the created row happily (proved live, three times in one run)
# while cleanup, standing on the same page looking at the same row, could not
# see it — and delete stayed discovered-but-never-performed.
# ===========================================================================


def test_identity_matching_ignores_case():
    from app.agent.temporary_record_registry import identity_matches_cells

    assert identity_matches_cells("gemmaqa_test_ab12@example.com", ["GemmaQA_Test_AB12@Example.com"])


def test_identity_matching_accepts_a_truncated_cell():
    """A collection commonly renders less than what was submitted."""
    from app.agent.temporary_record_registry import identity_matches_cells

    assert identity_matches_cells("gemmaqa_test_ab12_9@example.com", ["gemmaqa_test_ab12_9"])


def test_identity_matching_accepts_a_cell_that_contains_the_identity():
    from app.agent.temporary_record_registry import identity_matches_cells

    assert identity_matches_cells("ab12_9@example.com", ["Contact: ab12_9@example.com (test)"])


def test_identity_matching_rejects_a_too_generic_identity():
    """Three characters match unrelated rows by coincidence, and a
    mis-identified row means deleting someone else's record."""
    from app.agent.temporary_record_registry import identity_matches_cells

    assert not identity_matches_cells("ab1", ["ab1234", "something"])


def test_identity_matching_rejects_a_too_short_cell():
    from app.agent.temporary_record_registry import identity_matches_cells

    assert not identity_matches_cells("gemmaqa_test_ab12@example.com", ["ab", "x"])


def test_identity_matching_rejects_unrelated_cells():
    from app.agent.temporary_record_registry import identity_matches_cells

    assert not identity_matches_cells("gemmaqa_test_ab12@example.com", ["Ada", "Lovelace", "Springfield"])


def test_identity_matching_tolerates_missing_and_non_string_cells():
    from app.agent.temporary_record_registry import identity_matches_cells

    assert not identity_matches_cells("gemmaqa_test_ab12@example.com", [None, "", 7])
    assert not identity_matches_cells("", ["anything"])
    assert not identity_matches_cells("gemmaqa_test_ab12@example.com", None)


def test_the_frontier_and_cleanup_agree_on_a_recased_row():
    """The exact live failure: the row the frontier opens must be the row
    cleanup can delete through."""
    from app.agent.cleanup_planner import plan_cleanup_navigation
    from app.agent.frontier import FrontierBuilder

    recased = IDENTITY.upper()
    row = CollectionRow(
        stable_id="row_0", element_id="el_008", row_index=0,
        cell_values=["Ada", recased], is_activatable=True, activation_basis="structural_hypothesis",
    )
    model = CanonicalPageModel(
        url=LIST_URL, state_fingerprint="fp_list",
        collections=[
            RecordCollection(
                stable_id="table_003", element_id="table_003", collection_type="native_table",
                row_count=1, visible_rows=[row],
            )
        ],
    )
    registry = _registry_with_list_url("verified")
    entry = next(iter(registry.entries.values()))

    assert FrontierBuilder._matching_created_identity(row, [IDENTITY]) == IDENTITY
    action = plan_cleanup_navigation(entry, canonical_model=model, current_url=LIST_URL)
    assert action is not None and action.element_id == "el_008"


def test_the_detail_page_identity_guard_ignores_case():
    from app.agent.cleanup_planner import plan_cleanup_action

    registry = _registry_with_list_url("verified")
    entry = next(iter(registry.entries.values()))
    action = plan_cleanup_action(entry, canonical_model=_detail_model(identity=IDENTITY.upper()))
    assert action is not None and action.element_id == "el_003"


def test_the_detail_page_identity_guard_still_refuses_an_unrelated_record():
    """Case folding must not weaken the guard that stops GemmaQA deleting
    whatever record happens to be open."""
    from app.agent.cleanup_planner import plan_cleanup_action

    registry = _registry_with_list_url("verified")
    entry = next(iter(registry.entries.values()))
    assert plan_cleanup_action(entry, canonical_model=_detail_model(identity="someone.else@example.com")) is None


# ===========================================================================
# C4 — the HIGH-risk exemption for cleanup, and its three conditions
#
# `ActionValidator` blocked HIGH risk one line before it ever consulted
# `allow_destructive_actions`, so the whole cleanup path was unreachable by
# construction: every live run discovered its delete control, planned the
# delete, and had it refused with "Risk level 'RiskLevel.HIGH' is blocked".
# ===========================================================================


def _policy(*, allow_destructive: bool):
    from app.safety.policies import SafetyPolicy

    return SafetyPolicy(
        authorized_url="https://app.example.com/records",
        authorized_domain="app.example.com",
        allow_destructive_actions=allow_destructive,
    )


def _high_risk_click(**metadata):
    from app.schemas import ActionCategory, ActionType, BrowserAction, RiskLevel

    return BrowserAction(
        action=ActionType.CLICK,
        element_id="el_003",
        reason="Delete GemmaQA temporary test record",
        expected_result="The record is removed.",
        risk=RiskLevel.HIGH,
        category=ActionCategory.EXPLORATION,
        metadata=metadata,
    )


def _validate(action, *, allow_destructive: bool):
    from app.safety.validator import ActionValidator

    return ActionValidator(_policy(allow_destructive=allow_destructive)).validate(action)


def test_a_marked_cleanup_delete_is_allowed_when_the_run_opted_in():
    action = _high_risk_click(
        risk_class=CLEANUP_RISK_CLASS, cleanup_temporary_record_id="tr_1", cleanup_step="delete_control"
    )
    assert _validate(action, allow_destructive=True).allowed


def test_a_marked_cleanup_delete_is_still_blocked_without_the_opt_in():
    action = _high_risk_click(
        risk_class=CLEANUP_RISK_CLASS, cleanup_temporary_record_id="tr_1", cleanup_step="delete_control"
    )
    result = _validate(action, allow_destructive=False)
    assert not result.allowed
    assert "Risk level" in result.reason


def test_the_exemption_needs_the_registry_id_not_just_the_marker():
    """Ties the exemption to a tracked, run-owned record rather than to
    'any delete button that says it is cleanup'."""
    action = _high_risk_click(risk_class=CLEANUP_RISK_CLASS)
    assert not _validate(action, allow_destructive=True).allowed


def test_a_generic_destructive_label_earns_no_exemption():
    action = _high_risk_click(risk_class="destructive", cleanup_temporary_record_id="tr_1")
    assert not _validate(action, allow_destructive=True).allowed


def test_an_unmarked_high_risk_action_is_still_blocked():
    assert not _validate(_high_risk_click(), allow_destructive=True).allowed


def test_critical_risk_is_never_exempt():
    from app.schemas import RiskLevel

    action = _high_risk_click(
        risk_class=CLEANUP_RISK_CLASS, cleanup_temporary_record_id="tr_1", cleanup_step="delete_control"
    )
    action.risk = RiskLevel.CRITICAL
    assert not _validate(action, allow_destructive=True).allowed


def test_the_planned_delete_action_passes_the_validator_end_to_end():
    """The planner's real output — not a hand-built action — must survive the
    real gate. That coupling is what broke."""
    registry = _registry()
    entry = next(iter(registry.entries.values()))
    action = plan_cleanup_action(entry, canonical_model=_detail_model())
    assert action is not None
    assert _validate(action, allow_destructive=True).allowed
    assert not _validate(action, allow_destructive=False).allowed


def test_the_planned_confirmation_action_passes_the_validator_too():
    from app.agent.temporary_record_registry import CleanupPlan

    registry = _registry()
    entry = next(iter(registry.entries.values()))
    registry.request_cleanup(entry.temporary_record_id, plan=CleanupPlan(delete_control_element_id="el_003"))
    model = CanonicalPageModel(
        url="https://app.example.com/recordDetails",
        state_fingerprint="fp_dialog",
        dialogs=[
            DialogDescriptor(
                stable_id="dlg_1", element_id="dlg_1", dialog_type="modal",
                is_open=True, contained_element_ids=["el_020"],
            )
        ],
        action_semantics=[ActionSemantics(element_id="el_020", semantic_action="confirm")],
    )
    action = plan_cleanup_action(entry, canonical_model=model)
    assert action is not None and action.element_id == "el_020"
    assert _validate(action, allow_destructive=True).allowed


# ===========================================================================
# C5 — "I don't see the record" is not proof the record is gone
#
# A live cleanup pass navigated back to the record's list page, observed 2
# elements while the client-side application was still booting, was told by the
# Adaptive Application Understanding Engine that the state was
# `frontend_failure`, and reported the record as unreachable anyway — leaving
# GemmaQA's own test data on the application under test.
# ===========================================================================


def _assessment(state: str):
    from app.intelligence.adaptive_understanding.schemas import (
        ApplicationStateAssessment,
        UnderstandingAssessment,
    )

    return UnderstandingAssessment(state=ApplicationStateAssessment(primary_state=state, confidence=0.7))


def _controller_with(assessment):
    from app.agent.controller import AgentController

    controller = AgentController.__new__(AgentController)  # no browser, no run
    controller._last_understanding = assessment
    return controller


def test_a_frontend_failure_observation_cannot_prove_absence():
    assert _controller_with(_assessment("frontend_failure"))._observation_cannot_prove_absence()


def test_a_still_loading_observation_cannot_prove_absence():
    for state in ("initializing", "loading", "partially_interactive", "backend_failure", "unknown"):
        assert _controller_with(_assessment(state))._observation_cannot_prove_absence(), state


def test_a_rendered_collection_can_prove_absence():
    """Otherwise cleanup could never conclude anything and would burn its whole
    re-observation budget on every record."""
    for state in ("collection", "detail", "interactive", "empty_state", "no_data"):
        assert not _controller_with(_assessment(state))._observation_cannot_prove_absence(), state


def test_no_assessment_at_all_does_not_block_cleanup():
    """The engine is optional — its absence must not disable cleanup."""
    assert not _controller_with(None)._observation_cannot_prove_absence()


def test_untrustworthy_absence_states_are_all_real_application_states():
    """A typo here would silently never match, and cleanup would go back to
    concluding absence from an unrendered page."""
    from app.intelligence.adaptive_understanding.schemas import (
        APPLICATION_STATES,
        UNTRUSTWORTHY_ABSENCE_STATES,
    )

    assert UNTRUSTWORTHY_ABSENCE_STATES <= APPLICATION_STATES


def test_an_empty_state_is_trusted_because_it_is_a_rendered_answer():
    """`empty_state`/`no_data` mean the application DID render and reported
    nothing — the one case where absence is genuine evidence."""
    from app.intelligence.adaptive_understanding.schemas import UNTRUSTWORTHY_ABSENCE_STATES

    assert "empty_state" not in UNTRUSTWORTHY_ABSENCE_STATES
    assert "no_data" not in UNTRUSTWORTHY_ABSENCE_STATES


# ===========================================================================
# C6 — walk back INSIDE the application, not via its URL
#
# `open_url` reloads the document. An application whose session lives in memory
# rather than in a cookie is logged out by that: a live cleanup pass opened the
# record's list URL directly, landed unauthenticated on a two-element page, and
# reported the record unreachable. The record was fine; the session wasn't.
# ===========================================================================


def _model_with_nav_to(target: str | None) -> CanonicalPageModel:
    from app.perception.models import NavigationItem, NavigationRegion

    items = [NavigationItem(stable_id="n1", element_id="el_004", text="Contact List", target_url=target)]
    return CanonicalPageModel(
        url="https://app.example.com/addRecord",
        state_fingerprint="fp_form",
        navigation_regions=[NavigationRegion(stable_id="nav_1", items=items)],
    )


def test_cleanup_prefers_an_in_app_link_over_a_hard_navigation():
    from app.agent.cleanup_planner import plan_cleanup_navigation
    from app.schemas import ActionType

    registry = _registry_with_list_url("verified")
    entry = next(iter(registry.entries.values()))
    action = plan_cleanup_navigation(
        entry, canonical_model=_model_with_nav_to(LIST_URL), current_url="https://app.example.com/addRecord"
    )
    assert action is not None
    assert action.action == ActionType.CLICK
    assert action.element_id == "el_004"


def test_cleanup_falls_back_to_open_url_when_no_in_app_route_exists():
    from app.agent.cleanup_planner import plan_cleanup_navigation
    from app.schemas import ActionType

    registry = _registry_with_list_url("verified")
    entry = next(iter(registry.entries.values()))
    action = plan_cleanup_navigation(
        entry,
        canonical_model=_model_with_nav_to("https://app.example.com/somewhere-else"),
        current_url="https://app.example.com/addRecord",
    )
    assert action is not None
    assert action.action == ActionType.OPEN_URL
    assert action.url == LIST_URL


def test_an_in_app_route_is_matched_structurally_not_by_label():
    """A link labelled "Contact List" that points elsewhere must not be used —
    only its own target decides."""
    from app.agent.cleanup_planner import _find_in_app_route

    assert _find_in_app_route(LIST_URL, _model_with_nav_to(LIST_URL + "/")) == "el_004"
    assert _find_in_app_route(LIST_URL, _model_with_nav_to("https://other.example.com/records")) is None
    assert _find_in_app_route(LIST_URL, _model_with_nav_to(None)) is None


def test_a_breadcrumb_back_to_the_list_also_counts():
    from app.agent.cleanup_planner import _find_in_app_route
    from app.perception.models import BreadcrumbDescriptor

    model = CanonicalPageModel(
        url="https://app.example.com/recordDetails",
        state_fingerprint="fp_detail",
        breadcrumbs=[BreadcrumbDescriptor(stable_id="b1", element_id="el_009", ordinal=0, target_url=LIST_URL)],
    )
    assert _find_in_app_route(LIST_URL, model) == "el_009"


def test_the_in_app_navigation_step_stays_low_risk_and_needs_no_exemption():
    from app.agent.cleanup_planner import plan_cleanup_navigation
    from app.schemas import RiskLevel

    registry = _registry_with_list_url("verified")
    entry = next(iter(registry.entries.values()))
    action = plan_cleanup_navigation(
        entry, canonical_model=_model_with_nav_to(LIST_URL), current_url="https://app.example.com/addRecord"
    )
    assert action.risk == RiskLevel.LOW
    assert action.metadata["cleanup_step"] == "navigate_to_list"


def test_every_cleanup_step_carries_the_cleanup_marker():
    """A read-only navigation step was refused with "Blocked by safety pattern
    'delete'" because its own reason named the control it was walking toward.
    Every step of an authorized cleanup carries the marker; the RISK LEVEL, not
    the wording, is what distinguishes navigating from deleting."""
    from app.agent.cleanup_planner import plan_cleanup_action, plan_cleanup_navigation
    from app.schemas import RiskLevel

    registry = _registry_with_list_url("verified")
    entry = next(iter(registry.entries.values()))

    row = CollectionRow(
        stable_id="row_0", element_id="el_008", row_index=0,
        cell_values=["Ada", IDENTITY], is_activatable=True, activation_basis="observed",
    )
    list_model = CanonicalPageModel(
        url=LIST_URL, state_fingerprint="fp_list",
        collections=[
            RecordCollection(
                stable_id="table_003", element_id="table_003", collection_type="native_table",
                row_count=1, visible_rows=[row],
            )
        ],
    )

    steps = [
        plan_cleanup_navigation(entry, canonical_model=_model_with_nav_to(LIST_URL), current_url="https://app.example.com/addRecord"),
        plan_cleanup_navigation(entry, canonical_model=CanonicalPageModel(url="https://app.example.com/addRecord", state_fingerprint="f"), current_url="https://app.example.com/addRecord"),
        plan_cleanup_navigation(entry, canonical_model=list_model, current_url=LIST_URL),
        plan_cleanup_action(entry, canonical_model=_detail_model()),
    ]
    for step in steps:
        assert step is not None
        assert step.metadata["risk_class"] == CLEANUP_RISK_CLASS
        assert step.metadata["cleanup_temporary_record_id"] == entry.temporary_record_id
        assert _validate(step, allow_destructive=True).allowed

    # Only the delete itself is HIGH risk; walking there is not.
    assert [s.risk for s in steps] == [RiskLevel.LOW, RiskLevel.LOW, RiskLevel.LOW, RiskLevel.HIGH]
    # ...and so only the delete needs the operator's destructive opt-in. The
    # walk is ordinary navigation and stays allowed without it.
    assert not _validate(steps[-1], allow_destructive=False).allowed


# ===========================================================================
# C7 — absence is only verified against something that lists records
#
# The delete executed on the record's own detail page, which has no collection
# at all — so the old check found "no row carrying the identity" vacuously and
# reported the absence as verified. Cleanup coverage read 100% on evidence
# nobody had gathered.
# ===========================================================================


def test_absence_verification_needs_a_collection_to_look_in():
    import inspect

    from app.agent import controller as controller_module

    source = inspect.getsource(controller_module.AgentController._run_cleanup_pass)
    assert "if not collections:" in source
    assert "identity_matches_cells" in source


def test_a_deleted_record_whose_absence_is_unconfirmed_is_not_reported_as_succeeded():
    registry = _registry_with_list_url("verified")
    entry = next(iter(registry.entries.values()))
    registry.request_cleanup(entry.temporary_record_id, plan=CleanupPlan(delete_control_element_id="el_003"))
    registry.mark_delete_action_validated(entry.temporary_record_id)
    registry.mark_deleted(entry.temporary_record_id)

    assert entry.current_state == "deleted"
    assert entry.cleanup_result.attempted is True
    assert entry.cleanup_result.succeeded is False
    assert entry.cleanup_result.absence_verified is False


def test_absence_verified_is_what_marks_cleanup_successful():
    registry = _registry_with_list_url("verified")
    entry = next(iter(registry.entries.values()))
    registry.request_cleanup(entry.temporary_record_id, plan=CleanupPlan(delete_control_element_id="el_003"))
    registry.mark_delete_action_validated(entry.temporary_record_id)
    registry.mark_deleted(entry.temporary_record_id)
    assert registry.mark_absence_verified(entry.temporary_record_id)

    assert entry.current_state == "absence_verified"
    assert entry.cleanup_result.succeeded is True
    assert entry.cleanup_result.absence_verified is True
    assert registry.records_pending_cleanup() == []


# ===========================================================================
# F — cleanup blocked by a lost session says so, instead of failing silently
#
# Cleanup runs as an epilogue, often long after the last action. Navigating back
# to the record's list can land on a sign-in screen — the collection is then
# empty for a reason that has nothing to do with the record. The run reported
# `pending: 1, succeeded: 0, failed: 0`, which is indistinguishable from a
# record nobody attempted. GemmaQA reached the door and found it locked; it
# should say that.
#
# Re-authenticating from the epilogue is task #161 and is NOT done here.
# ===========================================================================


def test_an_authentication_screen_during_cleanup_is_recognised():
    assert _controller_with(_assessment("authentication"))._cleanup_blocked_by_lost_session()


def test_an_ordinary_page_during_cleanup_is_not_a_lost_session():
    for state in ("collection", "detail", "interactive", "empty_state", "frontend_failure"):
        assert not _controller_with(_assessment(state))._cleanup_blocked_by_lost_session(), state


def test_no_assessment_does_not_claim_a_lost_session():
    """The engine is optional; its absence must not invent a diagnosis."""
    assert not _controller_with(None)._cleanup_blocked_by_lost_session()


def test_a_session_loss_is_recorded_as_a_failure_with_a_reason():
    """`cleanup_failed` carries the reason into the report and the manual-cleanup
    instructions; `pending` carries nothing."""
    registry = _registry_with_list_url("verified")
    entry = next(iter(registry.entries.values()))
    registry.mark_cleanup_failed(
        entry.temporary_record_id, error="The session was no longer valid when cleanup ran"
    )

    assert entry.current_state == "cleanup_failed"
    assert entry.cleanup_result.attempted is True
    assert "session was no longer valid" in (entry.cleanup_result.last_error or "")


def test_the_cleanup_pass_consults_the_lost_session_check():
    import inspect

    from app.agent import controller as controller_module

    source = inspect.getsource(controller_module.AgentController._run_cleanup_pass)
    assert "_cleanup_blocked_by_lost_session()" in source


# ===========================================================================
# G — cleanup signs back in rather than abandoning the record
#
# Cleanup is an epilogue, so the session may be gone by the time it runs; then
# navigating back to the record's list lands on a login screen and the
# collection is empty for a reason unrelated to the record. GemmaQA was holding
# the credentials and standing outside the locked door.
#
# It reuses the run's OWN AuthenticationStrategy — its form detection, its fill
# plan, its post-submit evaluation. A second login implementation is exactly how
# this codebase grew the duplicate-rule bugs it spent a session unpicking.
# ===========================================================================



def _login_page():
    """A bare sign-in screen — what cleanup lands on when the session is gone."""
    from app.schemas import InteractiveElement, PageState

    def el(eid, tag, text, **kw):
        return InteractiveElement(
            element_id=eid, tag=tag, type=kw.pop("type", tag),
            visible_text=text, selector=f"#{eid}", **kw
        )

    return PageState(
        page_id="page_login", url="https://app.example.com/auth/login",
        title="Sign in", state_fingerprint="fp_login", headings=["Login"],
        interactive_elements=[
            el("el_002", "input", "", type="text", label="Username"),
            el("el_003", "input", "", type="password", label="Password"),
            el("el_004", "button", "Login"),
        ],
        forms=[FormDescriptor(
            form_id="form_login", submit_element_id="el_004",
            fields=[
                FormField(name="username", field_type="text", label="Username", element_id="el_002"),
                FormField(name="password", field_type="password", label="Password", element_id="el_003"),
            ],
        )],
    )


def _non_login_page():
    from app.schemas import PageState

    return PageState(
        page_id="p2", url="https://app.example.com/records", title="Records",
        state_fingerprint="fp_r", headings=["Records"],
    )


class _Recorder:
    """Stands in for the executor/validator/observer trio."""

    def __init__(self, *, allowed=True, succeeds=True, after=None):
        self._allowed = allowed
        self._succeeds = succeeds
        self._after = after
        self.executed: list[str] = []

    # validator
    def validate(self, action, **_kw):
        from types import SimpleNamespace

        return SimpleNamespace(
            allowed=self._allowed, reason="blocked by policy", sanitized_action=None
        )

    # executor
    async def execute(self, *, action, page, page_state, capture_evidence=False):
        from types import SimpleNamespace

        self.executed.append(action.element_id or action.action.value)
        return SimpleNamespace(success=self._succeeds, error="", message="", after_url=None)

    # observer
    async def observe(self, *_a, **_kw):
        return self._after if self._after is not None else _login_page()


def _controller_with_auth(auth):
    controller = _controller_with(_assessment("authentication"))
    from app.agent.memory import RunMemory
    from app.schemas import RunConfiguration

    controller.memory = RunMemory(
        run_id="run_1", start_url="https://app.example.com/",
        configuration=RunConfiguration(),
    )
    controller.memory.auth_strategy = auth
    controller._started_at = None
    return controller


def _auth_with_credentials(*, authenticated_after: bool):
    import asyncio

    from app.agent.auth_strategy import AuthenticationStrategy
    from app.agent.credentials import CredentialProfile, CredentialVault

    vault = CredentialVault()
    vault.store(
        CredentialProfile(profile_id="p1", username="admin", password="pw", source="operator_supplied"),
        make_active=True,
    )
    auth = AuthenticationStrategy(vault)
    auth.authenticated = False
    auth.evaluate_after_submit = lambda before, after, *, method: type(
        "E", (), {"authenticated": authenticated_after}
    )()
    return auth


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_cleanup_signs_back_in_when_the_session_is_gone():
    auth = _auth_with_credentials(authenticated_after=True)
    controller = _controller_with_auth(auth)
    rec = _Recorder()

    restored = _run(controller._reauthenticate_for_cleanup(
        page_state=_login_page(), executor=rec, page=None, validator=rec, observe=rec.observe,
    ))

    assert restored is not None
    assert rec.executed, "no login steps were executed"


def test_a_failed_sign_in_does_not_pretend_the_session_was_restored():
    auth = _auth_with_credentials(authenticated_after=False)
    controller = _controller_with_auth(auth)
    rec = _Recorder()

    assert _run(controller._reauthenticate_for_cleanup(
        page_state=_login_page(), executor=rec, page=None, validator=rec, observe=rec.observe,
    )) is None


def test_cleanup_does_not_re_authenticate_without_credentials():
    """Never invent a login. With no stored profile there is nothing to sign in
    with, and the record stays reported as unreachable."""
    from app.agent.auth_strategy import AuthenticationStrategy

    controller = _controller_with_auth(AuthenticationStrategy())
    rec = _Recorder()

    assert _run(controller._reauthenticate_for_cleanup(
        page_state=_login_page(), executor=rec, page=None, validator=rec, observe=rec.observe,
    )) is None
    assert rec.executed == []


def test_the_safety_gate_still_governs_a_cleanup_login():
    """Every step goes through the caller's validator — the gates that apply to a
    login mid-run apply identically here."""
    controller = _controller_with_auth(_auth_with_credentials(authenticated_after=True))
    rec = _Recorder(allowed=False)

    assert _run(controller._reauthenticate_for_cleanup(
        page_state=_login_page(), executor=rec, page=None, validator=rec, observe=rec.observe,
    )) is None
    assert rec.executed == []


def test_a_failed_step_aborts_the_sign_in():
    controller = _controller_with_auth(_auth_with_credentials(authenticated_after=True))
    rec = _Recorder(succeeds=False)

    assert _run(controller._reauthenticate_for_cleanup(
        page_state=_login_page(), executor=rec, page=None, validator=rec, observe=rec.observe,
    )) is None


def test_a_page_with_no_login_form_yields_no_attempt():
    controller = _controller_with_auth(_auth_with_credentials(authenticated_after=True))
    rec = _Recorder()

    assert _run(controller._reauthenticate_for_cleanup(
        page_state=_non_login_page(), executor=rec, page=None, validator=rec, observe=rec.observe,
    )) is None


def test_the_cleanup_pass_tries_re_authentication_before_giving_up():
    import inspect

    from app.agent import controller as controller_module

    source = inspect.getsource(controller_module.AgentController._run_cleanup_pass)
    assert "_reauthenticate_for_cleanup(" in source
    assert "reauth_attempted" in source, "re-authentication must be attempted at most once"
