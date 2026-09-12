"""Test-data model — application-neutral, closed-vocabulary schemas for
every generated value and every temporary record GemmaQA creates.

Design rules (same discipline as the intelligence packages this session
already built):

- Never a real name, real email, real phone number, or any other real PII.
  Every generated value is either clearly tagged as synthetic test data (via
  `app.safety.policies.TEST_DATA_PREFIX` plus a run/timestamp fragment) or,
  for human-name-shaped fields where an unnaturally tagged visible name would
  itself look like a data-quality bug, a safe synthetic name drawn from a
  small fixed pool — the run/record linkage lives in `TestDataValue`'s own
  `run_id`/`temporary_record_id` fields, never smuggled into the visible
  string, unless a caller explicitly asks for the linkage to be visible.
- Every field here is generic UI/data vocabulary (a name, an email, a
  status), never one target application's business vocabulary.
- No field defaults to a random id via a bare call at import time — ids are
  supplied by the caller (the run/record that owns them), same discipline as
  `app.intelligence.scenario_planning.schemas`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

FIELD_SEMANTIC_TYPES = frozenset(
    {
        "first_name",
        "middle_name",
        "last_name",
        "username",
        "email",
        "phone_number",
        "employee_identifier",
        "free_text",
        "description",
        "date",
        "date_range",
        "status",
        "role",
        "dropdown_option",
        "autocomplete_selection",
        "boolean",
        "number",
        "decimal",
        "url",
        # Address family. `address` is the pre-split legacy name, retained so
        # older callers and persisted records stay valid; new inference emits
        # the specific part instead, because a street address typed into a
        # Postal Code or Country field is rejected by any validating backend.
        "address",
        "street_address",
        "city",
        "state_province",
        "postal_code",
        "country",
        "file_upload",
        "unknown",
    }
)

DATA_CATEGORIES = frozenset(
    {
        "valid_positive",
        "valid_boundary_min",
        "valid_boundary_max",
        "invalid_empty",
        "invalid_below_min",
        "invalid_above_max",
        "invalid_format",
        "invalid_date_range",
        "invalid_duplicate",
        "invalid_unsupported_characters",
        "invalid_boundary",
    }
)

VALIDITY_INTENTS = frozenset({"valid", "negative"})

UNIQUENESS_STRATEGIES = frozenset(
    {
        "none",
        "run_scoped_unique",
        "globally_unique",
        "fixed_duplicate_probe",
    }
)

GENERATION_SOURCES = frozenset(
    {
        "deterministic_catalog",
        "constraint_derived",
        "dependency_derived",
        "fixture_file",
    }
)

# Every generated value is either explicitly test-data-tagged or a safe
# synthetic name — REAL personal data is never a valid classification here.
SENSITIVE_DATA_CLASSIFICATIONS = frozenset({"none", "synthetic_pii_like"})

CLEANUP_STATUSES = frozenset(
    {
        "not_applicable",
        "pending",
        "requested",
        "in_progress",
        "succeeded",
        "failed",
        "manual_action_required",
    }
)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


# ---------------------------------------------------------------------------
# Field constraints
# ---------------------------------------------------------------------------


class FieldConstraint(BaseModel):
    """Everything inferred about ONE field's valid-value shape, with the
    evidence that produced each piece — never a bare guess."""

    field_semantic_type: str = "unknown"
    required: bool = False
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    input_type: str = "text"
    options: list[str] = Field(default_factory=list)
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    min_date: Optional[str] = None
    max_date: Optional[str] = None
    step: Optional[float] = None
    # Other field keys (stable_field_key/element_id) this field's valid
    # values structurally depend on (e.g. an "end date" depending on a
    # "start date", or a "state" dropdown depending on a "country").
    depends_on_field_keys: list[str] = Field(default_factory=list)
    confidence: float = 0.3
    evidence: list[str] = Field(default_factory=list)

    @field_validator("field_semantic_type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value not in FIELD_SEMANTIC_TYPES:
            raise ValueError(f"Invalid field_semantic_type: {value!r} (expected one of {sorted(FIELD_SEMANTIC_TYPES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)


# ---------------------------------------------------------------------------
# Test data value
# ---------------------------------------------------------------------------


class TestDataValue(BaseModel):
    """One generated value for one field, fully traceable to the run/record
    that owns it and the constraint it was generated to satisfy."""

    field_semantic_type: str = "unknown"
    generated_value: str = ""
    data_category: str
    validity_intent: str
    uniqueness_strategy: str = "none"
    generation_source: str = "deterministic_catalog"
    constraints: FieldConstraint = Field(default_factory=FieldConstraint)
    related_field_dependencies: list[str] = Field(default_factory=list)
    sensitive_classification: str = "none"
    temporary_record_id: Optional[str] = None
    run_id: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    cleanup_status: str = "not_applicable"

    @field_validator("field_semantic_type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value not in FIELD_SEMANTIC_TYPES:
            raise ValueError(f"Invalid field_semantic_type: {value!r} (expected one of {sorted(FIELD_SEMANTIC_TYPES)})")
        return value

    @field_validator("data_category")
    @classmethod
    def _validate_category(cls, value: str) -> str:
        if value not in DATA_CATEGORIES:
            raise ValueError(f"Invalid data_category: {value!r} (expected one of {sorted(DATA_CATEGORIES)})")
        return value

    @field_validator("validity_intent")
    @classmethod
    def _validate_intent(cls, value: str) -> str:
        if value not in VALIDITY_INTENTS:
            raise ValueError(f"Invalid validity_intent: {value!r} (expected one of {sorted(VALIDITY_INTENTS)})")
        return value

    @field_validator("uniqueness_strategy")
    @classmethod
    def _validate_uniqueness(cls, value: str) -> str:
        if value not in UNIQUENESS_STRATEGIES:
            raise ValueError(f"Invalid uniqueness_strategy: {value!r} (expected one of {sorted(UNIQUENESS_STRATEGIES)})")
        return value

    @field_validator("generation_source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        if value not in GENERATION_SOURCES:
            raise ValueError(f"Invalid generation_source: {value!r} (expected one of {sorted(GENERATION_SOURCES)})")
        return value

    @field_validator("sensitive_classification")
    @classmethod
    def _validate_sensitive(cls, value: str) -> str:
        if value not in SENSITIVE_DATA_CLASSIFICATIONS:
            raise ValueError(
                f"Invalid sensitive_classification: {value!r} (expected one of {sorted(SENSITIVE_DATA_CLASSIFICATIONS)})"
            )
        return value

    @field_validator("cleanup_status")
    @classmethod
    def _validate_cleanup_status(cls, value: str) -> str:
        if value not in CLEANUP_STATUSES:
            raise ValueError(f"Invalid cleanup_status: {value!r} (expected one of {sorted(CLEANUP_STATUSES)})")
        return value
