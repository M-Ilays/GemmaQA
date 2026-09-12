"""Canonical Page Model (docs/CANONICAL_PAGE_MODEL.md): an application-neutral
representation of everything observable on a page. Covers construction,
validation, serialization, backward compatibility of the extended existing
schemas, and the deterministic PageState adapter."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.perception.models import (  # noqa: E402
    AlertDescriptor,
    BreadcrumbDescriptor,
    CanonicalPageModel,
    ConfidenceScore,
    DialogDescriptor,
    EvidenceReference,
    HeadingDescriptor,
    LinkDescriptor,
    NavigationItem,
    NavigationRegion,
    NetworkEvidence,
    PageRegion,
    PaginationDescriptor,
    PageStateDescriptor,
    TabDescriptor,
    TabGroup,
    TextBlockDescriptor,
    UnknownComponent,
    VisualRegion,
)
from app.schemas import (  # noqa: E402
    FormDescriptor,
    FormField,
    FormFieldDescriptor,
    InteractiveElement,
    NetworkEntry,
    PageState,
    TableColumnDescriptor,
    TableDescriptor,
    TableRowDescriptor,
)


# ---------------------------------------------------------------------------
# Backward compatibility of extended existing schemas
# ---------------------------------------------------------------------------


def test_interactive_element_still_constructs_with_only_original_fields():
    """Every field added to InteractiveElement must be optional/defaulted —
    existing observer.py/frontier.py construction code must be unaffected."""
    el = InteractiveElement(element_id="el_1", tag="button", accessible_name="Go")
    assert el.stable_id is None
    assert el.source == "dom"
    assert el.confidence == 1.0
    assert el.evidence == []
    assert el.status == "observed"


def test_form_descriptor_still_constructs_with_only_original_fields():
    form = FormDescriptor(form_id="f1", fields=[FormField(name="q")])
    assert form.field_descriptors == []
    assert form.confidence == 1.0


def test_table_descriptor_still_constructs_with_only_original_fields():
    table = TableDescriptor(table_id="t1", headers=["A"], row_count=1)
    assert table.columns == []
    assert table.rows == []


def test_form_field_descriptor_is_a_new_additive_companion_not_a_replacement():
    # The old FormField must still exist and work unmodified.
    old = FormField(name="q", field_type="text")
    assert old.field_type == "text"
    new = FormFieldDescriptor(stable_id="ffd_1", field_type="text", label="Query")
    assert new.status == "observed"


def test_table_row_descriptor_links_row_actions_to_the_row():
    row = TableRowDescriptor(
        stable_id="row_1",
        row_index=0,
        cell_values=["Item A", "Active"],
        row_action_element_ids=["el_edit_1", "el_delete_1"],
    )
    assert row.row_action_element_ids == ["el_edit_1", "el_delete_1"]


# ---------------------------------------------------------------------------
# ConfidenceScore / EvidenceReference validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [-0.1, 1.1, 2.0, -5])
def test_confidence_score_rejects_out_of_range_values(value):
    with pytest.raises(ValidationError):
        ConfidenceScore(value=value)


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
def test_confidence_score_accepts_boundary_values(value):
    assert ConfidenceScore(value=value).value == value


def test_evidence_reference_rejects_unknown_kind():
    with pytest.raises(ValidationError):
        EvidenceReference(kind="not_a_real_kind")


def test_evidence_reference_accepts_known_kinds():
    for kind in ("screenshot", "dom_snapshot", "network", "console", "manual"):
        assert EvidenceReference(kind=kind).kind == kind


# ---------------------------------------------------------------------------
# Controlled-vocabulary validation on descriptors
# ---------------------------------------------------------------------------


def test_page_region_rejects_business_specific_region_type():
    with pytest.raises(ValidationError):
        PageRegion(region_type="invoice")


@pytest.mark.parametrize("region_type", ["header", "nav", "main", "aside", "footer", "unknown"])
def test_page_region_accepts_generic_region_types(region_type):
    assert PageRegion(region_type=region_type).region_type == region_type


def test_heading_descriptor_rejects_invalid_level():
    with pytest.raises(ValidationError):
        HeadingDescriptor(level=7)
    with pytest.raises(ValidationError):
        HeadingDescriptor(level=0)


def test_dialog_descriptor_rejects_invalid_dialog_type():
    with pytest.raises(ValidationError):
        DialogDescriptor(dialog_type="lightbox")


def test_alert_descriptor_rejects_invalid_severity():
    with pytest.raises(ValidationError):
        AlertDescriptor(severity="critical-ish")


def test_text_block_rejects_invalid_block_type():
    with pytest.raises(ValidationError):
        TextBlockDescriptor(block_type="advertisement")


def test_visual_region_rejects_invalid_layout_hint():
    with pytest.raises(ValidationError):
        VisualRegion(layout_hint="diagonal")


def test_perceived_element_rejects_invalid_source_and_status():
    with pytest.raises(ValidationError):
        HeadingDescriptor(source="magic")
    with pytest.raises(ValidationError):
        HeadingDescriptor(status="vibes")


# ---------------------------------------------------------------------------
# CanonicalPageModel construction / validation
# ---------------------------------------------------------------------------


def test_canonical_page_model_requires_absolute_url():
    with pytest.raises(ValidationError):
        CanonicalPageModel(url="")
    with pytest.raises(ValidationError):
        CanonicalPageModel(url="/relative/path")


def test_canonical_page_model_derives_domain_from_url():
    m = CanonicalPageModel(url="https://app.example.com:8443/settings")
    assert m.domain == "app.example.com"


def test_canonical_page_model_explicit_domain_is_preserved():
    m = CanonicalPageModel(url="https://example.com/", domain="custom.example.com")
    assert m.domain == "custom.example.com"


def test_canonical_page_model_rejects_duplicate_stable_ids_within_one_collection():
    shared_id = "dup-1"
    with pytest.raises(ValidationError):
        CanonicalPageModel(
            url="https://example.com/",
            headings=[
                HeadingDescriptor(stable_id=shared_id, text="A"),
                HeadingDescriptor(stable_id=shared_id, text="B"),
            ],
        )


def test_canonical_page_model_allows_distinct_stable_ids_across_collections():
    m = CanonicalPageModel(
        url="https://example.com/",
        headings=[HeadingDescriptor(stable_id="h1", text="A")],
        text_blocks=[TextBlockDescriptor(stable_id="t1", text="B")],
    )
    assert len(m.headings) == 1
    assert len(m.text_blocks) == 1


def test_canonical_page_model_allows_the_same_stable_id_across_different_collections():
    """The same physical DOM element can legitimately appear in more than one
    semantic view at once — e.g. a clickable <h4><a>...</a></h4> is both a
    HeadingDescriptor and a LinkDescriptor. That must not be rejected."""
    shared_id = "el_007"
    m = CanonicalPageModel(
        url="https://example.com/",
        headings=[HeadingDescriptor(stable_id=shared_id, text="Clickable heading")],
        links=[LinkDescriptor(stable_id=shared_id, target_url="https://example.com/x")],
    )
    assert m.headings[0].stable_id == m.links[0].stable_id == shared_id


def test_canonical_page_model_has_no_business_specific_fields():
    """Contract check: field names must stay application-neutral."""
    forbidden = {"customer", "invoice", "product", "job", "contact"}
    field_names = set(CanonicalPageModel.model_fields.keys())
    for name in field_names:
        for word in forbidden:
            assert word not in name.lower(), f"business-specific field name leaked: {name}"


def test_canonical_page_model_reuses_existing_schemas_not_duplicates():
    """forms/tables/interactive_elements must be the SAME classes as app.schemas,
    not parallel redefinitions — this is the literal "reuse instead of
    duplicating" requirement."""
    assert CanonicalPageModel.model_fields["forms"].annotation == list[FormDescriptor]
    assert CanonicalPageModel.model_fields["tables"].annotation == list[TableDescriptor]
    assert CanonicalPageModel.model_fields["interactive_elements"].annotation == list[InteractiveElement]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_to_dict_and_from_dict_round_trip():
    m = CanonicalPageModel(
        url="https://example.com/page",
        title="A Page",
        headings=[HeadingDescriptor(text="Welcome", level=1)],
        links=[LinkDescriptor(target_url="https://example.com/x", is_external=False)],
    )
    data = m.to_dict()
    assert isinstance(data, dict)
    restored = CanonicalPageModel.from_dict(data)
    assert restored.url == m.url
    assert restored.headings[0].text == "Welcome"
    assert restored.links[0].target_url == "https://example.com/x"


