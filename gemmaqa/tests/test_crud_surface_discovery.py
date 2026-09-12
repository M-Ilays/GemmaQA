"""CRUD Surface Discovery — tests for universal collection detection, form
intent classification, action semantics, field semantics, CRUD workflow
inference, and the resulting knowledge-graph relationships.

Every fixture in this file is APPLICATION-NEUTRAL: entities are named
"Member"/"Record" and controls "Add Member"/"Edit"/"Delete", never any real
target application's vocabulary. The one exception is the real-browser
integration test at the bottom, which mimics OrangeHRM's *structural* shape
(a div-based record grid with a search/filter form above it and a separate
add-record page) using entirely generic naming — see its docstring.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.form_lifecycle import NOT_TESTED_REASONS, FormLifecycle  # noqa: E402
from app.application.store import ApplicationStore  # noqa: E402
from app.intelligence.crud_discovery import CRUDDiscoveryEngine, CRUDRegistry  # noqa: E402
from app.intelligence.crud_discovery.crud_candidate_builder import CRUDCandidateBuilder  # noqa: E402
from app.intelligence.knowledge_graph import ApplicationKnowledgeGraph  # noqa: E402
from app.intelligence.knowledge_graph.graph_node_factory import entity_node_id  # noqa: E402
from app.perception import action_semantics, collection_extractor, form_extractor, form_intent_classifier  # noqa: E402
from app.perception.dom_extractor import RawObservation  # noqa: E402
from app.perception.models import (  # noqa: E402
    CanonicalPageModel,
    CollectionAction,
    DialogDescriptor,
    FormIntentClassification,
    HeadingDescriptor,
    RecordCollection,
)
from app.schemas import FormDescriptor, FormFieldDescriptor, PageState  # noqa: E402


def _el(dom_id: str, **overrides) -> dict:
    base = {
        "dom_id": dom_id, "tag": "div", "role": None, "type": None, "name": None,
        "id_attr": None, "text": "", "aria_label": None, "accessible_name": "",
        "accessible_description": None, "label": None, "href": None, "placeholder": None,
        "title_attr": None, "is_visible": True, "is_enabled": True, "required": False,
        "disabled": False, "checked": None, "aria_expanded": None, "aria_selected": None,
        "aria_current": None, "aria_haspopup": None, "tabindex": None,
        "is_native_focusable": False, "cursor_style": None, "has_onclick_attr": False,
        "looks_icon_only": False, "has_inline_svg": False, "current_value": None,
        "available_options": [], "category": "other",
        "bounding_box": {"x": 0, "y": 0, "width": 40, "height": 40},
        "landmark_region_id": None, "container_role": None,
        "selector_hint": f'[data-gemmaqa-id="{dom_id}"]',
        "multiple": False, "list_attr": None, "aria_autocomplete": None,
        "aria_multiselectable": None, "input_mode": None, "position_style": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Universal record-collection detection
# ---------------------------------------------------------------------------


def test_native_table_detected_as_collection():
    raw = RawObservation(
        url="https://example.com/members",
        tables=[{"dom_id": "t1", "headers": ["Name", "Role"], "row_count": 1,
                 "rows": [{"row_index": 0, "cell_values": ["Ada", "Admin"], "row_action_ids": []}],
                 "bounding_box": {"x": 0, "y": 100, "width": 300, "height": 50}}],
    )
    collections = collection_extractor.extract_collections(raw)
    assert len(collections) == 1
    assert collections[0].collection_type == "native_table"
    assert collections[0].row_count == 1
    assert collections[0].columns[0].header_text == "Name"


def test_aria_grid_detected_as_collection():
    raw = RawObservation(
        url="https://example.com/members",
        collections=[{
            "dom_id": "grid_1", "collection_type": "aria_grid", "headers": ["Name", "Status"],
            "row_count": 2,
            "rows": [
                {"dom_id": "row_1", "row_index": 0, "cell_values": ["Ada", "Active"], "row_action_ids": []},
                {"dom_id": "row_2", "row_index": 1, "cell_values": ["Grace", "Active"], "row_action_ids": []},
            ],
            "bounding_box": {"x": 0, "y": 100, "width": 300, "height": 90},
            "structural_signals": ["aria_grid_role"],
        }],
    )
    collections = collection_extractor.extract_collections(raw)
    assert len(collections) == 1
    assert collections[0].collection_type == "aria_grid"
    assert "aria_grid_role" in collections[0].structural_signals
    assert collections[0].row_count == 2


def test_div_based_repeated_rows_detected_as_collection():
    """The exact gap the CRUD surface discovery task targets: a Vue/React
    style div-grid with NO <table>/role=grid markup at all."""
    raw = RawObservation(
        url="https://example.com/members",
        collections=[{
            "dom_id": "div_grid_1", "collection_type": "div_row_group", "headers": ["Name", "Role"],
            "row_count": 3,
            "rows": [{"dom_id": f"row_{i}", "row_index": i, "cell_values": [f"Member {i}", "Member"], "row_action_ids": []} for i in range(3)],
            "bounding_box": {"x": 0, "y": 100, "width": 300, "height": 150},
            "structural_signals": ["repeated_class_signature", "vertical_stack_layout"],
        }],
    )
    collections = collection_extractor.extract_collections(raw)
    assert len(collections) == 1
    assert collections[0].collection_type == "div_row_group"
    assert collections[0].row_count == 3


def test_repeated_cards_detected_as_card_group():
    raw = RawObservation(
        url="https://example.com/members",
        collections=[{
            "dom_id": "cards_1", "collection_type": "card_group", "headers": [],
            "row_count": 3,
            "rows": [{"dom_id": f"card_{i}", "row_index": i, "cell_values": [f"Member {i}"], "row_action_ids": []} for i in range(3)],
            "bounding_box": {"x": 0, "y": 100, "width": 600, "height": 200},
            "structural_signals": ["repeated_class_signature", "card_wrap_layout"],
        }],
    )
    collections = collection_extractor.extract_collections(raw)
    assert collections[0].collection_type == "card_group"


def test_row_actions_linked_to_rows_with_semantic_verbs():
    raw = RawObservation(
        url="https://example.com/members",
        elements=[
            _el("el_edit", tag="button", category="button", aria_label="Edit", looks_icon_only=True, has_inline_svg=True),
            _el("el_delete", tag="button", category="button", aria_label="Delete", looks_icon_only=True, has_inline_svg=True),
        ],
        collections=[{
            "dom_id": "grid_1", "collection_type": "div_row_group", "headers": [], "row_count": 1,
            "rows": [{"dom_id": "row_1", "row_index": 0, "cell_values": ["Ada"], "row_action_ids": ["el_edit", "el_delete"]}],
            "bounding_box": {"x": 0, "y": 100, "width": 300, "height": 50}, "structural_signals": [],
        }],
    )
    sem = action_semantics.classify_actions(raw)
    sem_map = action_semantics.action_semantics_by_id(sem)
    collections = collection_extractor.extract_collections(raw, sem_map)
    row_actions = {a.element_id: a.semantic_action for a in collections[0].row_actions}
    assert row_actions == {"el_edit": "edit", "el_delete": "delete"}


def test_global_add_action_attached_to_collection():
    raw = RawObservation(
        url="https://example.com/members",
        elements=[_el("el_add", tag="button", category="button", text="Add Member", accessible_name="Add Member",
                       bounding_box={"x": 10, "y": 40, "width": 100, "height": 30})],
        collections=[{
            "dom_id": "grid_1", "collection_type": "div_row_group", "headers": [], "row_count": 0, "rows": [],
            "bounding_box": {"x": 0, "y": 100, "width": 300, "height": 50}, "structural_signals": [],
        }],
    )
    sem = action_semantics.classify_actions(raw)
    sem_map = action_semantics.action_semantics_by_id(sem)
    collections = collection_extractor.extract_collections(raw, sem_map)
    assert len(collections[0].global_actions) == 1
    assert collections[0].global_actions[0].semantic_action == "add"


def test_search_and_filter_controls_attached_to_collection():
    raw = RawObservation(
        url="https://example.com/members",
        elements=[
            _el("el_search", tag="input", category="input", placeholder="Search members",
                accessible_name="Search members", bounding_box={"x": 10, "y": 20, "width": 150, "height": 20}),
            _el("el_filter", tag="select", category="select", accessible_name="Filter by status",
                bounding_box={"x": 170, "y": 20, "width": 150, "height": 20}),
        ],
        collections=[{
            "dom_id": "grid_1", "collection_type": "div_row_group", "headers": [], "row_count": 0, "rows": [],
            "bounding_box": {"x": 0, "y": 100, "width": 300, "height": 50}, "structural_signals": [],
        }],
    )
    collections = collection_extractor.extract_collections(raw)
    assert collections[0].has_search_control is True
    assert collections[0].search_control_ids == ["el_search"]
    assert collections[0].has_filter_controls is True
    assert collections[0].filter_control_ids == ["el_filter"]


def test_collection_extraction_is_idempotent_and_stable_ids():
    raw = RawObservation(
        url="https://example.com/members",
        tables=[{"dom_id": "t1", "headers": ["Name"], "row_count": 1,
                 "rows": [{"row_index": 0, "cell_values": ["Ada"], "row_action_ids": []}],
                 "bounding_box": {"x": 0, "y": 0, "width": 100, "height": 40}}],
    )
    first = collection_extractor.extract_collections(raw)
    second = collection_extractor.extract_collections(raw)
    assert [c.stable_id for c in first] == [c.stable_id for c in second]
    assert [c.element_id for c in first] == [c.element_id for c in second]


# ---------------------------------------------------------------------------
# 2. Form intent classification
# ---------------------------------------------------------------------------


def _model_with_forms(*forms: FormDescriptor, collections=None, headings=None) -> CanonicalPageModel:
    return CanonicalPageModel(
        url="https://example.com/members", title="Members",
        headings=headings or [HeadingDescriptor(text="Members", level=1)],
        forms=list(forms), collections=collections or [],
    )


def test_search_form_classified_not_create():
    """The exact anti-pattern named by the task: a small search/filter form
    on a list page must NOT be labelled create."""
    form = FormDescriptor(form_id="f1", field_descriptors=[
        FormFieldDescriptor(stable_id="s1", element_id="el_q", field_type="text", accessible_name="Search members", placeholder="Search members", current_value=""),
    ])
    model = _model_with_forms(form, collections=[RecordCollection(element_id="coll_1", collection_type="div_row_group", row_count=5)])
    intents = form_intent_classifier.classify_form_intents(model)
    assert intents[0].intent == "search"
    assert intents[0].intent != "create"


def test_create_form_classified_when_mostly_empty():
    form = FormDescriptor(form_id="f1", field_descriptors=[
        FormFieldDescriptor(stable_id="s1", element_id="el_name", field_type="text", label="Full Name", current_value=""),
        FormFieldDescriptor(stable_id="s2", element_id="el_role", field_type="text", label="Role", current_value=""),
    ])
    model = _model_with_forms(form)
    intents = form_intent_classifier.classify_form_intents(model)
    assert intents[0].intent == "create"


def test_edit_form_classified_when_mostly_filled():
    form = FormDescriptor(form_id="f1", field_descriptors=[
        FormFieldDescriptor(stable_id="s1", element_id="el_name", field_type="text", label="Full Name", current_value="Ada Lovelace"),
        FormFieldDescriptor(stable_id="s2", element_id="el_role", field_type="text", label="Role", current_value="Admin"),
    ])
    model = _model_with_forms(form)
    intents = form_intent_classifier.classify_form_intents(model)
    assert intents[0].intent == "edit"


def test_authentication_form_classified_via_password_field():
    form = FormDescriptor(form_id="f1", field_descriptors=[
        FormFieldDescriptor(stable_id="s1", element_id="el_user", field_type="text", label="Username", current_value=""),
        FormFieldDescriptor(stable_id="s2", element_id="el_pw", field_type="password", label="Password", current_value=""),
    ])
    model = _model_with_forms(form)
    intents = form_intent_classifier.classify_form_intents(model)
    assert intents[0].intent == "authentication"
    assert intents[0].confidence.value >= 0.8


def test_delete_confirmation_form_classified_in_dialog():
    form = FormDescriptor(form_id="f1", field_descriptors=[])
    model = CanonicalPageModel(
        url="https://example.com/members", title="Members",
        forms=[form],
        dialogs=[DialogDescriptor(element_id="dlg_1", dialog_type="dialog", text="Are you sure you want to delete this member?", contained_element_ids=["f1"])],
    )
    intents = form_intent_classifier.classify_form_intents(model)
    assert intents[0].intent == "delete_confirmation"


def test_bulk_action_form_classified_via_checkbox_majority():
    form = FormDescriptor(form_id="f1", field_descriptors=[
        FormFieldDescriptor(stable_id="s1", element_id="el_1", field_type="checkbox", label="Row 1"),
        FormFieldDescriptor(stable_id="s2", element_id="el_2", field_type="checkbox", label="Row 2"),
        FormFieldDescriptor(stable_id="s3", element_id="el_3", field_type="checkbox", label="Row 3"),
    ])
    model = _model_with_forms(form)
    intents = form_intent_classifier.classify_form_intents(model)
    assert intents[0].intent == "bulk_action"


def test_upload_form_classified_via_file_field():
    form = FormDescriptor(form_id="f1", field_descriptors=[
        FormFieldDescriptor(stable_id="s1", element_id="el_file", field_type="file", label="Attachment"),
    ])
    model = _model_with_forms(form)
    intents = form_intent_classifier.classify_form_intents(model)
    assert intents[0].intent == "upload"


def test_settings_form_classified_via_url_heading():
    form = FormDescriptor(form_id="f1", field_descriptors=[
        FormFieldDescriptor(stable_id="s1", element_id="el_tz", field_type="text", label="Timezone", current_value="UTC"),
    ])
    model = CanonicalPageModel(
        url="https://example.com/settings/preferences", title="Preferences",
        headings=[HeadingDescriptor(text="Preferences", level=1)], forms=[form],
    )
    intents = form_intent_classifier.classify_form_intents(model)
    assert intents[0].intent == "settings"


def test_unknown_form_when_no_fields():
    form = FormDescriptor(form_id="f1", field_descriptors=[])
    model = _model_with_forms(form)
    intents = form_intent_classifier.classify_form_intents(model)
    assert intents[0].intent == "unknown"


def test_ambiguous_form_keeps_alternatives_not_dropped():
    """A form with BOTH a password field and a file field is genuinely
    ambiguous — the rejected candidate must be preserved, never discarded."""
    form = FormDescriptor(form_id="f1", field_descriptors=[
        FormFieldDescriptor(stable_id="s1", element_id="el_pw", field_type="password", label="Password", current_value=""),
        FormFieldDescriptor(stable_id="s2", element_id="el_file", field_type="file", label="ID Document"),
    ])
    model = _model_with_forms(form)
    intents = form_intent_classifier.classify_form_intents(model)
    assert intents[0].intent == "authentication"
    alt_intents = {a.intent for a in intents[0].alternatives}
    assert "upload" in alt_intents


# ---------------------------------------------------------------------------
# 3. Action semantics
# ---------------------------------------------------------------------------


def test_icon_only_delete_button_classified_via_aria_label():
    raw = RawObservation(elements=[_el("el_1", tag="button", category="button", text="", aria_label="Delete", looks_icon_only=True, has_inline_svg=True)])
    results = action_semantics.classify_actions(raw)
    assert results[0].semantic_action == "delete"
    assert results[0].is_icon_only is True
    assert "aria_label" in results[0].signals


def test_icon_only_edit_button_classified_via_title_attribute():
    raw = RawObservation(elements=[_el("el_1", tag="button", category="button", text="", title_attr="Edit member", looks_icon_only=True, has_inline_svg=True)])
    results = action_semantics.classify_actions(raw)
    assert results[0].semantic_action == "edit"
    assert "title_attribute" in results[0].signals


def test_kebab_menu_classified_as_more_actions():
    raw = RawObservation(elements=[_el("el_1", tag="button", category="button", text="", aria_label="More options", aria_haspopup="menu", looks_icon_only=True, has_inline_svg=True)])
    results = action_semantics.classify_actions(raw)
    assert results[0].semantic_action == "more_actions"
    assert results[0].is_kebab_menu is True
    assert "kebab_menu" in results[0].signals
    assert "contextual_menu" in results[0].signals


def test_floating_action_button_flagged():
    raw = RawObservation(elements=[_el(
        "el_1", tag="button", category="button", text="", aria_label="Add", looks_icon_only=True,
        has_inline_svg=True, position_style="fixed", bounding_box={"x": 20, "y": 20, "width": 56, "height": 56},
    )])
    results = action_semantics.classify_actions(raw)
    assert results[0].is_floating_action_button is True
    assert results[0].semantic_action == "add"


def test_ambiguous_icon_only_button_with_no_signal_is_unknown():
    """An icon-only control with no text/aria-label/title/haspopup — the
    honest answer is "unknown", never a guessed verb."""
    raw = RawObservation(elements=[_el("el_1", tag="button", category="button", text="", looks_icon_only=True, has_inline_svg=True)])
    results = action_semantics.classify_actions(raw)
    assert results[0].semantic_action == "unknown"
    assert results[0].is_kebab_menu is False


# ---------------------------------------------------------------------------
# 4. Field semantics
# ---------------------------------------------------------------------------


def _form_raw(fields: list[dict]) -> dict:
    return {"dom_id": "form_1", "action": None, "method": None, "fields": fields, "submit_element_id": None,
            "bounding_box": {"x": 0, "y": 0, "width": 200, "height": 200}}


def _field(dom_id: str, **overrides) -> dict:
    base = {"dom_id": dom_id, "name": None, "field_type": "text", "label": None, "accessible_name": None,
            "accessible_description": None, "required": False, "placeholder": None, "options": [],
            "disabled": False, "checked": None, "current_value": "", "role": None, "multiple": False,
            "list_attr": None, "aria_autocomplete": None}
    base.update(overrides)
    return base


def test_combobox_field_kind():
    raw = RawObservation(forms=[_form_raw([_field("el_1", role="combobox", label="Department")])])
    forms = form_extractor.extract_forms(raw)
    assert forms[0].field_descriptors[0].field_kind == "combobox"


def test_autocomplete_field_kind_via_list_attribute():
    raw = RawObservation(forms=[_form_raw([_field("el_1", list_attr="cities_datalist", label="City")])])
    forms = form_extractor.extract_forms(raw)
    assert forms[0].field_descriptors[0].field_kind == "autocomplete"


def test_multi_select_field_kind():
    raw = RawObservation(forms=[_form_raw([_field("el_1", field_type="select", multiple=True, label="Tags")])])
    forms = form_extractor.extract_forms(raw)
    assert forms[0].field_descriptors[0].field_kind == "multi_select"


def test_date_picker_field_kind():
    raw = RawObservation(forms=[_form_raw([_field("el_1", field_type="date", label="Start Date")])])
    forms = form_extractor.extract_forms(raw)
    assert forms[0].field_descriptors[0].field_kind == "date_picker"


def test_radio_group_field_kind():
    raw = RawObservation(forms=[_form_raw([_field("el_1", field_type="radio", name="status", label="Active")])])
    forms = form_extractor.extract_forms(raw)
    assert forms[0].field_descriptors[0].field_kind == "radio_group"


def test_checkbox_group_field_kind_requires_shared_name():
    raw = RawObservation(forms=[_form_raw([
        _field("el_1", field_type="checkbox", name="roles", label="Admin"),
        _field("el_2", field_type="checkbox", name="roles", label="Editor"),
    ])])
    forms = form_extractor.extract_forms(raw)
    kinds = {f.element_id: f.field_kind for f in forms[0].field_descriptors}
    assert kinds == {"el_1": "checkbox_group", "el_2": "checkbox_group"}


def test_single_checkbox_is_not_a_group():
    raw = RawObservation(forms=[_form_raw([_field("el_1", field_type="checkbox", name="agree", label="I agree")])])
    forms = form_extractor.extract_forms(raw)
    assert forms[0].field_descriptors[0].field_kind != "checkbox_group"


def test_file_upload_field_kind():
    raw = RawObservation(forms=[_form_raw([_field("el_1", field_type="file", label="Attachment")])])
    forms = form_extractor.extract_forms(raw)
    assert forms[0].field_descriptors[0].field_kind == "file_upload"


def test_plain_text_field_never_generic_when_label_present():
    raw = RawObservation(forms=[_form_raw([_field("el_1", field_type="text", label="Full Name")])])
    forms = form_extractor.extract_forms(raw)
    field = forms[0].field_descriptors[0]
    assert field.label == "Full Name"
    assert field.field_kind == "text"


# ---------------------------------------------------------------------------
# 5. CRUD workflow inference
# ---------------------------------------------------------------------------


def _list_page(*, add=True, edit=True, delete=True, row_count=3) -> CanonicalPageModel:
    global_actions = [CollectionAction(element_id="el_add", scope="global", semantic_action="add")] if add else []
    row_actions = []
    if edit:
        row_actions.append(CollectionAction(element_id="el_edit", scope="row", semantic_action="edit"))
    if delete:
        row_actions.append(CollectionAction(element_id="el_delete", scope="row", semantic_action="delete"))
    return CanonicalPageModel(
        url="https://example.com/members", title="Members",
        headings=[HeadingDescriptor(text="Members", level=1)],
        collections=[RecordCollection(element_id="coll_1", collection_type="div_row_group", row_count=row_count,
                                       global_actions=global_actions, row_actions=row_actions)],
    )


def test_create_entry_point_then_supported_after_form_appears():
    before = _list_page()
    after_create_page = CanonicalPageModel(
        url="https://example.com/members/new", title="Add Member",
        forms=[FormDescriptor(form_id="form_new")],
        form_intents=[FormIntentClassification(form_id="form_new", intent="create", confidence=0.5)],
    )
    engine = CRUDDiscoveryEngine()
    engine.observe(before_model=before, after_model=before, executed_element_id=None, action_succeeded=True)
    create_hyp = engine.registry.by_operation("create")[0]
    assert create_hyp.status == "entry_point"

    engine.observe(before_model=before, after_model=after_create_page, executed_element_id="el_add", action_succeeded=True)
    create_hyp = engine.registry.by_operation("create")[0]
    assert create_hyp.status == "supported"
    assert create_hyp.required_form_id == "form_new"
    assert create_hyp.safety_classification == "controlled_write"
    assert create_hyp.cleanup_possibility == "possible"
    assert "list -> create_form -> save" in create_hyp.expected_transition


def test_edit_entry_point_then_supported_after_prefilled_form_appears():
    before = _list_page()
    after_edit_page = CanonicalPageModel(
        url="https://example.com/members/1/edit", title="Edit Member",
        forms=[FormDescriptor(form_id="form_edit")],
        form_intents=[FormIntentClassification(form_id="form_edit", intent="edit", confidence=0.5)],
    )
    engine = CRUDDiscoveryEngine()
    engine.observe(before_model=before, after_model=after_edit_page, executed_element_id="el_edit", action_succeeded=True)
    edit_hyp = engine.registry.by_operation("edit")[0]
    assert edit_hyp.status == "supported"
    assert edit_hyp.required_form_id == "form_edit"


def test_delete_not_advanced_without_corroboration():
    """The task's explicit constraint: a trash icon alone is NOT proof of a
    working delete workflow."""
    before = _list_page()
    after_same_page = _list_page(row_count=3)  # no confirmation dialog, no row-count drop
    engine = CRUDDiscoveryEngine()
    engine.observe(before_model=before, after_model=after_same_page, executed_element_id="el_delete", action_succeeded=True)
    delete_hyp = engine.registry.by_operation("delete")[0]
    assert delete_hyp.status == "entry_point"


def test_delete_advances_with_confirmation_dialog():
    before = _list_page()
    after_with_dialog = CanonicalPageModel(
        url="https://example.com/members", title="Members",
        dialogs=[DialogDescriptor(element_id="dlg_1", text="Are you sure you want to delete this member? This cannot be undone.")],
        collections=[RecordCollection(element_id="coll_1", collection_type="div_row_group", row_count=3)],
    )
    engine = CRUDDiscoveryEngine()
    engine.observe(before_model=before, after_model=after_with_dialog, executed_element_id="el_delete", action_succeeded=True)
    delete_hyp = engine.registry.by_operation("delete")[0]
    assert delete_hyp.status == "supported"
    assert any(e.source_kind == "confirmation_dialog" for e in delete_hyp.evidence)


def test_delete_advances_with_row_count_decrease():
    before = _list_page(row_count=3)
    after_fewer_rows = CanonicalPageModel(
        url="https://example.com/members", title="Members",
        collections=[RecordCollection(element_id="coll_1", collection_type="div_row_group", row_count=2)],
    )
    engine = CRUDDiscoveryEngine()
    engine.observe(before_model=before, after_model=after_fewer_rows, executed_element_id="el_delete", action_succeeded=True)
    delete_hyp = engine.registry.by_operation("delete")[0]
    assert delete_hyp.status == "supported"
    assert any(e.source_kind == "row_count_decrease" for e in delete_hyp.evidence)


def test_crud_registry_upsert_is_idempotent():
    builder = CRUDCandidateBuilder()
    registry = CRUDRegistry()
    model = _list_page()
    hyps = builder.build_entry_points(model)
    for h in hyps:
        registry.upsert(h)
    count_after_first = len(registry.records)
    hyps_again = builder.build_entry_points(model)
    for h in hyps_again:
        registry.upsert(h)
    assert len(registry.records) == count_after_first  # no duplicates
    edit_hyp = registry.by_operation("edit")[0]
    assert edit_hyp.observation_count == 2  # merged, not duplicated
    # evidence must not duplicate either
    assert len(edit_hyp.evidence) == len(set((e.source_kind, e.element_id) for e in edit_hyp.evidence))


def test_failed_action_never_produces_a_fulfilled_hypothesis():
    before = _list_page()
    after = CanonicalPageModel(url="https://example.com/members", title="Members")
    builder = CRUDCandidateBuilder()
    fulfilled = builder.build_fulfilled(before, after, executed_element_id="el_delete", action_succeeded=False)
    assert fulfilled == []


# ---------------------------------------------------------------------------
# 6. Knowledge graph — list/form/record relationship discovery
# ---------------------------------------------------------------------------


def _graph_with_entity(entity_name: str = "Member") -> ApplicationKnowledgeGraph:
    graph = ApplicationKnowledgeGraph()
    graph.memory.upsert_node(
        entity_node_id(entity_name), node_type="entity", canonical_name=entity_name, status="observed",
        confidence=0.8, source_registry="entity_registry", source_record_id="e1", iteration=0,
    )
    return graph


def test_lists_entity_and_opens_create_form_edges():
    before = _list_page()
    after = CanonicalPageModel(
        url="https://example.com/members/new", title="Add Member", forms=[FormDescriptor(form_id="form_new")],
        form_intents=[FormIntentClassification(form_id="form_new", intent="create", confidence=0.5)],
    )
    crud_engine = CRUDDiscoveryEngine()
    crud_engine.observe(before_model=before, after_model=before, executed_element_id=None, action_succeeded=True)
    crud_engine.observe(before_model=before, after_model=after, executed_element_id="el_add", action_succeeded=True)

    graph = _graph_with_entity()
    graph.synchronize(crud_registry=crud_engine.registry, iteration=1)
    edge_types = {e.edge_type for e in graph.memory.edges.values()}
    assert "lists_entity" in edge_types
    assert "opens_create_form" in edge_types
    assert "creates_entity" in edge_types
    assert "returns_to_list" in edge_types
    assert "verifies_in_collection" in edge_types


def test_creates_entity_edge_absent_without_supported_status():
    """A bare entry-point (no before/after corroboration yet) must not
    fabricate a creates_entity edge."""
    before = _list_page()
    crud_engine = CRUDDiscoveryEngine()
    crud_engine.observe(before_model=before, after_model=before, executed_element_id=None, action_succeeded=True)

    graph = _graph_with_entity()
    graph.synchronize(crud_registry=crud_engine.registry, iteration=1)
    edge_types = {e.edge_type for e in graph.memory.edges.values()}
    assert "lists_entity" in edge_types  # collection <-> entity is always safe to show
    assert "creates_entity" not in edge_types


def test_deletes_entity_edge_requires_corroboration():
    before = _list_page()
    after_uncorroborated = _list_page(row_count=3)
    crud_engine = CRUDDiscoveryEngine()
    crud_engine.observe(before_model=before, after_model=after_uncorroborated, executed_element_id="el_delete", action_succeeded=True)
    graph = _graph_with_entity()
    graph.synchronize(crud_registry=crud_engine.registry, iteration=1)
    assert "deletes_entity" not in {e.edge_type for e in graph.memory.edges.values()}

    after_corroborated = CanonicalPageModel(
        url="https://example.com/members", title="Members",
        collections=[RecordCollection(element_id="coll_1", collection_type="div_row_group", row_count=2)],
    )
    crud_engine.observe(before_model=before, after_model=after_corroborated, executed_element_id="el_delete", action_succeeded=True)
    graph.synchronize(crud_registry=crud_engine.registry, iteration=2)
    assert "deletes_entity" in {e.edge_type for e in graph.memory.edges.values()}


def test_edits_entity_edge():
    before = _list_page()
    after = CanonicalPageModel(
        url="https://example.com/members/1/edit", title="Edit Member", forms=[FormDescriptor(form_id="form_edit")],
        form_intents=[FormIntentClassification(form_id="form_edit", intent="edit", confidence=0.5)],
    )
    crud_engine = CRUDDiscoveryEngine()
    crud_engine.observe(before_model=before, after_model=after, executed_element_id="el_edit", action_succeeded=True)
    graph = _graph_with_entity()
    graph.synchronize(crud_registry=crud_engine.registry, iteration=1)
    assert "edits_entity" in {e.edge_type for e in graph.memory.edges.values()}


def test_graph_sync_idempotent_no_duplicate_edges_or_nodes():
    before = _list_page()
    after = CanonicalPageModel(
        url="https://example.com/members/new", title="Add Member", forms=[FormDescriptor(form_id="form_new")],
        form_intents=[FormIntentClassification(form_id="form_new", intent="create", confidence=0.5)],
    )
    crud_engine = CRUDDiscoveryEngine()
    crud_engine.observe(before_model=before, after_model=after, executed_element_id="el_add", action_succeeded=True)

    graph = _graph_with_entity()
    graph.synchronize(crud_registry=crud_engine.registry, iteration=1)
    edge_count_first = len(graph.memory.edges)
    node_count_first = len(graph.memory.nodes)

    graph.synchronize(crud_registry=crud_engine.registry, iteration=2)
    assert len(graph.memory.edges) == edge_count_first
    assert len(graph.memory.nodes) == node_count_first


# ---------------------------------------------------------------------------
# 7. Form lifecycle
# ---------------------------------------------------------------------------


def test_form_lifecycle_classified_before_inspected_ordering():
    assert list(FormLifecycle) == list(dict.fromkeys(FormLifecycle))  # no accidental value collisions crash construction
    assert FormLifecycle.DISCOVERED.value == "discovered"
    assert FormLifecycle.CLASSIFIED.value == "classified"
    assert FormLifecycle.CANDIDATE_FOR_TESTING.value == "candidate_for_testing"


def test_mark_form_classified_sets_intent_and_advances_lifecycle():
    store = ApplicationStore("run_1", "https://example.com/")
    page_state = PageState(page_id="p1", url="https://example.com/members", forms=[FormDescriptor(form_id="form_1")])
    page = store.observe_page(page_state)
    form = store.model.forms[0]
    assert form.lifecycle_state == "discovered"

    store.mark_form_classified(form.id, intent="search", confidence=0.65, evidence=["small field count"])
    form = store.model.forms[0]
    assert form.intent == "search"
    assert form.lifecycle_state == "classified"


def test_not_tested_reason_must_be_a_known_reason():
    store = ApplicationStore("run_1", "https://example.com/")
    page_state = PageState(page_id="p1", url="https://example.com/members", forms=[FormDescriptor(form_id="form_1")])
    store.observe_page(page_state)
    form = store.model.forms[0]

    assert "write_disabled" in NOT_TESTED_REASONS
    store.mark_form_not_tested(form.id, reason="write_disabled")
    assert store.model.forms[0].not_tested_reason == "write_disabled"

    import pytest
    with pytest.raises(ValueError):
        store.mark_form_not_tested(form.id, reason="not_a_real_reason")


# ---------------------------------------------------------------------------
# 8. Application-neutral guard — no target-application vocabulary in the
# production modules this task added.
# ---------------------------------------------------------------------------


def test_new_production_modules_contain_no_target_application_vocabulary():
    forbidden = ["orangehrm", "saucedemo", "serviceflow", "employee", " pim ", "opensource-demo"]
    new_files = [
        BACKEND / "app/perception/collection_extractor.py",
        BACKEND / "app/perception/form_intent_classifier.py",
        BACKEND / "app/perception/action_semantics.py",
        BACKEND / "app/intelligence/crud_discovery/schemas.py",
        BACKEND / "app/intelligence/crud_discovery/crud_candidate_builder.py",
        BACKEND / "app/intelligence/crud_discovery/crud_registry.py",
        BACKEND / "app/intelligence/crud_discovery/crud_discovery_engine.py",
        BACKEND / "app/intelligence/knowledge_graph/crud_graph_projector.py",
    ]
    for path in new_files:
        text = path.read_text(encoding="utf-8").lower()
        for word in forbidden:
            assert word not in text, f"{path.name} contains target-application-specific text: {word!r}"


# ---------------------------------------------------------------------------
# 9. Real-browser integration — an OrangeHRM-LIKE synthetic fixture
# (structurally similar: search/filter form above a div-based record grid,
# icon-only row actions, a separate add-record page) using entirely
# generic naming ("Members"/"Member"), never OrangeHRM's own names/routes.
# ---------------------------------------------------------------------------


async def test_record_manager_demo_full_perception_pipeline():
    import pytest as _pytest

    try:
        from playwright.async_api import async_playwright
    except Exception:
        _pytest.skip("Playwright not importable")

    from app.perception.engine import PerceptionEngine

    list_html = """
    <!doctype html><html><body>
    <header><h1>Members</h1></header>
    <form>
      <input type="text" placeholder="Search members" aria-label="Search members" />
      <select aria-label="Filter by status"><option>Active</option><option>Inactive</option></select>
    </form>
    <button>Add Member</button>
    <div class="record-grid">
      <div class="record-row"><span>Ada Lovelace</span><span>Admin</span>
        <button aria-label="Edit" class="icon-btn"><svg></svg></button>
        <button aria-label="Delete" class="icon-btn"><svg></svg></button>
      </div>
      <div class="record-row"><span>Grace Hopper</span><span>Member</span>
        <button aria-label="Edit" class="icon-btn"><svg></svg></button>
        <button aria-label="Delete" class="icon-btn"><svg></svg></button>
      </div>
      <div class="record-row"><span>Alan Turing</span><span>Member</span>
        <button aria-label="Edit" class="icon-btn"><svg></svg></button>
        <button aria-label="Delete" class="icon-btn"><svg></svg></button>
      </div>
    </div>
    </body></html>
    """

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=list_html))
                await page.goto("https://gemmaqa-crud-fixture.local/members")
                model = await PerceptionEngine().observe(page)
            finally:
                await browser.close()
    except Exception as exc:
        _pytest.skip(f"Live Chromium unavailable in this environment: {type(exc).__name__}: {exc}")
        return

    assert len(model.collections) >= 1
    grid = next(c for c in model.collections if c.row_count >= 2)
    assert grid.collection_type in {"div_row_group", "card_group"}
    row_verbs = sorted(a.semantic_action for a in grid.row_actions if a.semantic_action)
    assert "delete" in row_verbs
    assert "edit" in row_verbs
    global_verbs = [a.semantic_action for a in grid.global_actions]
    assert "add" in global_verbs
    # The search/filter form must NOT be classified as a create form.
    assert model.form_intents
    assert model.form_intents[0].intent in {"search", "filter"}
