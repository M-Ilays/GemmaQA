"""CRUD workflow candidate extraction — turns `RecordCollection`/action-
semantics/form-intent evidence into `CRUDWorkflowHypothesis` candidates.

Two distinct passes, matching the confidence discipline every other
intelligence package in this codebase follows:

- `build_entry_points()` — a SINGLE observation: "this collection has a
  control labelled add/edit/delete". Always `status="entry_point"`, always
  low confidence — a control existing is not proof the operation works.
- `build_fulfilled()` — a BEFORE/AFTER pair, after `executed_element_id`
  was actually clicked: looks for corroborating evidence (a create/edit-
  shaped form appearing, a confirmation dialog, an observed row-count
  decrease) before advancing a hypothesis to `status="supported"`.

Per the commissioning task's explicit constraint, a DELETE hypothesis is
NEVER advanced past `entry_point` on icon/label evidence alone — it
requires a confirmation dialog OR an observed row-count decrease.
"""

from __future__ import annotations

import re
from typing import Any

from app.intelligence.crud_discovery.schemas import CRUDEvidence, CRUDWorkflowHypothesis

_CONFIRMATION_HINTS = re.compile(r"are you sure|please confirm|cannot be undone|permanently", re.IGNORECASE)


def _entity_hypothesis_for(model: Any) -> str | None:
    if getattr(model, "headings", None):
        return model.headings[0].text
    return None


def _evidence(source_kind: str, text: str, *, page_url: str, element_id: str | None = None) -> CRUDEvidence:
    return CRUDEvidence(source_kind=source_kind, observed_text=(text or "")[:160], page_url=page_url, element_id=element_id)


def _looks_like_confirmation(text: str) -> bool:
    return bool(_CONFIRMATION_HINTS.search(text or ""))


def _is_record_detail_state(model: Any) -> bool:
    """Is this page one RECORD rather than a list of them?

    Structural only: no collection on the page, but per-record action semantics
    (edit/delete) are present. That is exactly the shape of a record-detail
    view, and it is where update and delete controls almost always live —
    observed live, where "Edit Contact"/"Delete Contact" sat on a detail page
    while every CRUD hypothesis required the control to hang off a COLLECTION,
    so update and delete stayed at zero DISCOVERED for an entire run even after
    the agent had reached and filled the edit form.
    """
    if getattr(model, "collections", None):
        return False
    return any(
        getattr(a, "semantic_action", None) in _RECORD_OPERATION_SEMANTICS
        for a in (getattr(model, "action_semantics", None) or [])
    )


def _detail_state_controls(model: Any) -> list[tuple[str, str]]:
    """`(operation, element_id)` for each per-record control on a detail state."""
    found: list[tuple[str, str]] = []
    for semantics in getattr(model, "action_semantics", None) or []:
        operation = _RECORD_OPERATION_SEMANTICS.get(getattr(semantics, "semantic_action", None) or "")
        element_id = getattr(semantics, "element_id", None)
        if operation and element_id:
            found.append((operation, str(element_id)))
    return found


# Semantic actions that operate on the ONE record a detail state is showing.
# Read from the perception layer's own closed `ActionSemantics` vocabulary — no
# label matching, no business words.
_RECORD_OPERATION_SEMANTICS: dict[str, str] = {
    "edit": "edit",
    "update": "edit",
    "delete": "delete",
    "remove": "delete",
}


