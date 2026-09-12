"""Universal Page Perception Engine: dom_extractor, accessibility_extractor,
interactive_detector, region_classifier, navigation_extractor, form_extractor,
table_extractor, image_extractor, dialog_extractor, state_builder,
evidence_merger, unknown_detector, and the engine.py orchestrator."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.perception import (  # noqa: E402
    accessibility_extractor,
    dialog_extractor,
    dom_extractor,
    evidence_merger,
    form_extractor,
    image_extractor,
    interactive_detector,
    navigation_extractor,
    region_classifier,
    state_builder,
    table_extractor,
    unknown_detector,
)
from app.perception.dom_extractor import RawObservation
from app.perception.engine import PerceptionEngine
from app.perception.models import ConfidenceScore, EvidenceReference
from app.schemas import InteractiveElement, NetworkEntry


def _el(dom_id, **overrides):
    base = {
        "dom_id": dom_id,
        "tag": "div",
        "role": None,
        "type": None,
        "name": None,
        "id_attr": None,
        "test_id": None,
        "text": "",
        "aria_label": None,
        "accessible_name": "",
        "accessible_description": None,
        "label": None,
        "href": None,
        "placeholder": None,
        "title_attr": None,
        "is_visible": True,
        "is_enabled": True,
        "required": False,
        "disabled": False,
        "checked": None,
        "aria_expanded": None,
        "aria_selected": None,
        "aria_current": None,
        "aria_haspopup": None,
        "tabindex": None,
        "is_native_focusable": False,
        "cursor_style": None,
        "has_onclick_attr": False,
        "looks_icon_only": False,
        "current_value": None,
        "available_options": [],
        "category": "other",
        "bounding_box": {"x": 0, "y": 0, "width": 40, "height": 40},
        "landmark_region_id": None,
        "container_role": None,
        "selector_hint": f'[data-gemmaqa-id="{dom_id}"]',
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# dom_extractor
# ---------------------------------------------------------------------------


def test_raw_observation_from_raw_dict_applies_caps_and_defaults():
    raw = RawObservation.from_raw_dict({"url": "https://example.com/", "elements": [_el("el_1")]})
    assert raw.url == "https://example.com/"
    assert len(raw.elements) == 1
    assert raw.forms == []


def test_extract_headings_preserves_level_across_h1_to_h6():
    raw = RawObservation(headings=[{"dom_id": "h1", "text": "Title", "level": 1}, {"dom_id": "h2", "text": "Sub", "level": 3}])
    headings = dom_extractor.extract_headings(raw)
    levels = {h.text: h.level for h in headings}
    assert levels == {"Title": 1, "Sub": 3}


def test_extract_text_blocks_wraps_visible_text_as_summary():
    raw = RawObservation(visible_text="Hello world")
    blocks = dom_extractor.extract_text_blocks(raw)
    assert len(blocks) == 1
    assert blocks[0].block_type == "summary"
    assert blocks[0].text == "Hello world"


def test_extract_text_blocks_empty_when_no_text():
    assert dom_extractor.extract_text_blocks(RawObservation()) == []


def test_extract_links_classifies_relative_url_as_internal():
    """Regression: a relative href ("/cart") was wrongly classified as external
    because same_origin()/origin_of() don't resolve relative URLs."""
    raw = RawObservation(
        url="https://example.com/products",
        elements=[_el("el_1", tag="a", category="link", href="/cart", text="Cart", accessible_name="Cart")],
    )
    links = dom_extractor.extract_links(raw)
    assert len(links) == 1
    assert links[0].is_external is False


def test_extract_links_classifies_absolute_external_url():
    raw = RawObservation(
        url="https://example.com/products",
        elements=[_el("el_1", tag="a", category="link", href="https://other.com/x", text="Ext")],
    )
    links = dom_extractor.extract_links(raw)
    assert links[0].is_external is True


def test_extract_links_ignores_non_link_elements():
    raw = RawObservation(
        url="https://example.com/",
        elements=[_el("el_1", tag="button", category="button", text="Go")],
    )
    assert dom_extractor.extract_links(raw) == []


# ---------------------------------------------------------------------------
# accessibility_extractor
# ---------------------------------------------------------------------------


def test_accessibility_index_infers_implicit_role_when_missing():
    raw = RawObservation(elements=[_el("el_1", tag="a", role=None, href="/x")])
    index = accessibility_extractor.build_accessibility_index(raw)
    assert index["el_1"].role == "link"


