"""Submission outcomes, address-family test data, and learn-from-rejection retry.

Every test here traces to a real failure observed running GemmaQA against a live
contact-management application: it filled a create form, the server answered
`400 Bad Request`, and the run recorded a completed create — then re-opened the
same form to submit the same refused values again until the budget ran out.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.field_constraint_inference import infer_constraint  # noqa: E402
from app.agent.form_workflow import GenericFormWorkflow  # noqa: E402
from app.agent.submission_outcome import (  # noqa: E402
    SUBMISSION_ACCEPTED,
    SUBMISSION_REJECTED_SERVER,
    SUBMISSION_REJECTED_VALIDATION,
    SUBMISSION_UNKNOWN,
    attribute_fields,
    classify_submission,
)
from app.agent.test_data_generator import (  # noqa: E402
    generate_conservative_value,
    generate_valid_value,
)
from app.schemas import (  # noqa: E402
    FormDescriptor,
    FormField,
    InteractiveElement,
    NetworkEntry,
    PageState,
)
from app.utils.ids import new_id  # noqa: E402

# The Add Contact form exactly as observed live.
CONTACT_LABELS = [
    "* First Name:",
    "* Last Name:",
    "Date of Birth:",
    "Email:",
    "Phone:",
    "Street Address 1:",
    "Street Address 2:",
    "City:",
    "State or Province:",
    "Postal Code:",
    "Country:",
]


def _fields() -> list[FormField]:
    return [
        FormField(name=None, field_type="text", label=label, element_id=f"el_{i:03d}")
        for i, label in enumerate(CONTACT_LABELS, start=2)
    ]


def _form(fields: list[FormField] | None = None) -> FormDescriptor:
    return FormDescriptor(form_id="form_015", fields=fields or _fields(), submit_element_id="el_013")


def _state(
    *,
    url: str = "https://app.example.com/addContact",
    forms: list[FormDescriptor] | None = None,
    network: list[NetworkEntry] | None = None,
    alerts: list[str] | None = None,
) -> PageState:
    return PageState(
        page_id=new_id(),
        url=url,
        title="Add Contact",
        forms=forms if forms is not None else [_form()],
        network_entries=network or [],
        alerts=alerts or [],
        interactive_elements=[
            InteractiveElement(element_id="el_013", tag="button", category="button", accessible_name="Submit")
        ],
    )


# ===========================================================================
# A — address-family semantic types (root cause of the invalid payload)
# ===========================================================================


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Street Address 1:", "street_address"),
        ("Street Address 2:", "street_address"),
        ("City:", "city"),
        ("State or Province:", "state_province"),
        ("Postal Code:", "postal_code"),
        ("Country:", "country"),
        ("Zip:", "postal_code"),
        ("Town", "city"),
    ],
)
def test_each_address_component_gets_its_own_semantic_type(label, expected):
    """They used to collapse into one `address` type that generated a street
    address for all of them."""
    constraint = infer_constraint(FormField(name=None, field_type="text", label=label))
    assert constraint.field_semantic_type == expected


def test_state_or_province_is_an_address_field_not_a_lifecycle_status():
    """`\\bstate\\b` in the status pattern used to swallow 'State or Province'
    and fill it with 'Active'."""
    constraint = infer_constraint(FormField(name=None, field_type="text", label="State or Province:"))
    assert constraint.field_semantic_type == "state_province"

    # A real status field must still classify as status.
    status = infer_constraint(FormField(name=None, field_type="text", label="Status"))
    assert status.field_semantic_type == "status"


def test_no_address_component_is_filled_with_a_street_address():
    for label in ("City:", "State or Province:", "Postal Code:", "Country:"):
        constraint = infer_constraint(FormField(name=None, field_type="text", label=label))
        value = generate_valid_value(constraint.field_semantic_type, constraint, run_id="r", seed=1).generated_value
        assert "Street" not in value, f"{label} was filled with a street address: {value!r}"


def test_postal_code_is_short_and_numeric():
    """A 37-character run prefix in a postal code is invalid in every
    application; the live backend capped it at 10 characters."""
    constraint = infer_constraint(FormField(name=None, field_type="text", label="Postal Code:"))
    value = generate_valid_value("postal_code", constraint, run_id="run", seed=3).generated_value
    assert value.isdigit()
    assert len(value) <= 10
    assert "GemmaQA_TEST" not in value


def test_the_whole_contact_form_generates_plausible_values():
    generated = {}
    for f in _fields():
        constraint = infer_constraint(f)
        generated[f.label] = generate_valid_value(
            constraint.field_semantic_type, constraint, run_id="run", seed=1
        ).generated_value

    assert "@" in generated["Email:"]
    assert generated["City:"] != generated["Country:"]
    assert generated["Postal Code:"].isdigit()
    assert "Street" in generated["Street Address 1:"]
    assert "Street" not in generated["Country:"]


# ===========================================================================
# B — conservative retry values
# ===========================================================================


def test_conservative_phone_is_digits_only():
    """The live backend rejected '+1-555-0105' with 'Phone number is invalid'."""
    constraint = infer_constraint(FormField(name=None, field_type="text", label="Phone:"))
    value = generate_conservative_value("phone_number", constraint, run_id="r", seed=5).generated_value
    assert value.isdigit()


def test_conservative_values_drop_the_run_prefix_and_stay_short():
    """The live backend capped street/city/country at 40 chars; the prefixed
    value was 52."""
    for label, semantic in (("Street Address 1:", "street_address"), ("Email:", "email")):
        constraint = infer_constraint(FormField(name=None, field_type="text", label=label))
        value = generate_conservative_value(semantic, constraint, run_id="run", seed=2).generated_value
        assert "GemmaQA_TEST" not in value
        assert len(value) <= 40, f"{label} conservative value too long: {value!r}"


def test_conservative_generation_covers_every_semantic_type_without_raising():
    from app.agent.test_data_schemas import FIELD_SEMANTIC_TYPES

    constraint = infer_constraint(FormField(name=None, field_type="text", label="Anything"))
    for semantic in sorted(FIELD_SEMANTIC_TYPES):
        value = generate_conservative_value(semantic, constraint, run_id="r", seed=1)
        assert value.generated_value, f"{semantic} produced no conservative value"


# ===========================================================================
# C — classifying what the application did
# ===========================================================================


def _failed_post(status: int) -> NetworkEntry:
    return NetworkEntry(method="POST", url="https://app.example.com/contacts", status=status, failed=True)


def test_a_4xx_on_the_submit_is_a_validation_rejection():
    outcome = classify_submission(
        before_state=_state(),
        after_state=_state(network=[_failed_post(400)]),
        form_id="form_015",
        fields=_fields(),
    )
    assert outcome.outcome == SUBMISSION_REJECTED_VALIDATION
    assert outcome.http_status == 400
    assert outcome.rejected is True
    # A refusal is not by itself an application defect — the data may be at fault.
    assert outcome.is_application_defect_candidate is False


def test_a_5xx_on_the_submit_is_an_application_failure():
    outcome = classify_submission(
        before_state=_state(),
        after_state=_state(network=[_failed_post(500)]),
        form_id="form_015",
        fields=_fields(),
    )
    assert outcome.outcome == SUBMISSION_REJECTED_SERVER
    assert outcome.is_application_defect_candidate is True


def test_navigating_away_from_the_form_is_acceptance():
    outcome = classify_submission(
        before_state=_state(),
        after_state=_state(url="https://app.example.com/contactList", forms=[]),
        form_id="form_015",
        fields=_fields(),
    )
    assert outcome.outcome == SUBMISSION_ACCEPTED


def test_a_new_validation_message_is_a_rejection_even_without_a_network_signal():
    outcome = classify_submission(
        before_state=_state(),
        after_state=_state(alerts=["Phone number is invalid"]),
        form_id="form_015",
        fields=_fields(),
    )
    assert outcome.outcome == SUBMISSION_REJECTED_VALIDATION


def test_sitting_on_the_same_form_with_no_signal_is_unknown_not_success():
    """The exact hole in the old logic: this used to count as a completed create."""
    outcome = classify_submission(
        before_state=_state(),
        after_state=_state(),
        form_id="form_015",
        fields=_fields(),
    )
    assert outcome.outcome == SUBMISSION_UNKNOWN
    assert outcome.outcome != SUBMISSION_ACCEPTED


def test_a_failed_get_during_submit_is_not_treated_as_a_rejection():
    """Analytics beacons and missing icons must not look like a refused write."""
    noise = NetworkEntry(method="GET", url="https://cdn.example.com/x.png", status=404, failed=True)
    outcome = classify_submission(
        before_state=_state(),
        after_state=_state(url="https://app.example.com/contactList", forms=[], network=[noise]),
        form_id="form_015",
        fields=_fields(),
    )
    assert outcome.outcome == SUBMISSION_ACCEPTED


def test_a_failure_already_present_before_the_submit_is_not_attributed_to_it():
    pre_existing = _failed_post(400)
    outcome = classify_submission(
        before_state=_state(network=[pre_existing]),
        after_state=_state(url="https://app.example.com/contactList", forms=[], network=[pre_existing]),
        form_id="form_015",
        fields=_fields(),
    )
    assert outcome.outcome == SUBMISSION_ACCEPTED


def test_a_second_failure_to_the_same_endpoint_is_still_a_rejection():
    """A retry hits the same URL as the attempt before it. Deduping by URL made
    the second refusal invisible, so a refused retry read as merely unproven."""
    first = _failed_post(400)
    outcome = classify_submission(
        before_state=_state(network=[first]),
        after_state=_state(network=[first, _failed_post(400)]),
        form_id="form_015",
        fields=_fields(),
    )
    assert outcome.outcome == SUBMISSION_REJECTED_VALIDATION
    assert outcome.http_status == 400


def test_a_click_that_never_executed_is_unknown():
    outcome = classify_submission(
        before_state=_state(), after_state=_state(), form_id="form_015",
        fields=_fields(), click_succeeded=False,
    )
    assert outcome.outcome == SUBMISSION_UNKNOWN


# ===========================================================================
# D — field attribution (best effort, never invention)
# ===========================================================================


def test_a_message_naming_a_field_attributes_to_that_field():
    hits = attribute_fields(["Phone number is invalid"], _fields())
    phone_id = next(f.element_id for f in _fields() if f.label == "Phone:")
    assert phone_id in hits


def test_a_generic_message_attributes_to_nothing():
    assert attribute_fields(["Please check your input and try again"], _fields()) == []


def test_short_label_tokens_do_not_match_everything():
    """'of' and 'or' appear in half the labels; matching on them would attribute
    every failure to every field."""
    hits = attribute_fields(["The value of the field or something"], _fields())
    assert len(hits) < len(_fields())


# ===========================================================================
# E — the workflow learns instead of repeating
# ===========================================================================


def _workflow() -> GenericFormWorkflow:
    wf = GenericFormWorkflow.start(_form(), page_url="https://app.example.com/addContact", run_id="run")
    assert wf is not None
    wf.plan_data()
    return wf


def test_a_rejected_submit_is_not_recorded_as_verified():
    wf = _workflow()
    wf.state = "submitted"
    rejection = classify_submission(
        before_state=_state(), after_state=_state(network=[_failed_post(400)]),
        form_id="form_015", fields=wf.fields,
    )
    wf.note_result(success=True, outcome=rejection)
    assert wf.state != "verified", "a 400-refused submit must never count as a completed write"


def test_a_rejected_submit_replans_conservatively_rather_than_retrying_identically():
    wf = _workflow()
    original = dict(wf.planned_values)
    wf.state = "submitted"
    wf.note_result(
        success=True,
        outcome=classify_submission(
            before_state=_state(), after_state=_state(network=[_failed_post(400)]),
            form_id="form_015", fields=wf.fields,
        ),
    )
    assert wf.state == "filling"
    assert wf.planned_values != original, "retried with byte-identical data the app already refused"
    # The values it now intends to type are the conservative ones.
    phone_id = next(f.element_id for f in wf.fields if f.label == "Phone:")
    assert wf.planned_values[phone_id].isdigit()


def test_an_attributed_rejection_replans_only_the_named_field():
    wf = _workflow()
    original = dict(wf.planned_values)
    phone_id = next(f.element_id for f in wf.fields if f.label == "Phone:")
    wf.state = "submitted"
    wf.note_result(
        success=True,
        outcome=classify_submission(
            before_state=_state(),
            after_state=_state(network=[_failed_post(400)], alerts=["Phone number is invalid"]),
            form_id="form_015",
            fields=wf.fields,
        ),
    )
    assert wf.planned_values[phone_id] != original[phone_id]
    city_id = next(f.element_id for f in wf.fields if f.label == "City:")
    assert wf.planned_values[city_id] == original[city_id], "unrelated field was needlessly changed"


def test_an_unattributed_rejection_retries_only_the_risky_looking_values():
    """A blind refill costs one action per field; observed live, an 11-field
    refill consumed the whole remaining budget. Target the values a validator is
    most likely to have objected to instead."""
    wf = _workflow()
    targets = wf._unattributed_retry_targets()
    assert 0 < len(targets) < len(wf.planned_values), "blind full-form refill is too expensive"

    by_element = {f.element_id: f.label for f in wf.fields}
    labels = {by_element[t] for t in targets}
    # The punctuated phone and the prefix-bearing street addresses are exactly
    # what the live backend refused.
    assert "Phone:" in labels
    assert "Street Address 1:" in labels
    # Plain catalogue values are left alone.
    assert "City:" not in labels
    assert "Postal Code:" not in labels


def test_retry_targeting_falls_back_to_everything_when_nothing_looks_risky():
    plain = [
        FormField(name=None, field_type="text", label="City:", element_id="el_002"),
        FormField(name=None, field_type="text", label="Country:", element_id="el_003"),
    ]
    wf = GenericFormWorkflow.start(
        _form(plain), page_url="https://app.example.com/addContact", run_id="run"
    )
    assert wf is not None
    wf.plan_data()
    assert set(wf._unattributed_retry_targets()) == set(wf.planned_values)


def test_repeated_rejection_eventually_gives_up_instead_of_looping():
    wf = _workflow()
    rejection = classify_submission(
        before_state=_state(), after_state=_state(network=[_failed_post(400)]),
        form_id="form_015", fields=wf.fields,
    )
    for _ in range(wf.max_attempts + 2):
        wf.note_result(success=True, outcome=rejection)
        if wf.state == "failed":
            break
    assert wf.state == "failed"
    assert wf.attempts <= wf.max_attempts + 1


def test_an_accepted_submit_is_verified():
    wf = _workflow()
    wf.state = "submitted"
    wf.note_result(
        success=True,
        outcome=classify_submission(
            before_state=_state(),
            after_state=_state(url="https://app.example.com/contactList", forms=[]),
            form_id="form_015",
            fields=wf.fields,
        ),
    )
    assert wf.state == "verified"


def test_an_unproven_submit_is_marked_unverified_rather_than_verified():
    wf = _workflow()
    wf.state = "submitted"
    wf.note_result(
        success=True,
        outcome=classify_submission(
            before_state=_state(), after_state=_state(), form_id="form_015", fields=wf.fields
        ),
    )
    assert wf.state == "submitted_unverified"


def test_rejections_are_kept_as_evidence():
    wf = _workflow()
    wf.note_result(
        success=True,
        outcome=classify_submission(
            before_state=_state(), after_state=_state(network=[_failed_post(422)]),
            form_id="form_015", fields=wf.fields,
        ),
    )
    assert wf.rejection_history
    assert wf.rejection_history[0]["http_status"] == 422


def test_callers_without_an_outcome_keep_the_old_behaviour():
    """Backward compatibility: `note_result(success=...)` alone still works."""
    wf = _workflow()
    wf.note_result(success=True)
    assert wf.state == "verified"


# ===========================================================================
# F — the test-data marker must not corrupt the value it marks
# ===========================================================================


def _validator():
    from app.safety.policies import SafetyPolicy
    from app.safety.validator import ActionValidator

    return ActionValidator(
        SafetyPolicy(
            authorized_url="https://app.example.com",
            authorized_domain="app.example.com",
            safe_mode=False,
            allow_controlled_writes=True,
        )
    )


def _fill(value: str, metadata: dict | None = None):
    from app.schemas import ActionType, BrowserAction, RiskLevel

    return BrowserAction(
        action=ActionType.FILL,
        element_id="el_002",
        value=value,
        reason="Fill test data",
        risk=RiskLevel.LOW,
        metadata=metadata or {},
    )


def test_plain_text_still_gets_the_test_data_marker():
    """The safety guarantee is unchanged for ordinary free text."""
    from app.safety.policies import TEST_DATA_PREFIX

    result = _validator().validate(_fill("Acme Corp"))
    assert result.allowed
    assert result.sanitized_action.value.startswith(TEST_DATA_PREFIX)


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-15",       # ISO date — prefixing made it an invalid date
        "01/15/2026",       # locale date
        "14:30",            # time
        "+1-555-0105",      # phone — prefixing also blew a 15-char cap
        "5550013",          # digits-only phone
        "62704",            # postal code
        "42",               # number
        "19.99",            # decimal
        "qa12@example.com",  # email
        "https://example.com",  # url
    ],
)
def test_format_constrained_values_are_never_prefixed(value):
    """Observed live: `GemmaQA_TEST_2026-01-15` and `GemmaQA_TEST_+1-555-0105`
    were rejected by the application, so every create failed regardless of how
    good the generated data was."""
    from app.safety.policies import TEST_DATA_PREFIX

    result = _validator().validate(_fill(value))
    assert result.allowed
    assert result.sanitized_action.value == value
    assert TEST_DATA_PREFIX not in result.sanitized_action.value


def test_an_already_marked_value_is_not_marked_twice():
    """Observed live as `GemmaQA_TEST_106 GemmaQA_TEST_..._Test Street`, which
    blew the field's length cap on the retry as well as the first attempt."""
    from app.safety.policies import TEST_DATA_PREFIX

    value = f"106 {TEST_DATA_PREFIX}abc_Test Street"
    result = _validator().validate(_fill(value))
    assert result.sanitized_action.value == value
    assert result.sanitized_action.value.count(TEST_DATA_PREFIX) == 1


