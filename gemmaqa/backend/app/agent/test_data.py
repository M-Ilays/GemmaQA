"""Deterministic safe test-data generation for exploratory form tests."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from app.utils.ids import new_id


@dataclass(frozen=True)
class TestValue:
    category: str
    value: str
    description: str


def run_tag() -> str:
    return f"QA_TEST_2026_{uuid4().hex[:8].upper()}"


def identifiable_name(prefix: str = "User") -> str:
    return f"QA_TEST_2026_{prefix}_{new_id()[:8]}"


def test_email(valid: bool = True) -> TestValue:
    tag = run_tag()
    if valid:
        return TestValue("valid_email", f"{tag.lower()}@example.com", "Valid test email")
    return TestValue("invalid_email", f"{tag.lower()}@@not-an-email", "Invalid email format")


def test_phone() -> TestValue:
    return TestValue("safe_phone", "+1-555-0100", "Safe fictional phone")


def test_date(valid: bool = True) -> TestValue:
    if valid:
        return TestValue("valid_date", "2026-07-24", "Valid ISO date")
    return TestValue("invalid_date", "2026-13-40", "Invalid calendar date")


def test_number(*, kind: str = "mid") -> TestValue:
    mapping = {
        "min": ("0", "Minimum boundary"),
        "max": ("999999", "Maximum-ish boundary"),
        "mid": ("42", "Safe mid number"),
        "invalid": ("12.34.56", "Invalid number format"),
        "negative": ("-1", "Negative boundary"),
    }
    value, desc = mapping.get(kind, mapping["mid"])
    return TestValue(f"number_{kind}", value, desc)


def long_text(length: int = 500) -> TestValue:
    body = ("QA_TEST_LONG_" + ("x" * max(1, length - 20)))[:length]
    return TestValue("long_text", body, f"Long string ({length} chars)")


def unicode_text() -> TestValue:
    return TestValue("unicode", "QA_TEST_你好_مرحبا_🙂", "Unicode / emoji input")


def special_chars() -> TestValue:
    # Intentionally NOT injection payloads — safe punctuation only
    return TestValue("special_chars", "QA_TEST_!@#_()-[]{}", "Special characters (non-destructive)")


def empty_value() -> TestValue:
    return TestValue("empty", "", "Empty value")


def duplicate_name() -> TestValue:
    # Stable within a process for duplicate-submit checks
    return TestValue("duplicate_name", "QA_TEST_2026_DUPLICATE_SAMPLE", "Duplicate submission name")


SAFE_TEST_CATALOG: list[TestValue] = [
    test_email(True),
    test_email(False),
    test_phone(),
    test_date(True),
    test_date(False),
    test_number(kind="min"),
    test_number(kind="max"),
    test_number(kind="invalid"),
    empty_value(),
    long_text(256),
    unicode_text(),
    special_chars(),
]


def values_for_field(field_type: str | None, label: str | None = None) -> list[TestValue]:
    """Suggest safe values for a field type — never personal data."""
    t = (field_type or "text").lower()
    blob = f"{t} {(label or '')}".lower()
    out: list[TestValue] = []
    if "email" in blob:
        out.extend([test_email(True), test_email(False), empty_value()])
    elif "phone" in blob or "tel" in t:
        out.extend([test_phone(), empty_value()])
    elif "date" in blob:
        out.extend([test_date(True), test_date(False), empty_value()])
    elif t in {"number", "range"} or "amount" in blob or "qty" in blob:
        out.extend(
            [
                test_number(kind="min"),
                test_number(kind="max"),
                test_number(kind="invalid"),
                empty_value(),
            ]
        )
    else:
        out.extend(
            [
                TestValue("identifiable_name", identifiable_name(), "Identifiable QA name"),
                empty_value(),
                long_text(200),
                special_chars(),
                unicode_text(),
            ]
        )
    return out