def test_accessibility_index_uses_explicit_role_over_implicit():
    raw = RawObservation(elements=[_el("el_1", tag="div", role="button")])
    index = accessibility_extractor.build_accessibility_index(raw)
    assert index["el_1"].role == "button"


def test_accessibility_index_falls_back_to_aria_current_for_selected():
    raw = RawObservation(elements=[_el("el_1", aria_selected=None, aria_current="page")])
    index = accessibility_extractor.build_accessibility_index(raw)
    assert index["el_1"].is_selected is True


def test_accessibility_index_joins_heading_level():
    raw = RawObservation(
        elements=[_el("el_1", tag="h2")],
        headings=[{"dom_id": "el_1", "text": "Section", "level": 2}],
    )
    index = accessibility_extractor.build_accessibility_index(raw)
    assert index["el_1"].heading_level == 2


def test_parent_child_map_uses_landmark_region_id():
    raw = RawObservation(elements=[_el("el_1", landmark_region_id="region_1")])
    parent_map = accessibility_extractor.build_parent_child_map(raw)
    assert parent_map["el_1"] == "region_1"


# ---------------------------------------------------------------------------
# interactive_detector
# ---------------------------------------------------------------------------


def test_native_tag_detected_as_interactive():
    raw = RawObservation(elements=[_el("el_1", tag="button", category="button", text="Go")])
    detected = interactive_detector.detect_interactive_elements(raw, {})
    assert len(detected) == 1
    assert "native_tag" in detected[0].evidence


def test_pointer_style_div_detected_as_heuristic_interactive():
    """The audit's key gap: a non-semantic <div> with only a pointer cursor."""
    raw = RawObservation(elements=[_el("el_1", tag="div", cursor_style="pointer")])
    detected = interactive_detector.detect_interactive_elements(raw, {})
    assert len(detected) == 1
    assert detected[0].source == "heuristic"
    assert "pointer_style" in detected[0].evidence
    assert detected[0].confidence < 1.0


def test_plain_div_with_no_signal_is_not_detected():
    raw = RawObservation(elements=[_el("el_1", tag="div")])
    assert interactive_detector.detect_interactive_elements(raw, {}) == []


def test_invisible_element_is_never_detected():
    raw = RawObservation(elements=[_el("el_1", tag="button", is_visible=False)])
    assert interactive_detector.detect_interactive_elements(raw, {}) == []


def test_tabindex_element_detected():
    raw = RawObservation(elements=[_el("el_1", tag="div", tabindex=0)])
    detected = interactive_detector.detect_interactive_elements(raw, {})
    assert "tabindex" in detected[0].evidence


def test_aria_role_only_element_detected_with_aria_source():
    raw = RawObservation(elements=[_el("el_1", tag="div", role="button")])
    detected = interactive_detector.detect_interactive_elements(raw, {})
    assert "aria_role" in detected[0].evidence
    assert detected[0].source == "aria"


def test_expandable_control_detected_via_aria_expanded_presence():
    raw = RawObservation(elements=[_el("el_1", tag="button", category="button", aria_expanded=False)])
    detected = interactive_detector.detect_interactive_elements(raw, {})
    assert "expandable_control" in detected[0].evidence


def test_custom_dropdown_trigger_detected_via_aria_haspopup():
    raw = RawObservation(elements=[_el("el_1", tag="div", aria_haspopup="listbox")])
    detected = interactive_detector.detect_interactive_elements(raw, {})
    assert "custom_dropdown_trigger" in detected[0].evidence


def test_table_row_action_detected_via_row_action_ids():
    raw = RawObservation(
        elements=[_el("el_btn", tag="button", category="button", text="Edit")],
        tables=[{"dom_id": "t1", "rows": [{"row_index": 0, "cell_values": [], "row_action_ids": ["el_btn"]}]}],
    )
    detected = interactive_detector.detect_interactive_elements(raw, {})
    assert "table_row_action" in detected[0].evidence


def test_navigable_card_detected_only_for_large_bare_pointer_elements():
    """navigable_card is additive to pointer_style, not exclusive with it — a
    large bare pointer container gets both; a small one only gets
    pointer_style (it's still interactive, just not card-sized)."""
    raw = RawObservation(
        elements=[
            _el("card_1", tag="article", cursor_style="pointer", bounding_box={"x": 0, "y": 0, "width": 200, "height": 150}),
            _el("tiny_1", tag="div", cursor_style="pointer", bounding_box={"x": 0, "y": 0, "width": 10, "height": 10}),
        ]
    )
    detected = interactive_detector.detect_interactive_elements(raw, {})
    by_id = {e.element_id: e for e in detected}
    assert "navigable_card" in by_id["card_1"].evidence
    assert "pointer_style" in by_id["card_1"].evidence
    assert "navigable_card" not in by_id["tiny_1"].evidence
    assert "pointer_style" in by_id["tiny_1"].evidence