def test_generator_produced_values_are_left_alone():
    """The generator already applies the marker wherever the field type allows
    one; re-applying it at execution time is duplicate and destructive."""
    from app.safety.policies import TEST_DATA_PREFIX

    result = _validator().validate(_fill("Springfield", {"generated_test_data": True}))
    assert result.sanitized_action.value == "Springfield"
    assert TEST_DATA_PREFIX not in result.sanitized_action.value


def test_form_workflow_fills_carry_the_generated_marker_flag():
    """Without this flag the validator would re-prefix every workflow fill."""
    wf = _workflow()
    action = wf.next_action()
    while action is not None and action.action.value != "fill":
        action = wf.next_action()
    assert action is not None
    assert (action.metadata or {}).get("generated_test_data") is True


# ===========================================================================
# G — the run reports refused writes honestly
# ===========================================================================


def test_memory_reports_accepted_and_refused_writes_separately():
    from app.agent.memory import RunMemory

    memory = RunMemory(run_id="r", start_url="https://app.example.com")
    memory.record_submission_outcome(
        "form_015",
        classify_submission(
            before_state=_state(), after_state=_state(network=[_failed_post(400)]),
            form_id="form_015", fields=_fields(),
        ),
    )
    memory.record_submission_outcome(
        "form_015",
        classify_submission(
            before_state=_state(),
            after_state=_state(url="https://app.example.com/contactList", forms=[]),
            form_id="form_015",
            fields=_fields(),
        ),
    )

    summary = memory.submission_outcome_summary()
    assert summary["total_submissions"] == 2
    assert summary["accepted"] == 1
    assert summary["refused"] == 1
    assert summary["refusals"][0]["http_status"] == 400