class CRUDCandidateBuilder:
    def build_entry_points(self, model: Any, *, current_actor_term: str | None = None) -> list[CRUDWorkflowHypothesis]:
        out: list[CRUDWorkflowHypothesis] = []
        entity = _entity_hypothesis_for(model)

        for collection in model.collections or []:
            for action in collection.global_actions:
                if action.semantic_action not in {"add", "create", "new"}:
                    continue
                out.append(
                    CRUDWorkflowHypothesis(
                        operation="create",
                        entity_hypothesis=entity,
                        actor_hypothesis=current_actor_term,
                        source_state="list",
                        required_controls=[action.element_id] if action.element_id else [],
                        verification_surface=collection.element_id,
                        collection_id=collection.element_id,
                        confidence=0.35,
                        safety_classification="controlled_write",
                        evidence=[
                            _evidence("global_action_control", action.semantic_action or "", page_url=model.url, element_id=action.element_id),
                            _evidence("record_collection", collection.collection_type, page_url=model.url, element_id=collection.element_id),
                        ],
                        source_page_url=model.url,
                        status="entry_point",
                    )
                )

            for action in collection.row_actions:
                if action.semantic_action == "edit":
                    out.append(
                        CRUDWorkflowHypothesis(
                            operation="edit",
                            entity_hypothesis=entity,
                            actor_hypothesis=current_actor_term,
                            source_state="record_row",
                            required_controls=[action.element_id] if action.element_id else [],
                            verification_surface=collection.element_id,
                            collection_id=collection.element_id,
                            confidence=0.35,
                            safety_classification="controlled_write",
                            evidence=[_evidence("row_action_control", "edit", page_url=model.url, element_id=action.element_id)],
                            source_page_url=model.url,
                            status="entry_point",
                        )
                    )
                elif action.semantic_action == "delete":
                    # Weak entry point ONLY — a trash icon/label is not
                    # proof of a working delete workflow. `build_fulfilled`
                    # is the only path that can advance this past
                    # "entry_point".
                    out.append(
                        CRUDWorkflowHypothesis(
                            operation="delete",
                            entity_hypothesis=entity,
                            actor_hypothesis=current_actor_term,
                            source_state="record_row",
                            required_controls=[action.element_id] if action.element_id else [],
                            verification_surface=collection.element_id,
                            collection_id=collection.element_id,
                            confidence=0.2,
                            safety_classification="destructive",
                            cleanup_possibility="not_possible",
                            evidence=[_evidence("row_action_control", "delete", page_url=model.url, element_id=action.element_id)],
                            source_page_url=model.url,
                            status="entry_point",
                        )
                    )

        # A record-detail state: the per-record update/delete controls, which no
        # collection ever contains. Still `entry_point` — a control existing is
        # not proof the operation works, exactly as for collection row actions.
        if _is_record_detail_state(model):
            for operation, element_id in _detail_state_controls(model):
                destructive = operation == "delete"
                out.append(
                    CRUDWorkflowHypothesis(
                        operation=operation,
                        entity_hypothesis=entity,
                        actor_hypothesis=current_actor_term,
                        source_state="record_detail",
                        target_state="edit_form_open" if operation == "edit" else "record_removed",
                        required_controls=[element_id],
                        expected_transition=(
                            "record detail -> edit form (prefilled) -> save -> updated value visible"
                            if operation == "edit"
                            else "record detail -> delete control -> confirmation -> record absent from collection"
                        ),
                        confidence=0.2,
                        safety_classification="destructive" if destructive else "controlled_write",
                        cleanup_possibility="not_possible" if destructive else "possible",
                        evidence=[
                            _evidence(
                                "record_detail_control", operation, page_url=model.url, element_id=element_id
                            )
                        ],
                        source_page_url=model.url,
                        status="entry_point",
                    )
                )
        return out

    def build_fulfilled(
        self,
        before_model: Any,
        after_model: Any,
        *,
        executed_element_id: str | None,
        action_succeeded: bool,
        current_actor_term: str | None = None,
    ) -> list[CRUDWorkflowHypothesis]:
        if not executed_element_id or not action_succeeded:
            return []

        source_collection = None
        matched_action = None
        for collection in before_model.collections or []:
            for action in [*collection.global_actions, *collection.row_actions]:
                if action.element_id == executed_element_id:
                    source_collection, matched_action = collection, action
                    break
            if matched_action is not None:
                break
        if matched_action is None:
            # Not a collection control. It may still be a per-record control on
            # a record-detail state, which no collection contains.
            return self._build_fulfilled_from_detail_state(
                before_model, after_model,
                executed_element_id=executed_element_id,
                current_actor_term=current_actor_term,
            )

        entity = _entity_hypothesis_for(before_model) or _entity_hypothesis_for(after_model)
        collection_id = source_collection.element_id if source_collection else None
        out: list[CRUDWorkflowHypothesis] = []

        if matched_action.semantic_action in {"add", "create", "new"}:
            create_intent = next((fi for fi in (after_model.form_intents or []) if fi.intent == "create"), None)
            if create_intent is not None:
                out.append(
                    CRUDWorkflowHypothesis(
                        operation="create",
                        entity_hypothesis=entity,
                        actor_hypothesis=current_actor_term,
                        source_state="list",
                        target_state="create_form_open",
                        required_controls=[executed_element_id],
                        required_form_id=create_intent.form_id,
                        expected_transition="list -> create_form -> save -> list (record added)",
                        verification_surface=collection_id,
                        collection_id=collection_id,
                        confidence=0.6,
                        safety_classification="controlled_write",
                        cleanup_possibility="possible",
                        evidence=[
                            _evidence("form_intent", "create", page_url=after_model.url, element_id=create_intent.form_id),
                            _evidence("navigation_transition", f"{before_model.url} -> {after_model.url}", page_url=after_model.url),
                        ],
                        source_page_url=before_model.url,
                        status="supported",
                    )
                )

        elif matched_action.semantic_action == "edit":
            edit_intent = next((fi for fi in (after_model.form_intents or []) if fi.intent == "edit"), None)
            if edit_intent is not None:
                out.append(
                    CRUDWorkflowHypothesis(
                        operation="edit",
                        entity_hypothesis=entity,
                        actor_hypothesis=current_actor_term,
                        source_state="record_row",
                        target_state="edit_form_open",
                        required_controls=[executed_element_id],
                        required_form_id=edit_intent.form_id,
                        expected_transition="record row -> edit_form (prefilled) -> save -> updated value visible",
                        verification_surface=collection_id,
                        collection_id=collection_id,
                        confidence=0.6,
                        safety_classification="controlled_write",
                        cleanup_possibility="possible",
                        evidence=[
                            _evidence("form_intent", "edit", page_url=after_model.url, element_id=edit_intent.form_id),
                            _evidence("field_prepopulation", "edit form fields carry existing values", page_url=after_model.url),
                        ],
                        source_page_url=before_model.url,
                        status="supported",
                    )
                )

        elif matched_action.semantic_action == "delete":
            evidence: list[CRUDEvidence] = []
            corroborated = False
            confirmation_dialog = next(
                (d for d in (after_model.dialogs or []) if _looks_like_confirmation(d.text or "")), None
            )
            if confirmation_dialog is not None:
                evidence.append(
                    _evidence("confirmation_dialog", confirmation_dialog.text or "", page_url=after_model.url, element_id=confirmation_dialog.element_id)
                )
                corroborated = True

            before_count = source_collection.row_count if source_collection else None
            after_collection = next(
                (c for c in (after_model.collections or []) if collection_id and c.element_id == collection_id), None
            )
            if before_count is not None and after_collection is not None and after_collection.row_count < before_count:
                evidence.append(
                    _evidence("row_count_decrease", f"{before_count} -> {after_collection.row_count}", page_url=after_model.url, element_id=collection_id)
                )
                corroborated = True

            if corroborated:
                out.append(
                    CRUDWorkflowHypothesis(
                        operation="delete",
                        entity_hypothesis=entity,
                        actor_hypothesis=current_actor_term,
                        source_state="record_row",
                        target_state="record_removed",
                        required_controls=[executed_element_id],
                        expected_transition="record row -> delete control -> confirmation -> confirm -> row removed from collection",
                        verification_surface=collection_id,
                        collection_id=collection_id,
                        confidence=0.65,
                        safety_classification="destructive",
                        cleanup_possibility="not_possible",
                        evidence=evidence,
                        source_page_url=before_model.url,
                        status="supported",
                    )
                )
        return out

    def _build_fulfilled_from_detail_state(
        self,
        before_model: Any,
        after_model: Any,
        *,
        executed_element_id: str,
        current_actor_term: str | None = None,
    ) -> list[CRUDWorkflowHypothesis]:
        """Corroborate a per-record control that was actually clicked.

        The collection path above can never see these: an update or delete
        control on a record-detail page belongs to no collection, so
        `matched_action` is always None there and every detail-state operation
        was silently dropped.

        Same confidence discipline as the collection path — an edit is
        `supported` only when a form actually appeared, and a delete is NEVER
        advanced past `entry_point` on control evidence alone.
        """
        if not _is_record_detail_state(before_model):
            return []
        operation = next(
            (op for op, el in _detail_state_controls(before_model) if el == executed_element_id), None
        )
        if operation is None:
            return []

        entity = _entity_hypothesis_for(before_model) or _entity_hypothesis_for(after_model)
        if operation != "edit":
            # Delete corroboration needs a confirmation dialog or an observed
            # row-count decrease, neither of which is visible from the detail
            # state we just left. Reported by `build_entry_points` instead.
            return []

        # The edit control opened a form. A form appearing where there was none
        # is the corroboration; prefilled values raise confidence further,
        # because a prefilled form is evidence the record was really loaded.
        before_form_ids = {f.form_id for f in (before_model.forms or [])}
        new_forms = [f for f in (after_model.forms or []) if f.form_id not in before_form_ids]
        if not new_forms:
            return []
        edit_form = new_forms[0]

        evidence = [
            _evidence(
                "record_detail_control", "edit", page_url=before_model.url, element_id=executed_element_id
            ),
            _evidence("form_appeared", edit_form.form_id, page_url=after_model.url, element_id=edit_form.form_id),
        ]
        prefilled = any(
            (getattr(field_obj, "current_value", None) or "").strip()
            for field_obj in (edit_form.fields or [])
        )
        if prefilled:
            evidence.append(
                _evidence(
                    "field_prepopulation",
                    "edit form fields carry existing values",
                    page_url=after_model.url,
                    element_id=edit_form.form_id,
                )
            )

        return [
            CRUDWorkflowHypothesis(
                operation="edit",
                entity_hypothesis=entity,
                actor_hypothesis=current_actor_term,
                source_state="record_detail",
                target_state="edit_form_open",
                required_controls=[executed_element_id],
                required_form_id=edit_form.form_id,
                expected_transition=(
                    "record detail -> edit form (prefilled) -> save -> updated value visible"
                ),
                confidence=0.65 if prefilled else 0.5,
                safety_classification="controlled_write",
                cleanup_possibility="possible",
                evidence=evidence,
                source_page_url=before_model.url,
                status="supported",
            )
        ]