def test_navigable_card_not_flagged_for_a_semantically_native_button():
    """A native <button> with cursor:pointer (the browser default) must not be
    tagged navigable_card just because it's large — it's already classified via
    native_tag, which is a stronger, more specific signal."""
    raw = RawObservation(
        elements=[_el("btn_1", tag="button", category="button", cursor_style="pointer", bounding_box={"x": 0, "y": 0, "width": 300, "height": 200})]
    )
    detected = interactive_detector.detect_interactive_elements(raw, {})
    assert "navigable_card" not in detected[0].evidence
    assert "native_tag" in detected[0].evidence


def test_icon_only_helper():
    assert interactive_detector.is_icon_only({"looks_icon_only": True}) is True
    assert interactive_detector.is_icon_only({"looks_icon_only": False}) is False


def test_duplicate_signals_on_same_element_collapse_into_one_with_combined_evidence():
    """A <button role="button"> matches BOTH native_tag and aria_role — must
    produce ONE InteractiveElement, not two, with both reasons recorded."""
    raw = RawObservation(elements=[_el("el_1", tag="button", role="button", category="button")])
    detected = interactive_detector.detect_interactive_elements(raw, {})
    assert len(detected) == 1
    assert {"native_tag", "aria_role"} <= set(detected[0].evidence)


# ---------------------------------------------------------------------------
# region_classifier
# ---------------------------------------------------------------------------


def test_header_landmark_classified_as_header():
    raw = RawObservation(regions_raw=[{"dom_id": "r1", "tag": "header", "role": None, "bounding_box": {}}])
    regions = region_classifier.classify_landmark_regions(raw)
    assert regions[0].region_type == "header"


def test_nav_near_top_classified_as_primary_navigation():
    raw = RawObservation(
        regions_raw=[{"dom_id": "r1", "tag": "nav", "bounding_box": {"x": 0, "y": 10, "width": 800, "height": 40}}]
    )
    regions = region_classifier.classify_landmark_regions(raw)
    assert regions[0].region_type == "primary_navigation"


def test_narrow_left_nav_classified_as_secondary_navigation():
    raw = RawObservation(
        regions_raw=[{"dom_id": "r1", "tag": "nav", "bounding_box": {"x": 10, "y": 300, "width": 200, "height": 500}}]
    )
    regions = region_classifier.classify_landmark_regions(raw)
    assert regions[0].region_type == "secondary_navigation"


def test_wide_mid_page_nav_classified_as_content_navigation():
    raw = RawObservation(
        regions_raw=[{"dom_id": "r1", "tag": "nav", "bounding_box": {"x": 300, "y": 400, "width": 600, "height": 40}}]
    )
    regions = region_classifier.classify_landmark_regions(raw)
    assert regions[0].region_type == "content_navigation"


def test_footer_with_short_links_classified_as_legal_region():
    raw = RawObservation(
        elements=[
            _el("l1", tag="a", category="link", text="ToS", landmark_region_id="r1"),
            _el("l2", tag="a", category="link", text="Privacy", landmark_region_id="r1"),
        ],
        regions_raw=[{"dom_id": "r1", "tag": "footer", "bounding_box": {}}],
    )
    regions = region_classifier.classify_landmark_regions(raw)
    assert regions[0].region_type == "legal_region"
    assert regions[0].confidence.value < 0.7  # stated as a lower-confidence guess


def test_footer_with_long_links_stays_generic_footer():
    raw = RawObservation(
        elements=[_el("l1", tag="a", category="link", text="Learn more about our company history", landmark_region_id="r1")],
        regions_raw=[{"dom_id": "r1", "tag": "footer", "bounding_box": {}}],
    )
    regions = region_classifier.classify_landmark_regions(raw)
    assert regions[0].region_type == "footer"


def test_main_and_aside_classified_generically():
    raw = RawObservation(
        regions_raw=[
            {"dom_id": "r1", "tag": "main", "bounding_box": {}},
            {"dom_id": "r2", "tag": "aside", "bounding_box": {}},
        ]
    )
    regions = region_classifier.classify_landmark_regions(raw)
    by_id = {r.stable_id: r for r in regions}
    assert by_id["r1"].region_type == "main_content"
    assert by_id["r2"].region_type == "sidebar"