def test_empty_summary_when_no_writes_were_attempted():
    from app.agent.memory import RunMemory

    memory = RunMemory(run_id="r", start_url="https://app.example.com")
    assert memory.submission_outcome_summary() == {}


# ===========================================================================
# H — a 2xx on a mutating request IS acceptance
#
# Acceptance used to be inferred only from the form disappearing or the URL
# changing — both UI side-effects, and both absent on an application that
# re-renders the same form in place after saving. So a create that genuinely
# succeeded came back `unknown`, the record was never registered, and the run
# moved on to fill another form instead of using the one it had just made.
# 4 of 14 live runs never registered a record because of this.
#
# A 2xx answer from the application is stronger evidence than anything the DOM
# can offer: it is the application itself saying yes.
# ===========================================================================


def _net(method, status, url="https://app.example.com/api/contacts", failed=False):
    from app.schemas import NetworkEntry

    return NetworkEntry(method=method, url=url, status=status, failed=failed)


def _net_state(entries, *, url="https://app.example.com/addContact", forms=None):
    """A PageState carrying network evidence. Named apart from this file's other
    helpers — an earlier draft shadowed `_form` and broke five passing tests."""
    from app.schemas import FormDescriptor, FormField, PageState

    default_form = FormDescriptor(
        form_id="form_1", submit_element_id="el_9",
        fields=[FormField(name="first", field_type="text", label="First Name", element_id="el_1")],
    )
    return PageState(
        page_id="p", url=url, title="t", state_fingerprint="fp",
        forms=[default_form] if forms is None else forms,
        network_entries=list(entries),
    )