def test_to_json_and_from_json_round_trip():
    m = CanonicalPageModel(url="https://example.com/page", title="A Page")
    text = m.to_json()
    assert isinstance(text, str)
    restored = CanonicalPageModel.from_json(text)
    assert restored.url == m.url
    assert restored.page_id == m.page_id


def test_datetimes_serialize_as_json_safe_strings():
    m = CanonicalPageModel(url="https://example.com/")
    data = m.to_dict()
    assert isinstance(data["observed_at"], str)


# ---------------------------------------------------------------------------
# from_page_state deterministic adapter
# ---------------------------------------------------------------------------


def _sample_page_state() -> PageState:
    return PageState(
        page_id="p1",
        url="https://example.com/inventory",
        title="Inventory",
        headings=["Products"],
        visible_text_summary="Some visible text",
        breadcrumbs=["Home", "Products"],
        navigation_items=["Home", "Cart"],
        interactive_elements=[
            InteractiveElement(
                element_id="el_1",
                tag="a",
                category="link",
                href="https://example.com/cart",
                accessible_name="Cart",
                is_visible=True,
            ),
            InteractiveElement(
                element_id="el_2",
                tag="a",
                category="link",
                href="https://external.com/x",
                accessible_name="External",
                is_visible=True,
            ),
            InteractiveElement(element_id="el_3", tag="button", category="button", accessible_name="Save"),
        ],
        forms=[FormDescriptor(form_id="f1", fields=[FormField(name="q", field_type="text")])],
        tables=[TableDescriptor(table_id="t1", headers=["A", "B"], row_count=2)],
        tabs=["Details", "Reviews"],
        dialogs=["A dialog"],
        modals=["A modal"],
        toasts=["Saved!"],
        alerts=["Something failed"],
        pagination_controls=["Next"],
        network_entries=[
            NetworkEntry(method="GET", url="https://example.com/api", status=500, failed=True)
        ],
        screenshot_path="/tmp/shot.png",
        state_fingerprint="abc123",
    )