def test_synthesize_structural_regions_covers_forms_tables_dialogs():
    raw = RawObservation(
        forms=[{"dom_id": "f1", "fields": []}],
        tables=[{"dom_id": "t1"}],
        dialogs=[{"dom_id": "d1", "contained_element_ids": ["el_1"]}],
    )
    regions = region_classifier.synthesize_structural_regions(raw)
    types = {r.region_type for r in regions}
    assert types == {"form_region", "data_table", "dialog"}


def test_card_group_region_requires_at_least_two_cards():
    one_card = [InteractiveElement(element_id="c1", tag="div", evidence=["navigable_card"])]
    assert region_classifier.synthesize_card_group_region(one_card) == []
    two_cards = [
        InteractiveElement(element_id="c1", tag="div", evidence=["navigable_card"]),
        InteractiveElement(element_id="c2", tag="div", evidence=["navigable_card"]),
    ]
    groups = region_classifier.synthesize_card_group_region(two_cards)
    assert len(groups) == 1
    assert groups[0].region_type == "card_group"
    assert set(groups[0].child_region_ids) == {"c1", "c2"}


def test_utility_region_requires_at_least_two_icon_only_controls_outside_landmarks():
    raw = RawObservation(
        elements=[
            _el("i1", looks_icon_only=True, landmark_region_id=None),
            _el("i2", looks_icon_only=True, landmark_region_id=None),
        ]
    )
    regions = region_classifier.synthesize_utility_region(raw)
    assert len(regions) == 1
    assert regions[0].region_type == "utility_region"


def test_visual_regions_bucket_by_geometry():
    raw = RawObservation(
        elements=[
            _el("top1", bounding_box={"x": 500, "y": 10, "width": 20, "height": 20}),
            _el("bottom1", bounding_box={"x": 500, "y": 790, "width": 20, "height": 20}),
        ]
    )
    regions = region_classifier.classify_visual_regions(raw, viewport_width=1280, viewport_height=800)
    hints = {r.layout_hint for r in regions}
    assert "top" in hints
    assert "bottom" in hints


# ---------------------------------------------------------------------------
# navigation_extractor
# ---------------------------------------------------------------------------


def test_extract_navigation_regions_preserves_href_and_current_state():
    raw = RawObservation(
        navigation_containers=[
            {"role": "nav", "aria_label": "Main", "items": [{"text": "Home", "href": "/", "is_current": "page"}]}
        ]
    )
    regions = navigation_extractor.extract_navigation_regions(raw)
    assert len(regions) == 1
    assert regions[0].items[0].target_url == "/"
    assert regions[0].items[0].is_current is True


def test_extract_breadcrumbs_assigns_ordinal():
    raw = RawObservation(breadcrumbs=[{"text": "Home", "href": "/"}, {"text": "Products", "href": None}])
    crumbs = navigation_extractor.extract_breadcrumbs(raw)
    assert [c.ordinal for c in crumbs] == [0, 1]


def test_extract_pagination_carries_numeric_state():
    raw = RawObservation(pagination={"items": [{"text": "2", "is_current": True}], "current_page": 2, "total_pages": 5})
    pages = navigation_extractor.extract_pagination(raw)
    assert pages[0].current_page == 2
    assert pages[0].total_pages == 5


def test_extract_pagination_empty_when_no_pagination():
    assert navigation_extractor.extract_pagination(RawObservation()) == []


def test_extract_tabs_finds_selected_tab():
    raw = RawObservation(
        elements=[
            _el("tab_1", tag="button", category="tab", role="tab", text="Details", aria_selected=True),
            _el("tab_2", tag="button", category="tab", role="tab", text="Reviews", aria_selected=False),
        ]
    )
    groups = navigation_extractor.extract_tabs(raw)
    assert len(groups) == 1
    assert groups[0].selected_tab_id == "tab_1"


def test_extract_tabs_empty_when_no_tabs():
    assert navigation_extractor.extract_tabs(RawObservation()) == []


def test_classify_link_locality_resolves_relative_urls():
    assert navigation_extractor.classify_link_locality("/x", "https://example.com/a") is False
    assert navigation_extractor.classify_link_locality("https://other.com/x", "https://example.com/a") is True
    assert navigation_extractor.classify_link_locality(None, "https://example.com/a") is None


# ---------------------------------------------------------------------------
# form_extractor
# ---------------------------------------------------------------------------