def test_a_successful_post_is_acceptance_even_while_still_on_the_form():
    """THE bug: the app saved the record and re-rendered the same form, so every
    DOM signal said "nothing happened"."""
    outcome = classify_submission(
        before_state=_net_state([]),
        after_state=_net_state([_net("POST", 201)]),
        form_id="form_1",
    )
    assert outcome.outcome == SUBMISSION_ACCEPTED
    assert outcome.http_status == 201
    assert "mutating_request_succeeded_201" in outcome.signals


def test_a_failure_still_outranks_a_success():
    """If anything failed, that matters more — a 200 on some unrelated PUT must
    never mask a 400 on the create."""
    outcome = classify_submission(
        before_state=_net_state([]),
        after_state=_net_state([_net("POST", 200), _net("POST", 400, url="https://app.example.com/api/x")]),
        form_id="form_1",
    )
    assert outcome.outcome == SUBMISSION_REJECTED_VALIDATION
    assert outcome.http_status == 400


def test_a_retrys_success_is_not_masked_by_the_first_attempts_success():
    """Counted per (status, url) exactly as failures are: a retry hits the same
    endpoint, so "this URL already succeeded" would hide the success belonging to
    THIS submit."""
    before = _net_state([_net("POST", 201)])
    after = _net_state([_net("POST", 201), _net("POST", 201)])

    assert classify_submission(before_state=before, after_state=after, form_id="form_1").outcome == (
        SUBMISSION_ACCEPTED
    )


