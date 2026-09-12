"""Human-readable live-action labels for the activity log and browser overlay.

Observational only: these functions never decide, execute, or delay a QA action.
They format what the run is already doing so an operator can follow it.
"""

from __future__ import annotations

from typing import Any

from app.safety.sensitive import find_element

# Closed verb list for the live indicator. Unknown action types fall back to
# their own name in uppercase so a new ActionType is still readable.
_ACTION_LINE_VERB = {
    "click": "CLICK",
    "open_tab": "CLICK",
    "fill": "FILL",
    "fill_form": "FILL",
    "submit": "CLICK",
    "select": "SELECT",
    "check": "CHECK",
    "uncheck": "UNCHECK",
    "open_url": "NAVIGATE",
    "go_back": "NAVIGATE",
    "refresh": "NAVIGATE",
    "inspect_form": "INSPECT_FORM",
    "inspect_table": "INSPECT",
    "press": "PRESS",
    "hover": "HOVER",
    "wait": "WAIT",
    "take_screenshot": "SCREENSHOT",
    "finish": "FINISH",
}

_STATUS_VERB = {
    "click": "CLICKING",
    "open_tab": "CLICKING",
    "fill": "FILLING",
    "fill_form": "FILLING",
    "submit": "SUBMITTING FORM",
    "select": "SELECTING",
    "check": "CHECKING",
    "uncheck": "UNCHECKING",
    "open_url": "NAVIGATING",
    "go_back": "NAVIGATING",
    "refresh": "NAVIGATING",
    "inspect_form": "INSPECTING FORM",
    "inspect_table": "INSPECTING",
    "press": "PRESSING",
    "hover": "HOVERING",
    "wait": "WAITING",
    "take_screenshot": "CAPTURING",
}


def _element_name(el: Any) -> str:
    return (
        (getattr(el, "accessible_name", None) or "").strip()
        or (getattr(el, "label", None) or "").strip()
        or (getattr(el, "visible_text", None) or "").strip()
        or (getattr(el, "text", None) or "").strip()
        or (getattr(el, "placeholder", None) or "").strip()
    )


def live_action_target(action: Any, page_state: Any | None = None) -> str:
    """The operator-facing name of the thing being acted on."""
    meta = getattr(action, "metadata", None) or {}
    labelled = str(meta.get("action_label") or "").strip()
    element_id = getattr(action, "element_id", None)
    el = find_element(page_state, element_id)
    el_name = _element_name(el) if el is not None else ""
    action_type = str(getattr(getattr(action, "action", None), "value", None) or "")
    # Fills must name the field. The planner sometimes copies a page heading
    # onto every input as action_label; the element's own name is the truth.
    if action_type in {"fill", "select", "check", "uncheck"} and el_name:
        return el_name[:80]
    if labelled:
        return labelled[:80]
    if el_name:
        return el_name[:80]
    url = getattr(action, "url", None) or getattr(action, "value", None)
    if url and getattr(getattr(action, "action", None), "value", "") in {
        "open_url",
        "go_back",
        "refresh",
    }:
        return str(url)[:80]
    if element_id:
        return str(element_id)
    return str(getattr(getattr(action, "action", None), "value", None) or "action")


def _is_submit(action: Any, target: str) -> bool:
    """True only for an actual form submit — never for Sign up / Add Contact alone."""
    meta = getattr(action, "metadata", None) or {}
    if meta.get("auth_submit") or meta.get("form_workflow_submit"):
        return True
    return "submit" in (target or "").lower()


def format_live_action_line(
    action_type: str,
    target: str,
    *,
    outcome: str | None = None,
    reason: str | None = None,
    submit: bool = False,
) -> str:
    """One line: `CLICK → Sign up` or `BLOCKED → Delete` + optional reason."""
    verb = _ACTION_LINE_VERB.get(action_type, (action_type or "ACTION").upper())
    label = (target or "control").strip() or "control"
    if submit and action_type in {"click", "open_tab"} and "submit" not in label.lower():
        label = f"{label} / Submit"
    if outcome == "blocked":
        line = f"BLOCKED → {label}"
        if reason:
            line = f"{line}\nREASON → {reason}"
        return line
    if outcome == "failed":
        line = f"FAILED → {label}"
        if reason:
            line = f"{line}\nREASON → {reason}"
        return line
    return f"{verb} → {label}"


def format_live_status_line(action_type: str, target: str, *, submit: bool = False) -> str:
    """Current-action banner: `CLICKING: Sign up` or `SUBMITTING FORM`."""
    if submit and action_type in {"click", "open_tab", "fill"}:
        return "SUBMITTING FORM"
    verb = _STATUS_VERB.get(action_type, (action_type or "RUNNING").upper())
    label = (target or "").strip()
    return f"{verb}: {label}" if label else verb


def live_action_fields(action: Any, page_state: Any | None = None) -> dict[str, str]:
    """Keys to merge into an existing activity-log payload. No new events."""
    action_type = str(getattr(getattr(action, "action", None), "value", None) or "")
    target = live_action_target(action, page_state)
    submit = _is_submit(action, target)
    return {
        "action_label": target,
        "live_action_line": format_live_action_line(action_type, target, submit=submit),
        "live_status_line": format_live_status_line(action_type, target, submit=submit),
    }