def test_extract_forms_builds_both_legacy_fields_and_field_descriptors():
    raw = RawObservation(
        forms=[
            {
                "dom_id": "f1",
                "action": "/submit",
                "method": "post",
                "submit_element_id": "el_submit",
                "bounding_box": {},
                "fields": [
                    {
                        "dom_id": "el_q",
                        "name": "q",
                        "field_type": "text",
                        "label": "Query",
                        "accessible_name": "Query",
                        "accessible_description": None,
                        "required": True,
                        "placeholder": None,
                        "options": [],
                        "disabled": False,
                        "checked": None,
                        "current_value": "abc",
                    }
                ],
            }
        ]
    )
    forms = form_extractor.extract_forms(raw)
    assert len(forms) == 1
    form = forms[0]
    assert form.form_id == "f1"
    assert len(form.fields) == 1 and form.fields[0].name == "q"
    assert len(form.field_descriptors) == 1
    assert form.field_descriptors[0].accessible_name == "Query"
    assert form.field_descriptors[0].is_required is True


def test_extract_forms_skips_forms_with_no_dom_id():
    raw = RawObservation(forms=[{"fields": []}])
    assert form_extractor.extract_forms(raw) == []


# ---------------------------------------------------------------------------
# table_extractor
# ---------------------------------------------------------------------------


def test_extract_tables_links_row_actions_to_the_row():
    raw = RawObservation(
        tables=[
            {
                "dom_id": "t1",
                "headers": ["Name", "Amount"],
                "row_count": 1,
                "bounding_box": {},
                "rows": [{"row_index": 0, "cell_values": ["Widget", "9.99"], "row_action_ids": ["el_edit", "el_delete"]}],
            }
        ]
    )
    tables = table_extractor.extract_tables(raw)
    assert len(tables) == 1
    assert tables[0].rows[0].row_action_element_ids == ["el_edit", "el_delete"]


def test_extract_tables_infers_numeric_column_type():
    raw = RawObservation(
        tables=[
            {
                "dom_id": "t1",
                "headers": ["Name", "Amount"],
                "row_count": 2,
                "rows": [
                    {"row_index": 0, "cell_values": ["Widget", "9.99"], "row_action_ids": []},
                    {"row_index": 1, "cell_values": ["Gadget", "19.99"], "row_action_ids": []},
                ],
            }
        ]
    )
    tables = table_extractor.extract_tables(raw)
    columns = {c.header_text: c.data_type for c in tables[0].columns}
    assert columns["Amount"] == "numeric"
    assert columns["Name"] == "text"


def test_extract_tables_preserves_legacy_sample_rows_and_headers():
    raw = RawObservation(
        tables=[
            {
                "dom_id": "t1",
                "headers": ["A"],
                "row_count": 1,
                "rows": [{"row_index": 0, "cell_values": ["x"], "row_action_ids": []}],
            }
        ]
    )
    table = table_extractor.extract_tables(raw)[0]
    assert table.headers == ["A"]
    assert table.sample_rows == [["x"]]


# ---------------------------------------------------------------------------
# image_extractor
# ---------------------------------------------------------------------------


def test_extract_images_with_alt_text_is_higher_confidence():
    raw = RawObservation(images=[{"dom_id": "img_1", "alt": "Logo", "src": "/logo.png", "surrounding_text": "Home"}])
    images = image_extractor.extract_images(raw)
    assert images[0].alt_text == "Logo"
    assert images[0].confidence.value == 1.0


def test_extract_images_without_alt_text_is_lower_confidence():
    raw = RawObservation(images=[{"dom_id": "img_1", "alt": None, "src": "/x.png", "surrounding_text": ""}])
    images = image_extractor.extract_images(raw)
    assert images[0].confidence.value < 1.0


# ---------------------------------------------------------------------------
# dialog_extractor
# ---------------------------------------------------------------------------


def test_extract_dialogs_links_contained_elements():
    raw = RawObservation(dialogs=[{"dom_id": "d1", "role": "dialog", "text": "Confirm?", "contained_element_ids": ["el_ok", "el_cancel"]}])
    dialogs = dialog_extractor.extract_dialogs(raw)
    assert dialogs[0].contained_element_ids == ["el_ok", "el_cancel"]
    assert dialogs[0].dialog_type == "dialog"


def test_extract_dialogs_alertdialog_role_maps_to_modal_type():
    raw = RawObservation(dialogs=[{"dom_id": "d1", "role": "alertdialog", "text": "Warning"}])
    assert dialog_extractor.extract_dialogs(raw)[0].dialog_type == "modal"


def test_extract_alerts_preserves_severity():
    raw = RawObservation(alerts=[{"text": "Something broke", "severity": "error"}])
    alerts = dialog_extractor.extract_alerts(raw)
    assert alerts[0].severity == "error"


