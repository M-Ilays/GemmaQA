"""Form extraction — builds the (reused) `FormDescriptor`/`FormField` plus the
richer, additive `FormFieldDescriptor` companion from the raw form facts
`dom_extractor` collected.

`FormDescriptor`/`FormField` are populated exactly as the existing observer
would (so anything reading the legacy shape sees no difference); the new
`field_descriptors` list carries the same fields with the full generic
perception vocabulary (accessible_name/description, ARIA checked state,
bounding box, confidence, evidence) alongside them.
"""

from __future__ import annotations

from app.schemas import FormDescriptor, FormField, FormFieldDescriptor

_DATE_TYPES = frozenset({"date", "month", "week", "time", "datetime-local"})


def _infer_field_kind(f: dict, *, checkbox_group_names: set[str]) -> str:
    """Structural classification only — field_type/role/list/multiple/
    aria-autocomplete attributes, never a business label. Order matters:
    file/date/multi-select are unambiguous from field_type/attributes alone
    and checked first; combobox vs autocomplete both need a `role`/`list`
    signal so a plain `<input type="text">` stays "text", never guessed."""
    field_type = (f.get("field_type") or "").lower()
    role = (f.get("role") or "").lower()
    name = f.get("name") or ""

    if field_type == "file":
        return "file_upload"
    if field_type in _DATE_TYPES:
        return "date_picker"
    if field_type == "radio":
        return "radio_group"
    if field_type == "checkbox" and name in checkbox_group_names:
        return "checkbox_group"
    if f.get("multiple") or role == "listbox":
        return "multi_select"
    if f.get("list_attr") or f.get("aria_autocomplete"):
        return "autocomplete"
    if role == "combobox":
        return "combobox"
    if field_type in {"text", "search", "email", "tel", "url", "number", "password", "textarea", ""}:
        return "text"
    return "unknown"


def _parse_float(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_forms(raw) -> list[FormDescriptor]:  # raw: RawObservation (avoid import cycle in type hints)
    forms: list[FormDescriptor] = []
    for form in raw.forms:
        dom_id = form.get("dom_id")
        if not dom_id:
            continue
        raw_fields = list(form.get("fields") or [])
        name_counts: dict[str, int] = {}
        for f in raw_fields:
            if (f.get("field_type") or "").lower() == "checkbox" and f.get("name"):
                name_counts[f["name"]] = name_counts.get(f["name"], 0) + 1
        checkbox_group_names = {name for name, count in name_counts.items() if count >= 2}
        fields = [
            FormField(
                name=f.get("name"),
                field_type=f.get("field_type") or "text",
                label=f.get("label") or f.get("accessible_name"),
                required=bool(f.get("required")),
                placeholder=f.get("placeholder"),
                options=list(f.get("options") or []),
                element_id=f.get("dom_id"),
                disabled=bool(f.get("disabled")),
                current_value=f.get("current_value"),
            )
            for f in raw_fields
        ]
        field_descriptors = [
            FormFieldDescriptor(
                stable_id=f.get("dom_id") or dom_id,
                element_id=f.get("dom_id"),
                parent_region_id=dom_id,
                source="dom",
                text=f.get("current_value"),
                accessible_name=f.get("accessible_name"),
                accessible_description=f.get("accessible_description"),
                dom_tag="input",
                field_type=f.get("field_type") or "text",
                label=f.get("label") or f.get("accessible_name"),
                placeholder=f.get("placeholder"),
                options=list(f.get("options") or []),
                current_value=f.get("current_value"),
                is_required=bool(f.get("required")),
                is_checked=f.get("checked"),
                is_enabled=not bool(f.get("disabled")),
                confidence=1.0,
                status="observed",
                field_kind=_infer_field_kind(f, checkbox_group_names=checkbox_group_names),
                min_length=_parse_int(f.get("minlength")),
                max_length=_parse_int(f.get("maxlength")),
                pattern=f.get("pattern") or None,
                min_value=_parse_float(f.get("min_attr")),
                max_value=_parse_float(f.get("max_attr")),
                step=_parse_float(f.get("step_attr")),
                min_value_raw=f.get("min_attr") or None,
                max_value_raw=f.get("max_attr") or None,
            )
            for f in raw_fields
            if f.get("dom_id")
        ]
        forms.append(
            FormDescriptor(
                form_id=dom_id,
                action=form.get("action"),
                method=form.get("method"),
                fields=fields,
                submit_element_id=form.get("submit_element_id"),
                stable_id=dom_id,
                source="dom",
                bounding_box=form.get("bounding_box"),
                confidence=1.0,
                field_descriptors=field_descriptors,
            )
        )
    return forms