def test_from_page_state_preserves_url_title_domain_fingerprint():
    m = CanonicalPageModel.from_page_state(_sample_page_state())
    assert m.url == "https://example.com/inventory"
    assert m.title == "Inventory"
    assert m.domain == "example.com"
    assert m.state_fingerprint == "abc123"
    assert m.page_state is not None
    assert m.page_state.fingerprint == "abc123"


def test_from_page_state_classifies_internal_vs_external_links():
    m = CanonicalPageModel.from_page_state(_sample_page_state())
    by_url = {link.target_url: link for link in m.links}
    assert by_url["https://example.com/cart"].is_external is False
    assert by_url["https://external.com/x"].is_external is True
    # A plain button must never show up as a link.
    assert all(link.target_url != "" for link in m.links)


def test_from_page_state_wraps_flat_tabs_into_a_tab_group():
    m = CanonicalPageModel.from_page_state(_sample_page_state())
    assert len(m.tabs) == 1
    labels = [t.text for t in m.tabs[0].tabs]
    assert labels == ["Details", "Reviews"]


def test_from_page_state_wraps_navigation_items_into_a_navigation_region():
    m = CanonicalPageModel.from_page_state(_sample_page_state())
    assert len(m.navigation_regions) == 1
    assert [i.text for i in m.navigation_regions[0].items] == ["Home", "Cart"]


def test_from_page_state_reuses_forms_and_tables_verbatim():
    page = _sample_page_state()
    m = CanonicalPageModel.from_page_state(page)
    assert m.forms == page.forms
    assert m.tables == page.tables
    assert isinstance(m.forms[0], FormDescriptor)
    assert isinstance(m.tables[0], TableDescriptor)


def test_from_page_state_reuses_interactive_elements_verbatim():
    page = _sample_page_state()
    m = CanonicalPageModel.from_page_state(page)
    assert m.interactive_elements == page.interactive_elements


def test_from_page_state_maps_dialogs_and_alerts():
    m = CanonicalPageModel.from_page_state(_sample_page_state())
    dialog_types = {(d.dialog_type, d.text) for d in m.dialogs}
    assert ("dialog", "A dialog") in dialog_types
    assert ("modal", "A modal") in dialog_types
    severities = {(a.severity, a.text) for a in m.alerts}
    assert ("error", "Something failed") in severities
    assert ("info", "Saved!") in severities


def test_from_page_state_maps_network_entries():
    m = CanonicalPageModel.from_page_state(_sample_page_state())
    assert len(m.network_evidence) == 1
    entry = m.network_evidence[0]
    assert entry.method == "GET"
    assert entry.http_status == 500
    assert entry.failed is True


def test_from_page_state_maps_screenshot_reference():
    m = CanonicalPageModel.from_page_state(_sample_page_state())
    assert m.screenshot_reference is not None
    assert m.screenshot_reference.kind == "screenshot"
    assert m.screenshot_reference.reference == "/tmp/shot.png"


def test_from_page_state_headings_default_to_level_one_documented_limitation():
    """PageState only preserves heading TEXT (h1/h2/h3 collapsed together), so the
    adapter cannot recover the real level — this documents that honestly rather
    than guessing, per "do not add hypothesis logic"."""
    m = CanonicalPageModel.from_page_state(_sample_page_state())
    assert all(h.level == 1 for h in m.headings)


def test_from_page_state_produces_empty_images_and_regions_not_invented_data():
    """PageState doesn't extract images or DOM landmark regions yet — the
    adapter must leave those empty rather than fabricate placeholder data."""
    m = CanonicalPageModel.from_page_state(_sample_page_state())
    assert m.images == []
    assert m.regions == []
    assert m.visual_regions == []
    assert m.unknown_components == []


def test_from_page_state_result_passes_full_model_validation():
    """The adapter's own output must never trip the model's own validators
    (duplicate stable_id, invalid url, ...)."""
    page = _sample_page_state()
    m = CanonicalPageModel.from_page_state(page)
    # Round-trip through validation explicitly.
    CanonicalPageModel.model_validate(m.model_dump())


def test_from_page_state_handles_a_minimal_empty_page_without_error():
    page = PageState(page_id="p2", url="https://example.com/", title="")
    m = CanonicalPageModel.from_page_state(page)
    assert m.headings == []
    assert m.links == []
    assert m.forms == []
    assert m.network_evidence == []