# ---------------------------------------------------------------------------
# state_builder
# ---------------------------------------------------------------------------


def _base_state_kwargs():
    return dict(url="https://example.com/", tabs=[], dialogs=[], interactive_elements=[], pagination=[], regions=[])


def test_state_builder_is_deterministic_for_identical_input():
    a = state_builder.build_page_state(**_base_state_kwargs())
    b = state_builder.build_page_state(**_base_state_kwargs())
    assert a.fingerprint == b.fingerprint


def test_state_builder_changes_fingerprint_when_dialog_opens():
    from app.perception.models import DialogDescriptor

    without = state_builder.build_page_state(**_base_state_kwargs())
    with_dialog = state_builder.build_page_state(
        **{**_base_state_kwargs(), "dialogs": [DialogDescriptor(element_id="d1", is_open=True)]}
    )
    assert without.fingerprint != with_dialog.fingerprint


def test_state_builder_changes_fingerprint_when_tab_selection_changes():
    from app.perception.models import TabDescriptor, TabGroup

    tab_a = TabGroup(tabs=[TabDescriptor(element_id="t1"), TabDescriptor(element_id="t2")], selected_tab_id="t1")
    tab_b = TabGroup(tabs=[TabDescriptor(element_id="t1"), TabDescriptor(element_id="t2")], selected_tab_id="t2")
    fp_a = state_builder.build_page_state(**{**_base_state_kwargs(), "tabs": [tab_a]}).fingerprint
    fp_b = state_builder.build_page_state(**{**_base_state_kwargs(), "tabs": [tab_b]}).fingerprint
    assert fp_a != fp_b


def test_state_builder_changes_fingerprint_when_element_expands():
    el_closed = InteractiveElement(element_id="e1", tag="button", is_expanded=False)
    el_open = InteractiveElement(element_id="e1", tag="button", is_expanded=True)
    fp_closed = state_builder.build_page_state(**{**_base_state_kwargs(), "interactive_elements": [el_closed]}).fingerprint
    fp_open = state_builder.build_page_state(**{**_base_state_kwargs(), "interactive_elements": [el_open]}).fingerprint
    assert fp_closed != fp_open


def test_state_builder_changes_fingerprint_for_active_filter_toggle():
    unchecked = InteractiveElement(element_id="f1", tag="input", category="checkbox", checked=False)
    checked = InteractiveElement(element_id="f1", tag="input", category="checkbox", checked=True)
    fp_off = state_builder.build_page_state(**{**_base_state_kwargs(), "interactive_elements": [unchecked]}).fingerprint
    fp_on = state_builder.build_page_state(**{**_base_state_kwargs(), "interactive_elements": [checked]}).fingerprint
    assert fp_off != fp_on


def test_state_builder_changes_fingerprint_for_search_state():
    empty = InteractiveElement(element_id="s1", tag="input", input_type="search", current_value="")
    filled = InteractiveElement(element_id="s1", tag="input", input_type="search", current_value="widgets")
    fp_empty = state_builder.build_page_state(**{**_base_state_kwargs(), "interactive_elements": [empty]}).fingerprint
    fp_filled = state_builder.build_page_state(**{**_base_state_kwargs(), "interactive_elements": [filled]}).fingerprint
    assert fp_empty != fp_filled


def test_state_builder_changes_fingerprint_for_pagination_state():
    from app.perception.models import PaginationDescriptor

    fp_1 = state_builder.build_page_state(**{**_base_state_kwargs(), "pagination": [PaginationDescriptor(current_page=1, total_pages=5)]}).fingerprint
    fp_2 = state_builder.build_page_state(**{**_base_state_kwargs(), "pagination": [PaginationDescriptor(current_page=2, total_pages=5)]}).fingerprint
    assert fp_1 != fp_2


def test_state_builder_changes_fingerprint_for_known_role():
    fp_anon = state_builder.build_page_state(**_base_state_kwargs(), known_role=None).fingerprint
    fp_admin = state_builder.build_page_state(**_base_state_kwargs(), known_role="admin").fingerprint
    assert fp_anon != fp_admin


def test_state_builder_includes_visible_regions_in_contributing_fields():
    descriptor = state_builder.build_page_state(**_base_state_kwargs())
    assert "visible_regions" in descriptor.contributing_fields
    assert "role" in descriptor.contributing_fields


# ---------------------------------------------------------------------------
# unknown_detector
# ---------------------------------------------------------------------------


