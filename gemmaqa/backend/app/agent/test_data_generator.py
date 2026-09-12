"""Valid and negative test-data generation — the concrete producer of
`TestDataValue` records for every `FIELD_SEMANTIC_TYPES` entry, satisfying
`field_constraint_inference`'s inferred constraints.

Identifiable-prefix scheme: extends (never replaces) the existing
`app.safety.policies.TEST_DATA_PREFIX` ("GemmaQA_TEST_") with a run fragment
and a timestamp fragment, e.g. `GemmaQA_TEST_3f9a1c2b_20260730142233_...` —
`ActionValidator`'s existing `value.startswith(TEST_DATA_PREFIX)` check
(app/safety/validator.py) keeps working unmodified against every value this
module produces; nothing here invents a second, incompatible test-data
marker.

Human names are the ONE deliberate exception: a `GemmaQA_TEST_...`-prefixed
"first name" would itself look like a data-quality bug in a demo recording
or screenshot. Name fields get a safe, ordinary-looking synthetic name from
a small fixed pool instead — the run/record linkage lives in the
`TestDataValue.run_id`/`temporary_record_id` fields, never smuggled into the
visible string. This is a deliberate trade-off, documented in
docs/TEST_DATA_LIFECYCLE.md.

Determinism: every generator accepts an optional `seed` — the SAME
(run_id, field_semantic_type, seed) always produces the SAME value, so tests
can assert on exact output without re-deriving randomness. Without a `seed`,
generation is still deterministic PER PROCESS (an incrementing counter), not
mtime/random-based, per this codebase's existing "no Math.random()/no
datetime.now() inside anything that must replay identically" discipline —
callers that need real uniqueness across runs pass the real `run_id`.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from app.agent.file_upload_fixtures import default_safe_upload_fixture
from app.agent.test_data_schemas import FieldConstraint, TestDataValue
from app.safety.policies import TEST_DATA_PREFIX

# Safe, ordinary-looking synthetic names — never a real person, never drawn
# from a real customer/employee list. Deliberately generic across any
# application (no target-app vocabulary).
_FIRST_NAMES = ("Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Sam", "Drew")
_MIDDLE_NAMES = ("Lee", "Ray", "Jae", "Kai", "Rowan", "Sage")
_LAST_NAMES = ("Rivera", "Chen", "Okafor", "Novak", "Singh", "Park", "Dubois", "Silva")
_ROLE_WORDS = ("Reviewer", "Coordinator", "Analyst", "Specialist")
_STATUS_WORDS = ("Active", "Pending", "Draft")
# Address-component catalogues. Short, ASCII, and unremarkable on purpose: an
# address component is validated for SHAPE by most backends (length caps,
# character classes), so the safest test value is an ordinary-looking one.
_CITY_NAMES = ("Springfield", "Riverton", "Lakeside", "Fairview", "Brookfield", "Ashford")
_STATE_PROVINCE_NAMES = ("Ontario", "Bavaria", "Queensland", "Gelderland", "Leinster", "Uppsala")
_COUNTRY_NAMES = ("Canada", "Germany", "Australia", "Netherlands", "Ireland", "Sweden")

_counter = {"n": 0}


def _next_counter() -> int:
    _counter["n"] += 1
    return _counter["n"]


def run_fragment(run_id: str) -> str:
    """A short, stable, non-reversible fragment identifying the run — never
    the raw run_id itself (keeps generated values short enough for typical
    max_length constraints)."""
    return hashlib.sha1((run_id or "local").encode("utf-8")).hexdigest()[:8]


def timestamp_fragment(*, at: datetime | None = None) -> str:
    """`at` must be supplied by the caller for deterministic tests — this
    module never calls `datetime.now()` itself (breaks replay, per this
    codebase's standing rule)."""
    at = at or datetime(2026, 1, 1)
    return at.strftime("%Y%m%d%H%M%S")


def build_prefix(run_id: str, *, at: datetime | None = None) -> str:
    """`TEST_DATA_PREFIX` plus the run fragment — 22 characters, and
    deliberately no timestamp.

    The prefix used to carry `timestamp_fragment()` as well, at 36 characters
    total. That is longer than many real text fields allow, so every prefixed
    human-text value was born too long: a live run had its contact submission
    refused with `street1 (...) is longer than the maximum allowed length
    (40)` and never created a record at all. The timestamp bought nothing —
    `run_fragment` already identifies the run uniquely — so it cost 15
    characters to make correct data invalid.

    `at` is still accepted so existing callers keep working; it no longer
    affects the prefix.
    """
    return f"{TEST_DATA_PREFIX}{run_fragment(run_id)}_"


def _pick(pool: tuple[str, ...], seed: int) -> str:
    return pool[seed % len(pool)]


def _clip(value: str, constraint: FieldConstraint) -> str:
    if constraint.max_length and len(value) > constraint.max_length:
        value = value[: constraint.max_length]
    return value


def _pad_to_min(value: str, constraint: FieldConstraint, filler: str = "x") -> str:
    if constraint.min_length and len(value) < constraint.min_length:
        value = value + filler * (constraint.min_length - len(value))
    return value


# ---------------------------------------------------------------------------
# Valid generation
# ---------------------------------------------------------------------------


def generate_valid_value(
    semantic_type: str,
    constraint: FieldConstraint,
    *,
    run_id: str,
    seed: int | None = None,
    at: datetime | None = None,
) -> TestDataValue:
    seed = seed if seed is not None else _next_counter()
    prefix = build_prefix(run_id, at=at)
    sensitive = "none"
    uniqueness = "run_scoped_unique"
    dependencies: list[str] = []

    if semantic_type == "file_upload":
        # Repository-controlled safe fixture ONLY — never an arbitrary or
        # generated file. See app.agent.file_upload_fixtures.
        fixture_path = default_safe_upload_fixture()
        return TestDataValue(
            field_semantic_type=semantic_type,
            generated_value=str(fixture_path),
            data_category="valid_positive",
            validity_intent="valid",
            uniqueness_strategy="none",
            generation_source="fixture_file",
            constraints=constraint,
            sensitive_classification="none",
            run_id=run_id,
            cleanup_status="not_applicable",
        )

    if semantic_type == "first_name":
        value = _pick(_FIRST_NAMES, seed)
        sensitive = "synthetic_pii_like"
        uniqueness = "none"
    elif semantic_type == "middle_name":
        value = _pick(_MIDDLE_NAMES, seed)
        sensitive = "synthetic_pii_like"
        uniqueness = "none"
    elif semantic_type == "last_name":
        value = _pick(_LAST_NAMES, seed)
        sensitive = "synthetic_pii_like"
        uniqueness = "none"
    elif semantic_type == "username":
        # No fixed truncation here: the shared `_clip` below only shortens
        # this when the field's OWN observed max_length requires it, so the
        # seed-bearing suffix (the actual uniqueness guarantee) survives
        # whenever no max_length constraint was observed.
        value = f"{prefix}user{seed}".lower().replace("_", "")
    elif semantic_type == "email":
        value = f"{prefix}{seed}@example.com".lower()
    elif semantic_type == "phone_number":
        # A fictional, obviously-non-dialable NANP number range (555 is
        # reserved for fiction) — never a real subscriber number.
        value = f"+1-555-01{seed % 100:02d}"
        sensitive = "synthetic_pii_like"
        uniqueness = "none"
    elif semantic_type == "employee_identifier":
        value = f"{prefix}EMP{seed}"
    elif semantic_type == "description":
        value = f"{prefix}description for automated verification (seed {seed})"
    elif semantic_type == "date":
        value = (constraint.min_date or "2026-01-15")
    elif semantic_type == "date_range":
        value = "2026-01-15..2026-01-20"
        dependencies = ["date_range_start", "date_range_end"]
    elif semantic_type == "status":
        value = constraint.options[0] if constraint.options else _pick(_STATUS_WORDS, seed)
        uniqueness = "none"
    elif semantic_type == "role":
        value = constraint.options[0] if constraint.options else _pick(_ROLE_WORDS, seed)
        uniqueness = "none"
    elif semantic_type in {"dropdown_option", "autocomplete_selection"}:
        value = constraint.options[0] if constraint.options else f"{prefix}option"
        uniqueness = "none"
    elif semantic_type == "boolean":
        value = "true"
        uniqueness = "none"
    elif semantic_type == "number":
        lo = constraint.min_value if constraint.min_value is not None else 1
        hi = constraint.max_value if constraint.max_value is not None else lo + 100
        mid = lo + (hi - lo) / 2
        value = str(int(mid))
        uniqueness = "none"
    elif semantic_type == "decimal":
        lo = constraint.min_value if constraint.min_value is not None else 0.0
        hi = constraint.max_value if constraint.max_value is not None else lo + 100.0
        value = f"{(lo + (hi - lo) / 2):.2f}"
        uniqueness = "none"
    elif semantic_type == "url":
        value = f"https://example.com/{prefix.lower()}{seed}"
    elif semantic_type in {"address", "street_address"}:
        # "address" is the pre-split legacy name, kept so any caller still
        # passing it keeps working.
        value = f"{100 + seed} {prefix}Test Street"
    elif semantic_type == "city":
        value = _pick(_CITY_NAMES, seed)
        uniqueness = "none"
    elif semantic_type == "state_province":
        value = _pick(_STATE_PROVINCE_NAMES, seed)
        uniqueness = "none"
    elif semantic_type == "postal_code":
        # Digits only and short: postal codes are validated for shape almost
        # everywhere, and no real one carries a run-scoped test prefix.
        value = f"{10000 + (seed % 90000)}"
        uniqueness = "none"
    elif semantic_type == "country":
        value = _pick(_COUNTRY_NAMES, seed)
        uniqueness = "none"
    else:  # free_text / unknown
        value = f"{prefix}{seed}"

    value = _pad_to_min(_clip(value, constraint), constraint)

    return TestDataValue(
        field_semantic_type=semantic_type,
        generated_value=value,
        data_category="valid_positive",
        validity_intent="valid",
        uniqueness_strategy=uniqueness,
        generation_source="constraint_derived" if constraint.evidence else "deterministic_catalog",
        constraints=constraint,
        related_field_dependencies=dependencies,
        sensitive_classification=sensitive,
        run_id=run_id,
        cleanup_status="not_applicable",
    )


def generate_conservative_value(
    semantic_type: str,
    constraint: FieldConstraint,
    *,
    run_id: str,
    seed: int | None = None,
) -> TestDataValue:
    """The shortest, plainest valid-looking value for this field.

    Used after an application REFUSES a submission (see
    `app.agent.submission_outcome`). Browsers do not expose response bodies, so
    GemmaQA frequently learns "that was rejected" without learning "because of
    field X, rule Y". Retrying the identical data would be pointless; guessing a
    specific rule would be invention. Retrying with the most conservative
    representation is the honest middle: digits-only phone numbers, short ASCII
    text, no run-scoped prefix, nothing near a length boundary.

    The trade-off is deliberate: these values lose the `GemmaQA_TEST_` prefix
    that makes test records identifiable, so they are only used on retry, never
    on a first attempt.
    """
    seed = seed if seed is not None else _next_counter()

    if semantic_type == "phone_number":
        # Digits only. Many backends validate phone with a numeric-only rule and
        # reject the punctuated international form that reads as valid to a human.
        value = f"555{seed % 10000:04d}"
    elif semantic_type == "email":
        value = f"qa{seed % 10000}@example.com"
    elif semantic_type in {"address", "street_address"}:
        value = f"{100 + (seed % 800)} Test St"
    elif semantic_type == "postal_code":
        value = f"{10000 + (seed % 90000)}"
    elif semantic_type == "city":
        value = _pick(_CITY_NAMES, seed)
    elif semantic_type == "state_province":
        value = _pick(_STATE_PROVINCE_NAMES, seed)
    elif semantic_type == "country":
        value = _pick(_COUNTRY_NAMES, seed)
    elif semantic_type == "first_name":
        value = _pick(_FIRST_NAMES, seed)
    elif semantic_type == "middle_name":
        value = _pick(_MIDDLE_NAMES, seed)
    elif semantic_type == "last_name":
        value = _pick(_LAST_NAMES, seed)
    elif semantic_type == "username":
        value = f"qauser{seed % 10000}"
    elif semantic_type == "url":
        value = "https://example.com"
    elif semantic_type == "date":
        value = constraint.min_date or "1990-01-15"
    elif semantic_type in {"status", "role", "dropdown_option", "autocomplete_selection"}:
        value = constraint.options[0] if constraint.options else "Active"
    elif semantic_type == "boolean":
        value = "true"
    elif semantic_type in {"number", "decimal"}:
        lo = constraint.min_value if constraint.min_value is not None else 1
        value = str(int(lo))
    elif semantic_type == "description":
        value = "QA test note"
    else:
        value = f"QA{seed % 10000}"

    value = _pad_to_min(_clip(value, constraint), constraint)

    return TestDataValue(
        field_semantic_type=semantic_type,
        generated_value=value,
        data_category="valid_positive",
        validity_intent="valid",
        uniqueness_strategy="none",
        generation_source="deterministic_catalog",
        constraints=constraint,
        sensitive_classification="none",
        run_id=run_id,
        cleanup_status="not_applicable",
    )


def generate_boundary_values(
    semantic_type: str, constraint: FieldConstraint, *, run_id: str, at: datetime | None = None
) -> list[TestDataValue]:
    """`valid_boundary_min`/`valid_boundary_max` — only produced where a
    real min/max constraint was actually observed (never invented)."""
    out: list[TestDataValue] = []
    prefix = build_prefix(run_id, at=at)
    if semantic_type in {"number", "decimal"} and constraint.min_value is not None:
        out.append(
            TestDataValue(
                field_semantic_type=semantic_type, generated_value=str(constraint.min_value),
                data_category="valid_boundary_min", validity_intent="valid", uniqueness_strategy="none",
                generation_source="constraint_derived", constraints=constraint, run_id=run_id,
            )
        )
    if semantic_type in {"number", "decimal"} and constraint.max_value is not None:
        out.append(
            TestDataValue(
                field_semantic_type=semantic_type, generated_value=str(constraint.max_value),
                data_category="valid_boundary_max", validity_intent="valid", uniqueness_strategy="none",
                generation_source="constraint_derived", constraints=constraint, run_id=run_id,
            )
        )
    if semantic_type in {"free_text", "description", "username"} and constraint.max_length:
        out.append(
            TestDataValue(
                field_semantic_type=semantic_type,
                generated_value=_pad_to_min(prefix, constraint)[: constraint.max_length],
                data_category="valid_boundary_max", validity_intent="valid", uniqueness_strategy="none",
                generation_source="constraint_derived", constraints=constraint, run_id=run_id,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Negative generation
# ---------------------------------------------------------------------------


def generate_negative_values(
    semantic_type: str, constraint: FieldConstraint, *, run_id: str, at: datetime | None = None
) -> list[TestDataValue]:
    """Every negative case is SAFE: no script injection, no shell/SQL
    payloads, no destructive vocabulary — only shape-invalid data
    (empty/too-short/too-long/wrong-format/bad-range/duplicate/unsupported-
    but-harmless characters). A negative test's job is to confirm the
    application's OWN validation rejects it, never to attempt an exploit."""
    out: list[TestDataValue] = []
    prefix = build_prefix(run_id, at=at)

    def add(category: str, value: str) -> None:
        out.append(
            TestDataValue(
                field_semantic_type=semantic_type, generated_value=value, data_category=category,
                validity_intent="negative", uniqueness_strategy="none", generation_source="constraint_derived",
                constraints=constraint, run_id=run_id,
            )
        )

    if constraint.required:
        add("invalid_empty", "")

    if constraint.min_length:
        add("invalid_below_min", "x" * max(0, constraint.min_length - 1))
    if constraint.max_length:
        add("invalid_above_max", (prefix + "x" * (constraint.max_length + 20))[: constraint.max_length + 10])

    if semantic_type == "email":
        add("invalid_format", f"{prefix}not-an-email")
    elif semantic_type == "phone_number":
        add("invalid_format", "not-a-phone-###")
    elif semantic_type == "url":
        add("invalid_format", "not a url")
    elif semantic_type in {"number", "decimal"}:
        add("invalid_format", "12.34.56")
        if constraint.min_value is not None:
            add("invalid_below_min", str(constraint.min_value - 1))
        if constraint.max_value is not None:
            add("invalid_above_max", str(constraint.max_value + 1))
    elif semantic_type == "date":
        add("invalid_format", "2026-13-40")
    elif semantic_type == "date_range":
        add("invalid_date_range", "2026-01-20..2026-01-15")  # end before start

    # Unsupported-but-harmless characters — punctuation/unicode noise, never
    # an injection payload (no quotes-as-SQL-breakout, no <script>, no shell
    # metacharacters used destructively).
    add("invalid_unsupported_characters", f"{prefix}<>{{}}&%~")

    if constraint.pattern:
        add("invalid_format", f"{prefix}!!!does-not-match-pattern!!!")

    return out


def generate_duplicate_probe(
    semantic_type: str, existing_value: str, *, run_id: str
) -> TestDataValue:
    """A deliberate re-submission of an ALREADY-USED value, to confirm the
    application enforces its own uniqueness constraint — the value itself is
    still test-data-tagged (via whatever prefix `existing_value` already
    carries), never a real record's value."""
    return TestDataValue(
        field_semantic_type=semantic_type, generated_value=existing_value, data_category="invalid_duplicate",
        validity_intent="negative", uniqueness_strategy="fixed_duplicate_probe", generation_source="dependency_derived",
        constraints=FieldConstraint(field_semantic_type=semantic_type), run_id=run_id,
    )
