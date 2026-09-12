"""Field-constraint and semantic-type inference — application-neutral,
structural-signal-first, exactly the discipline this session's perception
work already established (never a business word, never a guess presented as
certain).

Works over ANY field-shaped object via `getattr` (a legacy `FormField`, the
richer `FormFieldDescriptor`, or a raw dom_extractor dict) so it plugs into
both the OLDER Planner-facing pipeline and the NEWER Canonical Page Model
pipeline without duplicating this logic in each.
"""

from __future__ import annotations

import re
from typing import Any

from app.agent.test_data_schemas import FieldConstraint

# ---------------------------------------------------------------------------
# Semantic-type inference — label/name/placeholder/field_kind keyword
# matching, ordered most-specific-first so e.g. "employee id" is never
# swallowed by a generic "id" or "name" match.
# ---------------------------------------------------------------------------

_SEMANTIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("employee_identifier", re.compile(r"employee\s*(id|number|no\.?)|emp\s*(id|no\.?)|staff\s*(id|number)", re.IGNORECASE)),
    ("first_name", re.compile(r"\bfirst\s*name\b|\bgiven\s*name\b", re.IGNORECASE)),
    ("middle_name", re.compile(r"\bmiddle\s*name\b", re.IGNORECASE)),
    ("last_name", re.compile(r"\blast\s*name\b|\bsurname\b|\bfamily\s*name\b", re.IGNORECASE)),
    ("username", re.compile(r"\buser\s*name\b|\blogin\s*id\b|\buserid\b", re.IGNORECASE)),
    ("email", re.compile(r"\be[-\s]?mail\b", re.IGNORECASE)),
    # "contact number" is ordinary English for a phone field, in the same
    # register as "telephone" and "mobile" — not application vocabulary. It was
    # missing, so a correctly-resolved "Contact Number" label still fell through
    # to free_text and the field was filled with a random token that the
    # application rejected with "Allows numbers and only + - / ( )".
    ("phone_number", re.compile(r"\bphone\b|\btelephone\b|\bmobile\b|\b(cell|contact)\s*(number|no\.?)\b", re.IGNORECASE)),
    ("date_range", re.compile(r"date\s*range|from.*\bto\b.*date|start.*end.*date", re.IGNORECASE)),
    ("date", re.compile(r"\bdate\b|\bdob\b|\bbirthday\b", re.IGNORECASE)),
    # -- address family -------------------------------------------------------
    # These MUST precede "status", because "State or Province" is an address
    # component, not a lifecycle status. A single collapsed "address" type used
    # to serve all of them and generated a street address into City, Postal
    # Code, and Country — values every validating backend rejects. Each part of
    # an address has its own value space, so each gets its own semantic type.
    ("postal_code", re.compile(r"\bpostal\s*code\b|\bpost\s*code\b|\bzip\b|\bpincode\b|\bpin\s*code\b", re.IGNORECASE)),
    ("state_province", re.compile(r"\bstate\b\s*(or|/|&)?\s*\bprovince\b|\bprovince\b|\bcounty\b|\bregion\b", re.IGNORECASE)),
    ("country", re.compile(r"\bcountry\b|\bnation\b", re.IGNORECASE)),
    ("city", re.compile(r"\bcity\b|\btown\b|\bsuburb\b|\bmunicipality\b", re.IGNORECASE)),
    ("street_address", re.compile(r"\bstreet\b|\baddress\s*(line)?\s*\d?\b|\baddress\b|\bapt\b|\bsuite\b", re.IGNORECASE)),
    ("status", re.compile(r"\bstatus\b|\bstate\b(?!\w)", re.IGNORECASE)),
    ("role", re.compile(r"\brole\b|\bpermission\s*group\b|\bjob\s*title\b", re.IGNORECASE)),
    ("url", re.compile(r"\burl\b|\bwebsite\b|\blink\b", re.IGNORECASE)),
    ("description", re.compile(r"\bdescription\b|\bnotes?\b|\bcomments?\b|\bremarks?\b", re.IGNORECASE)),
]


def _text_blob(field: Any) -> str:
    return " ".join(
        str(x) for x in (
            getattr(field, "label", None),
            getattr(field, "accessible_name", None),
            getattr(field, "placeholder", None),
            getattr(field, "name", None),
        )
        if x
    )


