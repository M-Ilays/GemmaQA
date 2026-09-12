"""Sanitize page/model context — never send secrets to Gemma."""

from __future__ import annotations

from typing import Any

from app.schemas import InteractiveElement, PageState
from app.utils.sanitization import MASK, sanitize_dict, sanitize_text, sanitize_url

PASSWORDISH = {
    "password",
    "passwd",
    "pwd",
    "passcode",
    "secret",
    "api_key",
    "token",
    "authorization",
    "cookie",
    "session",
}


def _is_sensitive_field(el: InteractiveElement) -> bool:
    bits = " ".join(
        filter(
            None,
            [
                el.input_type,
                el.type,
                el.name,
                el.id_attr,
                el.placeholder,
                el.label,
                el.accessible_name,
                el.aria_label,
            ],
        )
    ).lower()
    if (el.input_type or el.type or "").lower() == "password":
        return True
    if (el.input_type or el.type or "").lower() == "hidden":
        return True
    return any(k in bits for k in PASSWORDISH)


def sanitize_element_for_model(el: InteractiveElement) -> dict[str, Any]:
    data = {
        "element_id": el.element_id,
        "tag": el.tag,
        "category": el.category,
        "role": el.role,
        "accessible_name": el.accessible_name or el.text or el.visible_text,
        "input_type": el.input_type or el.type,
        "href": sanitize_url(el.href) if el.href else None,
        "required": el.required,
        "disabled": el.disabled,
        "placeholder": el.placeholder,
    }
    if _is_sensitive_field(el):
        data["current_value"] = MASK if el.current_value else None
        data["input_type"] = data["input_type"] or "password"
        data["sensitive"] = True
    else:
        # Never forward raw values that look like secrets
        val = el.current_value
        if val and any(k in (el.name or "").lower() for k in PASSWORDISH):
            data["current_value"] = MASK
        else:
            data["current_value"] = None  # omit free-text values from model context
    return data


def sanitize_page_state_for_model(page_state: PageState | dict[str, Any]) -> dict[str, Any]:
    """Build a model-safe page observation (no credentials, cookies, hidden values)."""
    if hasattr(page_state, "model_dump"):
        raw = page_state.model_dump(mode="json")
        elements = page_state.interactive_elements  # type: ignore[attr-defined]
        forms = page_state.forms  # type: ignore[attr-defined]
        url = page_state.url  # type: ignore[attr-defined]
    else:
        raw = dict(page_state)
        elements = []
        forms = raw.get("forms") or []
        url = raw.get("url") or ""

    safe_elements = []
    if elements and hasattr(elements[0], "element_id"):
        for el in elements[:60]:
            safe_elements.append(sanitize_element_for_model(el))
    else:
        for el in (raw.get("interactive_elements") or [])[:60]:
            if isinstance(el, dict):
                copy = {k: el.get(k) for k in (
                    "element_id", "tag", "category", "role", "accessible_name",
                    "input_type", "type", "href", "required", "disabled", "placeholder",
                )}
                itype = (copy.get("input_type") or copy.get("type") or "").lower()
                if itype in {"password", "hidden"}:
                    copy["current_value"] = MASK
                    copy["sensitive"] = True
                else:
                    copy.pop("current_value", None)
                if copy.get("href"):
                    copy["href"] = sanitize_url(str(copy["href"]))
                safe_elements.append(copy)

    safe_forms = []
    for form in forms:
        if hasattr(form, "model_dump"):
            f = form.model_dump(mode="json")
        else:
            f = dict(form)
        fields = []
        for field in f.get("fields") or []:
            ft = (field.get("field_type") or field.get("type") or "").lower()
            name = (field.get("name") or field.get("label") or "").lower()
            if ft in {"password", "hidden"} or any(k in name for k in PASSWORDISH):
                field = {**field, "current_value": MASK if field.get("current_value") else None}
            else:
                field = {**field, "current_value": None}
            fields.append(field)
        safe_forms.append({
            "form_id": f.get("form_id"),
            "action": sanitize_url(f["action"]) if f.get("action") else None,
            "method": f.get("method"),
            "fields": fields,
            "submit_element_id": f.get("submit_element_id"),
        })

    return sanitize_dict(
        {
            "url": sanitize_url(str(url)),
            "title": raw.get("title"),
            "headings": (raw.get("headings") or [])[:12],
            "visible_text_summary": sanitize_text((raw.get("visible_text_summary") or "")[:500]),
            "breadcrumbs": raw.get("breadcrumbs") or [],
            "navigation_items": (raw.get("navigation_items") or [])[:20],
            "forms": safe_forms,
            "tables": [
                {
                    "table_id": t.get("table_id") if isinstance(t, dict) else getattr(t, "table_id", None),
                    "headers": (t.get("headers") if isinstance(t, dict) else getattr(t, "headers", []))[:20],
                    "row_count": t.get("row_count") if isinstance(t, dict) else getattr(t, "row_count", 0),
                }
                for t in (raw.get("tables") or [])[:5]
            ],
            "tabs": raw.get("tabs") or [],
            "dialogs": raw.get("dialogs") or [],
            "modals": raw.get("modals") or [],
            "toasts": raw.get("toasts") or [],
            "alerts": [sanitize_text(a) for a in (raw.get("alerts") or [])[:10]],
            "console_errors": [sanitize_text(e) for e in (raw.get("console_errors") or [])[:10]],
            "network_failures": [sanitize_text(e) for e in (raw.get("network_failures") or [])[:10]],
            "interactive_elements": safe_elements,
            "state_fingerprint": raw.get("state_fingerprint"),
            # Explicitly omit cookies, storage, authorization, raw HTML
        }
    )
