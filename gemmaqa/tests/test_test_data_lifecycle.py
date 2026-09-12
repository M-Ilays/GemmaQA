"""Safe test-data generation and temporary-record lifecycle (Turn 4):
field-constraint inference, valid/negative/boundary generation, the
Temporary Record Registry's cleanup state machine, creation/update/deletion
verification, and cleanup action planning.

Thirteen categories exercised here (see docs/TEST_DATA_LIFECYCLE.md):
unique value generation, constraint satisfaction, field dependencies,
autocomplete handling, date picker handling, temporary-record tracking,
created-record-only delete policy, cleanup success, cleanup failure,
duplicate prevention, no real PII, deterministic test mode, and
verification requirements.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.cleanup_planner import plan_cleanup_action  # noqa: E402
from app.agent.creation_verifier import verify_creation, verify_deletion, verify_update  # noqa: E402
from app.agent.field_constraint_inference import infer_constraint, infer_semantic_type  # noqa: E402
from app.agent.file_upload_fixtures import SAFE_UPLOAD_FIXTURES, default_safe_upload_fixture  # noqa: E402
from app.agent.temporary_record_registry import (  # noqa: E402
    MAX_CLEANUP_ATTEMPTS,
    CleanupPlan,
    TemporaryRecordRegistry,
)
from app.agent.test_data_generator import (  # noqa: E402
    build_prefix,
    generate_duplicate_probe,
    generate_negative_values,
    generate_valid_value,
)
from app.agent.test_data_schemas import FIELD_SEMANTIC_TYPES, FieldConstraint  # noqa: E402
from app.perception.models import (  # noqa: E402
    ActionSemantics,
    CanonicalPageModel,
    CollectionAction,
    CollectionRow,
    DialogDescriptor,
    RecordCollection,
)
from app.safety.policies import TEST_DATA_PREFIX  # noqa: E402
from app.schemas import FormFieldDescriptor  # noqa: E402

_AT = datetime(2026, 1, 1, 12, 0, 0)
_RUN_A = "run-aaaa"
_RUN_B = "run-bbbb"


def _field(**kwargs) -> FormFieldDescriptor:
    kwargs.setdefault("stable_id", "f1")
    kwargs.setdefault("field_id", "f1")
    return FormFieldDescriptor(**kwargs)


# ---------------------------------------------------------------------------
# 1. Unique value generation
# ---------------------------------------------------------------------------


def test_unique_value_generation_differs_by_seed():
    constraint = FieldConstraint(field_semantic_type="email")
    v1 = generate_valid_value("email", constraint, run_id=_RUN_A, seed=1, at=_AT)
    v2 = generate_valid_value("email", constraint, run_id=_RUN_A, seed=2, at=_AT)
    assert v1.generated_value != v2.generated_value
    assert v1.uniqueness_strategy == "run_scoped_unique"


def test_unique_value_generation_no_collisions_across_many_seeds():
    constraint = FieldConstraint(field_semantic_type="username")
    values = {
        generate_valid_value("username", constraint, run_id=_RUN_A, seed=i, at=_AT).generated_value
        for i in range(200)
    }
    assert len(values) == 200


# ---------------------------------------------------------------------------
# 2. Constraint satisfaction
# ---------------------------------------------------------------------------


def test_constraint_inference_captures_html_attributes():
    field = _field(field_type="text", label="Username", is_required=True, min_length=4, max_length=10)
    constraint = infer_constraint(field)
    assert constraint.required is True
    assert constraint.min_length == 4
    assert constraint.max_length == 10
    assert constraint.evidence  # every populated attribute is backed by evidence


def test_generated_value_respects_min_and_max_length():
    field = _field(field_type="text", label="Username", is_required=True, min_length=8, max_length=12)
    constraint = infer_constraint(field)
    value = generate_valid_value("username", constraint, run_id=_RUN_A, seed=1, at=_AT).generated_value
    assert 8 <= len(value) <= 12


def test_required_field_never_generates_empty_value():
    field = _field(field_type="text", label="Notes", is_required=True)
    constraint = infer_constraint(field)
    value = generate_valid_value("description", constraint, run_id=_RUN_A, seed=1, at=_AT).generated_value
    assert value != ""


# ---------------------------------------------------------------------------
# 3. Field dependencies
# ---------------------------------------------------------------------------


def test_constraint_records_dependent_field_keys():
    field = _field(field_type="date", label="End Date")
    constraint = infer_constraint(field, dependent_field_keys=["start_date_field"])
    assert constraint.depends_on_field_keys == ["start_date_field"]
    assert any("depends on" in e for e in constraint.evidence)


def test_date_range_value_declares_its_dependencies():
    constraint = FieldConstraint(field_semantic_type="date_range")
    value = generate_valid_value("date_range", constraint, run_id=_RUN_A, at=_AT)
    assert value.related_field_dependencies == ["date_range_start", "date_range_end"]


# ---------------------------------------------------------------------------
# 4. Autocomplete handling
# ---------------------------------------------------------------------------


def test_autocomplete_field_kind_infers_autocomplete_selection():
    field = _field(field_type="text", field_kind="autocomplete", label="Assignee")
    assert infer_semantic_type(field) == "autocomplete_selection"


def test_autocomplete_generation_prefers_existing_option():
    constraint = FieldConstraint(field_semantic_type="autocomplete_selection", options=["Jane Reviewer"])
    value = generate_valid_value("autocomplete_selection", constraint, run_id=_RUN_A, at=_AT)
    assert value.generated_value == "Jane Reviewer"
    assert value.uniqueness_strategy == "none"


# ---------------------------------------------------------------------------
# 5. Date picker handling
# ---------------------------------------------------------------------------


def test_date_picker_field_kind_infers_date():
    field = _field(field_type="date", field_kind="date_picker", label="Start Date")
    assert infer_semantic_type(field) == "date"


def test_date_picker_min_date_is_read_from_raw_string_not_min_value():
    # Mirrors the real pipeline: form_extractor never parses a date input's
    # min/max as a float (it isn't one) -- min_value stays None and the raw
    # ISO string lands in min_value_raw instead.
    field = _field(field_type="date", field_kind="date_picker", label="Start Date", min_value_raw="2026-03-01")
    constraint = infer_constraint(field)
    assert constraint.min_date == "2026-03-01"
    assert constraint.min_value is None  # never conflated with a numeric min


def test_date_generation_uses_inferred_min_date_when_present():
    constraint = FieldConstraint(field_semantic_type="date", min_date="2026-03-01")
    value = generate_valid_value("date", constraint, run_id=_RUN_A, at=_AT)
    assert value.generated_value == "2026-03-01"


# ---------------------------------------------------------------------------
# 6. Temporary-record tracking
# ---------------------------------------------------------------------------


def test_registry_tracks_created_record_through_verification():
    registry = TemporaryRecordRegistry(run_id=_RUN_A)
    entry = registry.register_created(record_type="employee", generated_identity="Alex Rivera")
    assert entry.current_state == "created"
    registry.mark_verified(entry.temporary_record_id, evidence=["list_row match"])
    assert registry.entries[entry.temporary_record_id].current_state == "verified"
    snapshot = registry.snapshot()
    assert snapshot["total_records"] == 1
    assert snapshot["by_state"] == {"verified": 1}


def test_registry_tracks_update_history():
    registry = TemporaryRecordRegistry(run_id=_RUN_A)
    entry = registry.register_created(record_type="employee", generated_identity="Alex Rivera")
    registry.mark_verified(entry.temporary_record_id, evidence=["e"])
    registry.mark_updated(
        entry.temporary_record_id, field_key="status", before_value="Active", after_value="Pending",
        verified=True, evidence=["toast"],
    )
    updated = registry.entries[entry.temporary_record_id]
    assert updated.current_state == "updated"
    assert len(updated.update_history) == 1
    assert updated.update_history[0].after_value == "Pending"


def test_mark_updated_refreshes_identity_when_that_field_changed():
    """Run 55588f75 edited First Name, then could not prove the detail page
    still showed the old identity, so Delete Contact was never planned."""
    registry = TemporaryRecordRegistry(run_id=_RUN_A)
    entry = registry.register_created(record_type="employee", generated_identity="Alex Rivera")
    registry.mark_verified(entry.temporary_record_id, evidence=["e"])
    registry.mark_updated(
        entry.temporary_record_id,
        field_key="firstName",
        before_value="Alex Rivera",
        after_value="Alex Rivera Updated",
        verified=True,
        evidence=["edit_submitted"],
    )
    updated = registry.entries[entry.temporary_record_id]
    assert updated.generated_identity == "Alex Rivera Updated"
    assert updated.current_state == "updated"


# ---------------------------------------------------------------------------
# 7. Created-record-only delete policy
# ---------------------------------------------------------------------------


def test_eligible_for_cleanup_requires_current_run_by_default():
    registry = TemporaryRecordRegistry(run_id=_RUN_A)
    entry = registry.register_created(record_type="employee", generated_identity="Alex Rivera")
    registry.mark_verified(entry.temporary_record_id, evidence=["e"])
    assert registry.eligible_for_cleanup(entry.temporary_record_id, current_run_id=_RUN_A) is True
    assert registry.eligible_for_cleanup(entry.temporary_record_id, current_run_id=_RUN_B) is False


def test_eligible_for_cleanup_cross_run_requires_explicit_opt_in():
    registry = TemporaryRecordRegistry(run_id=_RUN_A)
    entry = registry.register_created(record_type="employee", generated_identity="Alex Rivera")
    registry.mark_verified(entry.temporary_record_id, evidence=["e"])
    assert registry.eligible_for_cleanup(
        entry.temporary_record_id, current_run_id=_RUN_B, allow_cross_run_cleanup=True
    ) is True


def test_terminal_record_is_never_eligible_for_cleanup_again():
    registry = TemporaryRecordRegistry(run_id=_RUN_A)
    entry = registry.register_created(record_type="employee", generated_identity="Alex Rivera")
    registry.mark_verified(entry.temporary_record_id, evidence=["e"])
    registry.request_cleanup(entry.temporary_record_id, plan=CleanupPlan())
    registry.mark_delete_action_validated(entry.temporary_record_id)
    registry.mark_deleted(entry.temporary_record_id)
    registry.mark_absence_verified(entry.temporary_record_id)
    assert registry.eligible_for_cleanup(entry.temporary_record_id, current_run_id=_RUN_A) is False


# ---------------------------------------------------------------------------
# 8. Cleanup success
# ---------------------------------------------------------------------------


def test_cleanup_full_lifecycle_reaches_absence_verified():
    registry = TemporaryRecordRegistry(run_id=_RUN_A)
    entry = registry.register_created(record_type="employee", generated_identity="Alex Rivera")
    registry.mark_verified(entry.temporary_record_id, evidence=["e"])
    assert registry.request_cleanup(entry.temporary_record_id, plan=CleanupPlan()) is True
    assert registry.mark_delete_action_validated(entry.temporary_record_id) is True
    assert registry.mark_deleted(entry.temporary_record_id) is True
    assert registry.mark_absence_verified(entry.temporary_record_id) is True
    final = registry.entries[entry.temporary_record_id]
    assert final.current_state == "absence_verified"
    assert final.is_terminal() is True
    assert final.cleanup_result.succeeded is True
    assert final.cleanup_result.absence_verified is True


def test_cleanup_planner_plans_delete_then_confirm_then_stops():
    registry = TemporaryRecordRegistry(run_id=_RUN_A)
    entry = registry.register_created(
        record_type="employee", generated_identity="GemmaQA_TEST_abc123_x", collection_element_id="grid_1",
    )
    registry.mark_verified(entry.temporary_record_id, evidence=["e"])

    row = CollectionRow(stable_id="row_1", cell_values=["GemmaQA_TEST_abc123_x"])
    delete_action = CollectionAction(
        stable_id="del_1", element_id="del_1", scope="row", row_id="row_1", semantic_action="delete",
    )
    collection = RecordCollection(
        element_id="grid_1", visible_rows=[row], row_actions=[delete_action],
    )
    model_no_dialog = CanonicalPageModel(url="https://example.test/list", collections=[collection])

    action = plan_cleanup_action(entry, canonical_model=model_no_dialog)
    assert action is not None
    assert action.element_id == "del_1"

    # Simulate the registry transition that happens right before this action
    # is validated/executed (mirrors AgentController._run_cleanup_pass).
    registry.request_cleanup(entry.temporary_record_id, plan=CleanupPlan(delete_control_element_id="del_1"))

    # No confirmation dialog open -> nothing left to plan.
    assert plan_cleanup_action(entry, canonical_model=model_no_dialog) is None


def test_cleanup_planner_finds_confirmation_control_in_open_dialog():
    registry = TemporaryRecordRegistry(run_id=_RUN_A)
    entry = registry.register_created(record_type="employee", generated_identity="Alex Rivera")
    registry.mark_verified(entry.temporary_record_id, evidence=["e"])
    registry.request_cleanup(entry.temporary_record_id, plan=CleanupPlan())

    dialog = DialogDescriptor(element_id="dlg_1", is_open=True, contained_element_ids=["confirm_btn"])
    confirm_semantics = ActionSemantics(element_id="confirm_btn", semantic_action="confirm")
    model = CanonicalPageModel(url="https://example.test/list", dialogs=[dialog], action_semantics=[confirm_semantics])

    action = plan_cleanup_action(entry, canonical_model=model)
    assert action is not None
    assert action.element_id == "confirm_btn"


# ---------------------------------------------------------------------------
# 9. Cleanup failure
# ---------------------------------------------------------------------------


def test_cleanup_failure_is_bounded_and_becomes_manual_after_max_attempts():
    registry = TemporaryRecordRegistry(run_id=_RUN_A)
    entry = registry.register_created(record_type="employee", generated_identity="Alex Rivera")
    registry.mark_verified(entry.temporary_record_id, evidence=["e"])

    for _ in range(MAX_CLEANUP_ATTEMPTS):
        registry.mark_cleanup_failed(entry.temporary_record_id, error="delete control not found")

    final = registry.entries[entry.temporary_record_id]
    assert final.current_state == "manual_cleanup_required"
    assert final.cleanup_result.attempts == MAX_CLEANUP_ATTEMPTS
    manual = registry.manual_cleanup_report()
    assert len(manual) == 1
    assert manual[0]["temporary_record_id"] == entry.temporary_record_id
    assert "Alex Rivera" in manual[0]["instructions"]


def test_cleanup_failure_never_retries_indefinitely():
    registry = TemporaryRecordRegistry(run_id=_RUN_A)
    entry = registry.register_created(record_type="employee", generated_identity="Alex Rivera")
    registry.mark_verified(entry.temporary_record_id, evidence=["e"])
    for _ in range(MAX_CLEANUP_ATTEMPTS + 5):
        registry.mark_cleanup_failed(entry.temporary_record_id, error="still failing")
    final = registry.entries[entry.temporary_record_id]
    assert final.cleanup_result.attempts == MAX_CLEANUP_ATTEMPTS + 5
    assert final.current_state == "manual_cleanup_required"


# ---------------------------------------------------------------------------
# 10. Duplicate prevention
# ---------------------------------------------------------------------------


def test_duplicate_probe_reuses_existing_value_as_negative_case():
    probe = generate_duplicate_probe("email", "GemmaQA_TEST_abc_1@example.com", run_id=_RUN_A)
    assert probe.generated_value == "GemmaQA_TEST_abc_1@example.com"
    assert probe.data_category == "invalid_duplicate"
    assert probe.validity_intent == "negative"
    assert probe.uniqueness_strategy == "fixed_duplicate_probe"


def test_negative_generation_includes_duplicate_relevant_boundary_cases():
    constraint = FieldConstraint(field_semantic_type="username", required=True, min_length=4, max_length=10)
    negatives = generate_negative_values("username", constraint, run_id=_RUN_A, at=_AT)
    categories = {n.data_category for n in negatives}
    assert "invalid_empty" in categories
    assert "invalid_below_min" in categories
    assert "invalid_above_max" in categories


def test_negative_values_never_contain_injection_payloads():
    constraint = FieldConstraint(field_semantic_type="free_text")
    negatives = generate_negative_values("free_text", constraint, run_id=_RUN_A, at=_AT)
    for n in negatives:
        assert "<script" not in n.generated_value.lower()
        assert "drop table" not in n.generated_value.lower()
        assert "; rm -rf" not in n.generated_value


# ---------------------------------------------------------------------------
# 11. No real PII
# ---------------------------------------------------------------------------


def test_generated_names_come_only_from_the_safe_synthetic_pool():
    from app.agent.test_data_generator import _FIRST_NAMES, _LAST_NAMES, _MIDDLE_NAMES

    constraint = FieldConstraint(field_semantic_type="first_name")
    for seed in range(20):
        value = generate_valid_value("first_name", constraint, run_id=_RUN_A, seed=seed, at=_AT)
        assert value.generated_value in _FIRST_NAMES
        assert value.sensitive_classification == "synthetic_pii_like"

    for seed in range(20):
        value = generate_valid_value("last_name", constraint, run_id=_RUN_A, seed=seed, at=_AT)
        assert value.generated_value in _LAST_NAMES

    for seed in range(20):
        value = generate_valid_value("middle_name", constraint, run_id=_RUN_A, seed=seed, at=_AT)
        assert value.generated_value in _MIDDLE_NAMES


def test_email_and_username_are_test_data_prefixed_not_real_pii():
    constraint = FieldConstraint(field_semantic_type="email")
    value = generate_valid_value("email", constraint, run_id=_RUN_A, seed=1, at=_AT)
    assert value.generated_value.startswith(TEST_DATA_PREFIX.lower())
    assert value.generated_value.endswith("@example.com")


def test_phone_number_uses_reserved_fictional_range():
    constraint = FieldConstraint(field_semantic_type="phone_number")
    value = generate_valid_value("phone_number", constraint, run_id=_RUN_A, seed=1, at=_AT)
    assert "555-01" in value.generated_value


def test_file_upload_uses_repository_fixture_never_arbitrary_file():
    field = _field(field_type="file", field_kind="file_upload", label="Attachment")
    semantic_type = infer_semantic_type(field)
    assert semantic_type == "file_upload"
    constraint = infer_constraint(field)
    value = generate_valid_value(semantic_type, constraint, run_id=_RUN_A, at=_AT)
    assert value.generation_source == "fixture_file"
    assert Path(value.generated_value) in SAFE_UPLOAD_FIXTURES.values()
    assert Path(value.generated_value).exists()
    assert default_safe_upload_fixture(accept="image/png") == SAFE_UPLOAD_FIXTURES["image"]
    assert default_safe_upload_fixture(accept=None) == SAFE_UPLOAD_FIXTURES["text"]


# ---------------------------------------------------------------------------
# 12. Deterministic test mode
# ---------------------------------------------------------------------------


def test_same_seed_and_run_id_produce_identical_value():
    constraint = FieldConstraint(field_semantic_type="username")
    v1 = generate_valid_value("username", constraint, run_id=_RUN_A, seed=7, at=_AT)
    v2 = generate_valid_value("username", constraint, run_id=_RUN_A, seed=7, at=_AT)
    assert v1.generated_value == v2.generated_value


def test_prefix_is_stable_for_same_run_id_and_timestamp():
    p1 = build_prefix(_RUN_A, at=_AT)
    p2 = build_prefix(_RUN_A, at=_AT)
    assert p1 == p2
    assert build_prefix(_RUN_B, at=_AT) != p1


def test_prefix_stays_short_enough_for_ordinary_text_fields():
    """A live submission was refused with `street1 (...) is longer than the
    maximum allowed length (40)` because the prefix alone was 36 characters.
    The prefix has to leave room for the value it tags."""
    assert len(build_prefix(_RUN_A, at=_AT)) <= 24


def test_prefixed_address_values_fit_a_forty_character_field():
    constraint = FieldConstraint(field_semantic_type="street_address")
    value = generate_valid_value("street_address", constraint, run_id=_RUN_A, seed=6, at=_AT)
    assert len(value.generated_value) <= 40
    # Still identifiable as GemmaQA test data — shortening must not cost the tag.
    assert "GemmaQA_TEST_" in value.generated_value


def test_prefixed_name_and_place_values_fit_a_forty_character_field():
    for semantic_type in ("city", "state_province", "country", "free_text", "employee_identifier"):
        constraint = FieldConstraint(field_semantic_type=semantic_type)
        value = generate_valid_value(semantic_type, constraint, run_id=_RUN_A, seed=6, at=_AT)
        assert len(value.generated_value) <= 40, f"{semantic_type}: {value.generated_value!r}"


def test_field_semantic_types_vocabulary_is_closed_and_covers_spec_list():
    required = {
        "first_name", "middle_name", "last_name", "username", "email", "phone_number",
        "employee_identifier", "free_text", "description", "date", "date_range", "status",
        "role", "dropdown_option", "autocomplete_selection", "boolean", "number", "decimal",
        "url", "address",
    }
    assert required.issubset(FIELD_SEMANTIC_TYPES)


# ---------------------------------------------------------------------------
# 13. Verification requirements
# ---------------------------------------------------------------------------


def test_bare_successful_click_is_not_treated_as_creation():
    result = verify_creation(
        before_url="https://example.test/list", after_url="https://example.test/list", after_visible_text="",
    )
    assert result.verified is False
    assert result.signals == []


def test_toast_and_list_row_evidence_together_verify_creation():
    collection = RecordCollection(
        element_id="grid_1", visible_rows=[CollectionRow(stable_id="r1", cell_values=["GemmaQA_TEST_x"])],
    )
    result = verify_creation(
        before_url="https://example.test/new", after_url="https://example.test/list",
        after_visible_text="Record saved successfully",
        after_toasts=["Record saved successfully"],
        expected_value="GemmaQA_TEST_x",
        after_collections=[collection],
    )
    assert result.verified is True
    assert "list_row" in result.signals
    assert "success_toast" in result.signals


def test_failure_toast_contradicts_and_blocks_verification():
    result = verify_creation(
        before_url="https://example.test/new", after_url="https://example.test/new",
        after_visible_text="", after_toasts=["Error: required field missing"],
    )
    assert result.verified is False
    assert result.contradicted is True


def test_verify_update_requires_actual_change_to_expected_value():
    assert verify_update("Active", "Pending", expected_value="Pending") is True
    assert verify_update("Active", "Active", expected_value="Active") is False
    assert verify_update("Active", "Draft", expected_value="Pending") is False


def test_verify_deletion_requires_absence_evidence():
    result = verify_deletion(before_row_count=5, after_row_count=4, identity="GemmaQA_TEST_x")
    assert result.verified is True
    assert "list_row" in result.signals

    result_no_evidence = verify_deletion(
        before_row_count=5, after_row_count=5, identity="GemmaQA_TEST_x", after_visible_text="GemmaQA_TEST_x still here",
    )
    assert result_no_evidence.verified is False