def infer_semantic_type(field: Any) -> str:
    """Structural-first, then label-keyword fallback — never asserted with
    high confidence from label text alone (see `infer_constraint`'s
    confidence scoring)."""
    field_type = (getattr(field, "field_type", None) or "text").lower()
    field_kind = (getattr(field, "field_kind", None) or "").lower()
    blob = _text_blob(field)

    if field_kind == "file_upload" or field_type == "file":
        return "file_upload"
    if field_type == "checkbox" and field_kind != "checkbox_group":
        return "boolean"
    if field_kind == "date_picker":
        return "date"
    if field_kind == "autocomplete":
        return "autocomplete_selection"
    if field_kind in {"combobox", "radio_group"} or field_type == "select":
        return "dropdown_option"
    if field_type == "textarea":
        return "description"
    if field_type == "number":
        return "number"
    if field_type == "url":
        return "url"
    if field_type == "email":
        return "email"
    if field_type == "tel":
        return "phone_number"

    for semantic_type, pattern in _SEMANTIC_PATTERNS:
        if pattern.search(blob):
            return semantic_type

    if field_type == "textarea" or "description" in blob.lower():
        return "description"
    return "free_text"


# ---------------------------------------------------------------------------
# Constraint inference
# ---------------------------------------------------------------------------


def infer_constraint(
    field: Any,
    *,
    nearby_text: str = "",
    validation_message: str = "",
    rejection_messages: list[str] | None = None,
    dependent_field_keys: list[str] | None = None,
) -> FieldConstraint:
    """Every populated attribute carries its own evidence string — a
    downstream consumer can always tell WHY a constraint was inferred, never
    just what the final number is."""
    semantic_type = infer_semantic_type(field)
    evidence: list[str] = []

    required = bool(getattr(field, "required", None) or getattr(field, "is_required", None))
    if required:
        evidence.append("required indicator observed (required attribute / aria-required)")

    min_length = getattr(field, "min_length", None)
    max_length = getattr(field, "max_length", None)
    if min_length is not None:
        evidence.append(f"minlength attribute = {min_length}")
    if max_length is not None:
        evidence.append(f"maxlength attribute = {max_length}")

    pattern = getattr(field, "pattern", None)
    if pattern:
        evidence.append(f"pattern attribute = {pattern!r}")

    min_value = getattr(field, "min_value", None)
    max_value = getattr(field, "max_value", None)
    step = getattr(field, "step", None)
    if min_value is not None:
        evidence.append(f"min attribute = {min_value}")
    if max_value is not None:
        evidence.append(f"max attribute = {max_value}")

    input_type = (getattr(field, "field_type", None) or "text").lower()
    if input_type:
        evidence.append(f"input type = {input_type!r}")

    placeholder = getattr(field, "placeholder", None)
    if placeholder:
        evidence.append(f"placeholder = {placeholder!r}")

    accessible_description = getattr(field, "accessible_description", None)
    if accessible_description:
        evidence.append(f"accessible description = {accessible_description!r}")

    if nearby_text:
        evidence.append(f"nearby help text = {nearby_text!r}")
    if validation_message:
        evidence.append(f"validation message observed = {validation_message!r}")

    options = list(getattr(field, "options", None) or [])
    if options:
        evidence.append(f"{len(options)} existing option value(s) observed")

    min_date = max_date = None
    if semantic_type == "date":
        # HTML date inputs carry min/max as ISO date STRINGS: `min_value`/
        # `max_value` are always None for these (a date string never parses
        # as float — see app.perception.form_extractor._parse_float), so the
        # raw string echo (`min_value_raw`/`max_value_raw`) is the only place
        # this constraint is actually observable.
        min_date = getattr(field, "min_value_raw", None)
        max_date = getattr(field, "max_value_raw", None)
        if min_date:
            evidence.append(f"min date attribute = {min_date!r}")
        if max_date:
            evidence.append(f"max date attribute = {max_date!r}")

    depends_on = list(dependent_field_keys or [])
    if depends_on:
        evidence.append(f"depends on field(s): {', '.join(depends_on)}")

    rejections = list(rejection_messages or [])
    if rejections:
        evidence.append(f"{len(rejections)} observed rejection message(s) informed this constraint")

    # Confidence: a directly-observed HTML constraint attribute (required/
    # minlength/maxlength/pattern/min/max) is high confidence; a
    # label-text-only inference (semantic type from keyword match) alone is
    # much lower — never conflate "we saw the attribute" with "we guessed
    # from the label".
    strong_signals = sum(
        1 for v in (required, min_length, max_length, pattern, min_value, max_value) if v
    )
    confidence = min(1.0, 0.3 + 0.15 * strong_signals + (0.1 if rejections else 0.0))

    return FieldConstraint(
        field_semantic_type=semantic_type,
        required=required,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
        input_type=input_type,
        options=options,
        min_value=min_value if semantic_type != "date" else None,
        max_value=max_value if semantic_type != "date" else None,
        min_date=min_date,
        max_date=max_date,
        step=step,
        depends_on_field_keys=depends_on,
        confidence=confidence,
        evidence=evidence,
    )
