"""Cleanup action planning — turns a `TemporaryRecordEntry` awaiting cleanup
into the next `BrowserAction` needed to delete it, using ONLY structural
evidence already available on the Canonical Page Model (Turn 2's
`RecordCollection`/`action_semantics`, Turn 2/3's dialog detection) — never
a fixed selector, never a business-specific rule.

Every action this module returns still goes through the SAME
`ActionValidator`/`ActionExecutor` pipeline every other action does; nothing
here executes a browser action directly. `ActionValidator`'s own
`destructive_actions_disabled` gate (default True) means a delete click is
rejected there unless the run explicitly sets `allow_destructive_actions=
True` — this module does not, and must not, bypass that.

Delete actions keep `RiskLevel.HIGH` — removing a record IS high risk and
mislabelling it to slip past a gate would hide that from every other consumer.
They instead carry `metadata["risk_class"] = CLEANUP_RISK_CLASS` plus the
registry id of the record being removed, which is what
`ActionValidator._is_authorized_record_cleanup` requires before it will admit a
HIGH-risk action at all. This module is the only writer of that marker.
"""

from __future__ import annotations

import re
from typing import Any

from app.agent.temporary_record_registry import (
    MIN_IDENTITY_MATCH_CHARS,
    TemporaryRecordEntry,
    identity_matches_cells,
)
from app.safety.policies import CLEANUP_RISK_CLASS
from app.schemas import ActionCategory, ActionType, BrowserAction, RiskLevel

_CONFIRM_HINTS = re.compile(r"confirm|yes|delete|remove|ok\b", re.IGNORECASE)
_CANCEL_HINTS = re.compile(r"cancel|no\b|keep|dismiss", re.IGNORECASE)


def _find_collection(entry: TemporaryRecordEntry, collections: list[Any]) -> Any | None:
    if entry.collection_element_id:
        match = next((c for c in collections if getattr(c, "element_id", None) == entry.collection_element_id), None)
        if match is not None:
            return match
    # Fall back to searching every collection for a row containing the
    # record's identity — the collection_element_id may be unset (e.g. the
    # verification signal that registered this record wasn't `list_row`).
    for c in collections:
        for row in getattr(c, "visible_rows", None) or []:
            if identity_matches_cells(entry.generated_identity, row.cell_values):
                return c
    return None


def _find_delete_row_action(collection: Any, entry: TemporaryRecordEntry) -> Any | None:
    for row in getattr(collection, "visible_rows", None) or []:
        if not identity_matches_cells(entry.generated_identity, row.cell_values):
            continue
        for action in getattr(collection, "row_actions", None) or []:
            if action.row_id == row.stable_id and action.semantic_action == "delete":
                return action
    return None


def _page_shows_identity(entry: TemporaryRecordEntry, canonical_model: Any) -> bool:
    """Is the record we intend to delete demonstrably the one on screen?

    THE safety guard for detail-page deletion. A list row proves which record a
    row-level delete belongs to; a detail page's single delete button proves
    nothing on its own, so the record's own identity value must be visible
    somewhere on the page — in its text, a heading, or a form field's current
    value. When it cannot be confirmed, the answer is None and the record stays
    pending, never "delete whatever record happens to be open".
    """
    identity = (entry.generated_identity or "").strip().lower()
    if len(identity) < MIN_IDENTITY_MATCH_CHARS:
        return False
    haystacks: list[str] = []
    for block in getattr(canonical_model, "text_blocks", None) or []:
        haystacks.append(str(getattr(block, "text", "") or ""))
    for heading in getattr(canonical_model, "headings", None) or []:
        haystacks.append(str(getattr(heading, "text", "") or ""))
    for form in getattr(canonical_model, "forms", None) or []:
        for field_obj in getattr(form, "fields", None) or []:
            haystacks.append(str(getattr(field_obj, "current_value", "") or ""))
    # One-directional containment only: a page's text blocks are long and
    # arbitrary, so the reverse test that makes row matching tolerant would let
    # any short block match here. Applications do re-case what they display,
    # hence the case fold.
    return any(identity in text.lower() for text in haystacks)


# True record lists. A detail page that stacks labeled fields in similar
# `<p>`/`<div>` children is often extracted as `div_row_group` — that is not a
# list, and treating it as one hid Contact List's Delete Contact (run 55588f75).
_LIST_COLLECTION_TYPES = frozenset({"native_table", "aria_grid", "aria_treegrid"})