def test_unclaimed_visible_element_becomes_unknown_component():
    raw = RawObservation(elements=[_el("el_1", tabindex=0)])
    unknown = unknown_detector.detect_unknown_components(raw, claimed_dom_ids=set())
    assert len(unknown) == 1
    assert unknown[0].element_id == "el_1"
    assert "tabindex" in unknown[0].detection_reason


def test_claimed_element_is_not_flagged_as_unknown():
    raw = RawObservation(elements=[_el("el_1", tabindex=0)])
    unknown = unknown_detector.detect_unknown_components(raw, claimed_dom_ids={"el_1"})
    assert unknown == []


def test_invisible_element_is_never_flagged_as_unknown():
    raw = RawObservation(elements=[_el("el_1", tabindex=0, is_visible=False)])
    assert unknown_detector.detect_unknown_components(raw, claimed_dom_ids=set()) == []


# ---------------------------------------------------------------------------
# evidence_merger
# ---------------------------------------------------------------------------


def test_combine_evidence_dedupes_plain_strings():
    combined = evidence_merger.combine_evidence(["a", "b"], ["b", "c"])
    assert combined == ["a", "b", "c"]


def test_combine_evidence_dedupes_evidence_references_by_id():
    ref = EvidenceReference(evidence_id="e1", kind="manual")
    combined = evidence_merger.combine_evidence([ref], [ref])
    assert len(combined) == 1


def test_merge_confidence_plain_floats_takes_max():
    assert evidence_merger.merge_confidence(0.5, 0.9, 0.3) == 0.9


def test_merge_confidence_confidence_scores_takes_max_value():
    a = ConfidenceScore(value=0.4, basis="heuristic")
    b = ConfidenceScore(value=0.8, basis="direct_observation")
    merged = evidence_merger.merge_confidence(a, b)
    assert merged.value == 0.8
    assert merged.basis == "direct_observation"
    assert "heuristic" in merged.notes


def test_merge_duplicate_elements_combines_evidence_and_keeps_first_as_base():
    items = [
        InteractiveElement(element_id="e1", tag="button", evidence=["native_tag"], confidence=1.0),
        InteractiveElement(element_id="e1", tag="button", evidence=["aria_role"], confidence=0.6),
    ]
    merged = evidence_merger.merge_duplicate_elements(items, lambda e: e.element_id)
    assert len(merged) == 1
    assert set(merged[0].evidence) == {"native_tag", "aria_role"}
    assert merged[0].confidence == 1.0


def test_merge_duplicate_elements_is_a_no_op_for_unique_keys():
    items = [
        InteractiveElement(element_id="e1", tag="button"),
        InteractiveElement(element_id="e2", tag="a"),
    ]
    merged = evidence_merger.merge_duplicate_elements(items, lambda e: e.element_id)
    assert len(merged) == 2


# ---------------------------------------------------------------------------
# engine.py orchestrator
# ---------------------------------------------------------------------------


def _rich_raw_observation() -> RawObservation:
    return RawObservation(
        url="https://example.com/inventory",
        title="Inventory",
        visible_text="Some products",
        elements=[
            _el("el_link", tag="a", category="link", href="/cart", text="Cart", accessible_name="Cart"),
            _el("el_card", tag="article", cursor_style="pointer", bounding_box={"x": 0, "y": 0, "width": 200, "height": 200}),
            _el("el_unknown", tabindex=-1),  # matched raw selector but not focusable, no other signal
        ],
        headings=[{"dom_id": "el_h", "text": "Inventory", "level": 1}],
        forms=[{"dom_id": "f1", "fields": [], "bounding_box": {}}],
        tables=[{"dom_id": "t1", "headers": [], "row_count": 0, "rows": [], "bounding_box": {}}],
        images=[{"dom_id": "img1", "alt": "Logo", "src": "/logo.png", "surrounding_text": ""}],
        dialogs=[],
        alerts=[],
        navigation_containers=[],
        breadcrumbs=[],
        pagination=None,
        regions_raw=[{"dom_id": "region_1", "tag": "header", "bounding_box": {}}],
    )


def test_engine_build_from_raw_produces_a_fully_populated_canonical_model():
    engine = PerceptionEngine()
    model = engine._build_from_raw(
        _rich_raw_observation(), screenshot_path="/tmp/s.png", known_role=None, network_entries=[]
    )
    assert model.url == "https://example.com/inventory"
    assert model.headings and model.headings[0].text == "Inventory"
    assert model.links and model.links[0].is_external is False
    assert model.forms and model.forms[0].form_id == "f1"
    assert model.tables and model.tables[0].table_id == "t1"
    assert model.images and model.images[0].alt_text == "Logo"
    assert model.regions  # header + form_region + data_table + card_group at least
    assert model.screenshot_reference is not None
    assert model.state_fingerprint