def test_an_unchanged_successful_request_is_not_new_acceptance():
    """A 2xx already present before the submit says nothing about this submit."""
    state = _net_state([_net("POST", 201)])
    outcome = classify_submission(before_state=state, after_state=state, form_id="form_1")

    assert outcome.outcome == SUBMISSION_UNKNOWN


def test_a_successful_read_is_not_acceptance():
    """GET is not a state change; a page merely loading data proves nothing."""
    outcome = classify_submission(
        before_state=_net_state([]),
        after_state=_net_state([_net("GET", 200)]),
        form_id="form_1",
    )
    assert outcome.outcome == SUBMISSION_UNKNOWN


def test_a_request_marked_failed_is_not_acceptance_whatever_its_status():
    outcome = classify_submission(
        before_state=_net_state([]),
        after_state=_net_state([_net("POST", 200, failed=True)]),
        form_id="form_1",
    )
    assert outcome.outcome != SUBMISSION_ACCEPTED


def test_the_dom_route_to_acceptance_still_works():
    """Applications that DO navigate away must keep being understood — this adds
    evidence, it does not replace what worked."""
    outcome = classify_submission(
        before_state=_net_state([]),
        after_state=_net_state([], url="https://app.example.com/contactList", forms=[]),
        form_id="form_1",
    )
    assert outcome.outcome == SUBMISSION_ACCEPTED
    assert "navigated_away_from_form" in outcome.signals