def _is_record_list_state(canonical_model: Any) -> bool:
    """True when the page is a real record collection, not a detail field stack."""
    for collection in getattr(canonical_model, "collections", None) or []:
        if getattr(collection, "collection_type", "") in _LIST_COLLECTION_TYPES:
            return True
    return False


def _find_detail_state_delete(entry: TemporaryRecordEntry, canonical_model: Any) -> str | None:
    """A `delete` control on a record-detail page showing THIS record."""
    if _is_record_list_state(canonical_model):
        # A native/ARIA table is a list state; the row-level path above is the
        # correct (and safely record-scoped) one. Spurious div-row groups on a
        # detail page must not hide the page-level Delete control.
        return None
    if not _page_shows_identity(entry, canonical_model):
        return None
    for sem in getattr(canonical_model, "action_semantics", None) or []:
        if getattr(sem, "semantic_action", None) in {"delete", "remove"} and getattr(sem, "element_id", None):
            return str(sem.element_id)
    return None


def _find_confirmation_control(dialogs: list[Any], action_semantics: list[Any]) -> str | None:
    """Best-effort: an action-semantics entry classified `confirm`, or the
    strongest text-matched control, INSIDE a currently-open dialog."""
    open_dialog_element_ids: set[str] = set()
    for d in dialogs or []:
        open_dialog_element_ids.update(getattr(d, "contained_element_ids", None) or [])
        if getattr(d, "element_id", None):
            open_dialog_element_ids.add(d.element_id)
    for sem in action_semantics or []:
        if sem.element_id in open_dialog_element_ids and sem.semantic_action == "confirm":
            return sem.element_id
    for sem in action_semantics or []:
        if sem.element_id in open_dialog_element_ids and sem.semantic_action == "delete":
            return sem.element_id
    return None


def _same_location(a: str | None, b: str | None) -> bool:
    def _key(url: str | None) -> str:
        return (url or "").split("#", 1)[0].rstrip("/").lower()

    return bool(a) and _key(a) == _key(b)


def _find_in_app_route(target_url: str, canonical_model: Any) -> str | None:
    """An in-application control that navigates to `target_url`.

    Preferred over `open_url` because a hard navigation reloads the document,
    and an application that keeps its session in memory rather than in a cookie
    is logged out by that. A live cleanup pass opened the record's list URL
    directly, landed unauthenticated, observed a page with two elements, and
    concluded the record was unreachable — the record was fine; the session
    wasn't.

    Structural only: a link's own target compared with where we need to go. No
    label matching, no route conventions.
    """
    for region in getattr(canonical_model, "navigation_regions", None) or []:
        for item in getattr(region, "items", None) or []:
            if getattr(item, "element_id", None) and _same_location(
                getattr(item, "target_url", None), target_url
            ):
                return str(item.element_id)
    for crumb in getattr(canonical_model, "breadcrumbs", None) or []:
        if getattr(crumb, "element_id", None) and _same_location(
            getattr(crumb, "target_url", None), target_url
        ):
            return str(crumb.element_id)
    return None


