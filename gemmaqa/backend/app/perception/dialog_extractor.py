"""Dialog/alert extraction — builds `DialogDescriptor`/`AlertDescriptor` from
the raw overlay facts `dom_extractor` collected, including
`contained_element_ids` (the audit's "elements inside a dialog aren't linked to
it" finding) — every element `dom_extractor` tagged during its DOM walk that
falls inside the dialog's subtree is linked here.
"""

from __future__ import annotations

from app.perception.models import AlertDescriptor, DialogDescriptor

_DIALOG_ROLE_TYPES = {"dialog": "dialog", "alertdialog": "modal"}


def extract_dialogs(raw) -> list[DialogDescriptor]:  # raw: RawObservation
    dialogs: list[DialogDescriptor] = []
    for d in raw.dialogs:
        dom_id = d.get("dom_id")
        if not dom_id:
            continue
        role = (d.get("role") or "").lower()
        dialog_type = _DIALOG_ROLE_TYPES.get(role, "modal" if role else "dialog")
        # An overlay found by its stacking layer rather than by an ARIA role is a
        # real overlay — measured, not declared — but the page never said so, and
        # a large positioned layer holding the viewport centre is occasionally
        # just a layout. Reported at lower confidence so the frontier's Escape
        # candidate stays cheap and honest about what it is acting on.
        by_mechanism = d.get("detected_by") == "stacking_layer"
        if by_mechanism and not role:
            dialog_type = "modal"
        dialogs.append(
            DialogDescriptor(
                element_id=dom_id,
                stable_id=dom_id,
                aria_role=d.get("role"),
                text=d.get("text"),
                dialog_type=dialog_type,
                is_open=True,
                contained_element_ids=list(d.get("contained_element_ids") or []),
                source="dom",
                confidence=0.6 if by_mechanism else 1.0,
            )
        )
    return dialogs


def extract_alerts(raw) -> list[AlertDescriptor]:  # raw: RawObservation
    return [
        AlertDescriptor(
            text=a.get("text"),
            severity=a.get("severity") or "info",
            source="dom",
            confidence=0.8,
        )
        for a in raw.alerts
        if a.get("text")
    ]