def test_engine_unknown_element_not_claimed_by_any_extractor_is_preserved():
    engine = PerceptionEngine()
    model = engine._build_from_raw(_rich_raw_observation(), screenshot_path=None, known_role=None, network_entries=[])
    unknown_ids = {u.element_id for u in model.unknown_components}
    assert "el_unknown" in unknown_ids
    assert "el_link" not in unknown_ids  # claimed by extract_links / interactive_detector


def test_engine_is_deterministic_for_identical_raw_input():
    engine = PerceptionEngine()
    raw = _rich_raw_observation()
    model_a = engine._build_from_raw(raw, screenshot_path=None, known_role=None, network_entries=[])
    model_b = engine._build_from_raw(raw, screenshot_path=None, known_role=None, network_entries=[])
    assert model_a.state_fingerprint == model_b.state_fingerprint
    assert len(model_a.interactive_elements) == len(model_b.interactive_elements)


def test_engine_maps_network_entries_into_evidence():
    engine = PerceptionEngine()
    entries = [NetworkEntry(method="GET", url="https://example.com/api", status=500, failed=True)]
    model = engine._build_from_raw(RawObservation(url="https://example.com/"), screenshot_path=None, known_role=None, network_entries=entries)
    assert len(model.network_evidence) == 1
    assert model.network_evidence[0].http_status == 500


def test_engine_result_round_trips_through_serialization():
    engine = PerceptionEngine()
    model = engine._build_from_raw(_rich_raw_observation(), screenshot_path=None, known_role=None, network_entries=[])
    restored = type(model).from_dict(model.to_dict())
    assert restored.url == model.url
    assert restored.state_fingerprint == model.state_fingerprint


def test_engine_never_produces_business_specific_region_names():
    engine = PerceptionEngine()
    model = engine._build_from_raw(_rich_raw_observation(), screenshot_path=None, known_role=None, network_entries=[])
    forbidden = {"customer", "invoice", "product", "job", "contact"}
    for region in model.regions:
        for word in forbidden:
            assert word not in region.region_type.lower()


async def test_dom_extractor_ids_never_collide_with_page_observer_ids_live():
    """Regression: PageObserver (app/browser/observer.py) and dom_extractor
    each stamp `data-gemmaqa-id` using their OWN independent, zero-seeded
    counter. When both run against the same live page in sequence (exactly
    what controller.py._observe() does — PageObserver first, then
    PerceptionEngine second), dom_extractor minting a fresh id for something
    PageObserver never tagged (e.g. an <img>) could land on the SAME literal
    id PageObserver already gave a completely different element — verified
    live on a plain nav/tabs/dropdown/image/footer page before the fix
    (dom_extractor's counter now seeds from `document.querySelectorAll(
    '[data-gemmaqa-id]').length` instead of 0)."""
    import pytest as _pytest

    try:
        from playwright.async_api import async_playwright
    except Exception:
        _pytest.skip("Playwright not importable")

    from app.browser.console_monitor import ConsoleMonitor
    from app.browser.network_monitor import NetworkMonitor
    from app.browser.observer import PageObserver

    html = """
    <!doctype html><html><body>
    <header><nav aria-label="Main"><a href="/home">Home</a><a href="/products">Products</a></nav></header>
    <div role="tablist"><button role="tab" aria-selected="true">Overview</button>
    <button role="tab" aria-selected="false">Reviews</button></div>
    <button aria-haspopup="listbox">Sort by</button>
    <img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBTAA7"
         alt="Quarterly sales chart trending upward" width="300" height="200">
    <footer><a href="/about">About</a><a href="https://twitter.com/x">Twitter</a></footer>
    </body></html>
    """

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route(
                    "**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
                )
                await page.goto("https://gemmaqa-id-collision-test.local/")

                observer = PageObserver(ConsoleMonitor(), NetworkMonitor())
                state = await observer.observe(page)
                model = await PerceptionEngine().observe(page)
            finally:
                await browser.close()
    except Exception as exc:
        _pytest.skip(f"Live Chromium unavailable in this environment: {type(exc).__name__}: {exc}")
        return

    page_observer_ids = {e.element_id for e in state.interactive_elements}
    image_ids = {i.element_id for i in model.images}
    assert image_ids, "fixture must actually produce an image to test the collision"
    assert not (image_ids & page_observer_ids), (
        "an image id minted by dom_extractor collided with a PageObserver-assigned "
        "interactive-element id — the two independent id counters are not coordinated"
    )