def plan_cleanup_navigation(
    entry: TemporaryRecordEntry, *, canonical_model: Any, current_url: str | None
) -> BrowserAction | None:
    """The next step needed to GET somewhere this record can be deleted from.

    Cleanup runs once at the end of a run, and by then the browser is usually on
    an unrelated page — so `plan_cleanup_action` found nothing and every record
    was left pending. Delete was discovered every run and performed in none.

    Two bounded, read-only steps, in order:

      1. Return to the URL where the record was last seen in a collection.
      2. On that page, open the row whose cells carry this record's identity.

    Both are ordinary navigation, and step 2 is identity-matched — this never
    opens "whatever record happens to be first". Returns None when neither step
    applies, and the caller leaves the record pending rather than guessing.
    """
    if entry.current_state not in {"verified", "updated", "cleanup_failed"}:
        return None

    if entry.list_url and not _same_location(current_url, entry.list_url):
        in_app = _find_in_app_route(entry.list_url, canonical_model)
        if in_app is not None:
            return BrowserAction(
                action=ActionType.CLICK,
                element_id=in_app,
                reason=f"Navigate within the application back to where GemmaQA's temporary record '{entry.generated_identity}' was last seen",
                expected_result="The record's list page loads with the session intact.",
                risk=RiskLevel.LOW,
                category=ActionCategory.NAVIGATION_TEST,
                metadata={
                    "cleanup_temporary_record_id": entry.temporary_record_id,
                    "cleanup_step": "navigate_to_list",
                    "risk_class": CLEANUP_RISK_CLASS,
                    "action_label": "Back to list",
                },
            )
        return BrowserAction(
            action=ActionType.OPEN_URL,
            url=entry.list_url,
            reason=f"Return to where GemmaQA's temporary record '{entry.generated_identity}' was last seen, to clean it up",
            expected_result="The record's list page loads.",
            risk=RiskLevel.LOW,
            category=ActionCategory.NAVIGATION_TEST,
            metadata={
                "cleanup_temporary_record_id": entry.temporary_record_id,
                "cleanup_step": "navigate_to_list",
                "risk_class": CLEANUP_RISK_CLASS,
                "action_label": "Open URL",
            },
        )

    identity = (entry.generated_identity or "").strip()
    if len(identity) < MIN_IDENTITY_MATCH_CHARS:
        return None
    for collection in getattr(canonical_model, "collections", None) or []:
        for row in getattr(collection, "visible_rows", None) or []:
            if not getattr(row, "element_id", None) or not getattr(row, "is_activatable", False):
                continue
            if not identity_matches_cells(identity, row.cell_values):
                continue
            return BrowserAction(
                action=ActionType.CLICK,
                element_id=row.element_id,
                reason=f"Open GemmaQA's temporary record '{identity}' so its own controls can be reached",
                expected_result="The record's own page opens.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EXPLORATION,
                metadata={
                    "cleanup_temporary_record_id": entry.temporary_record_id,
                    "cleanup_step": "open_record",
                    "risk_class": CLEANUP_RISK_CLASS,
                    "action_label": str(getattr(row, "identity_hint", None) or identity)[:80],
                },
            )
    return None


def plan_cleanup_action(entry: TemporaryRecordEntry, *, canonical_model: Any) -> BrowserAction | None:
    """Returns the next action to advance THIS entry's cleanup, or None if
    nothing on the CURRENT page can advance it (the caller decides whether
    to navigate elsewhere and retry, or leave it pending)."""
    if canonical_model is None:
        return None
    collections = list(getattr(canonical_model, "collections", None) or [])
    dialogs = list(getattr(canonical_model, "dialogs", None) or [])
    action_semantics = list(getattr(canonical_model, "action_semantics", None) or [])

    if entry.current_state in {"verified", "updated", "cleanup_failed"}:
        delete_element_id: str | None = None
        collection = _find_collection(entry, collections)
        if collection is not None:
            delete_action = _find_delete_row_action(collection, entry)
            if delete_action is not None and delete_action.element_id:
                delete_element_id = delete_action.element_id
        if delete_element_id is None:
            # No collection-row delete. The record's own detail page usually has
            # one — and until this existed, cleanup could never advance for any
            # application that puts delete there rather than in the list, which
            # is most of them.
            delete_element_id = _find_detail_state_delete(entry, canonical_model)
        if delete_element_id is None:
            return None
        return BrowserAction(
            action=ActionType.CLICK,
            element_id=delete_element_id,
            reason=f"Delete GemmaQA temporary test record '{entry.generated_identity}'",
            expected_result="A confirmation dialog appears, or the record is removed.",
            risk=RiskLevel.HIGH,
            category=ActionCategory.EXPLORATION,
            metadata={
                "cleanup_temporary_record_id": entry.temporary_record_id,
                "cleanup_step": "delete_control",
                "risk_class": CLEANUP_RISK_CLASS,
            },
        )

    if entry.current_state == "cleanup_requested":
        confirm_id = _find_confirmation_control(dialogs, action_semantics)
        if not confirm_id:
            return None
        return BrowserAction(
            action=ActionType.CLICK,
            element_id=confirm_id,
            reason=f"Confirm deletion of GemmaQA temporary test record '{entry.generated_identity}'",
            expected_result="Record is deleted; dialog closes.",
            risk=RiskLevel.HIGH,
            category=ActionCategory.EXPLORATION,
            metadata={
                "cleanup_temporary_record_id": entry.temporary_record_id,
                "cleanup_step": "confirm_delete",
                "risk_class": CLEANUP_RISK_CLASS,
            },
        )

    return None
