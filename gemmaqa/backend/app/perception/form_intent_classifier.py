"""Form intent classification — WHAT a form is FOR, never assumed from "it
has input elements" alone (the task's explicit anti-pattern: a search/filter
form on a list page must not be labelled `create`).

Evidence considered, all application-neutral (never a business word):

- field types present (password -> authentication signal; file -> upload;
  checkbox-ratio -> bulk action)
- how many fields are already filled ("mostly filled" -> edit-shaped,
  "mostly empty" -> create-shaped)
- field count (a 1-3 field form is structurally different from a 12-field
  form regardless of what it's for)
- whether the form sits inside a dialog (`CanonicalPageModel.dialogs`)
- the submit control's resolved action-verb (from `action_semantics.py`,
  passed in — never recomputed here)
- page heading / URL path segments (generic UI words only: "search",
  "filter", "settings", "login", "sign in" — never an entity name)
- whether a record collection exists on the same page (a small form next to
  a collection, with search/filter-shaped fields, is far more likely a
  search/filter form than a create form)
"""

from __future__ import annotations

import re
from typing import Any

from app.perception.models import FormIntentAlternative, FormIntentClassification

_SEARCH_WORD = re.compile(r"search|lookup|find", re.IGNORECASE)
_FILTER_WORD = re.compile(r"filter|status|category|sort\s|show\s", re.IGNORECASE)
_SETTINGS_WORD = re.compile(r"settings|preferences|configuration|options", re.IGNORECASE)
_AUTH_WORD = re.compile(r"log\s?in|sign\s?in|sign\s?up|register|password reset|forgot password", re.IGNORECASE)
_CONFIRM_WORD = re.compile(r"are you sure|please confirm|cannot be undone|permanently", re.IGNORECASE)

_MIN_CONFIDENCE_FOR_ALTERNATIVE = 0.15


def _field_summary(form: Any) -> dict[str, Any]:
    fields = list(getattr(form, "field_descriptors", None) or [])
    if not fields:
        fields = [
            type("_F", (), {
                "field_type": f.field_type, "current_value": f.current_value,
                "label": f.label, "element_id": f.element_id,
                "accessible_name": None, "placeholder": f.placeholder,
            })()
            for f in (getattr(form, "fields", None) or [])
        ]
    total = len(fields)
    filled = sum(1 for f in fields if (getattr(f, "current_value", None) or "").strip())
    password_count = sum(1 for f in fields if (getattr(f, "field_type", "") or "").lower() == "password")
    file_count = sum(1 for f in fields if (getattr(f, "field_type", "") or "").lower() == "file")
    checkbox_count = sum(1 for f in fields if (getattr(f, "field_type", "") or "").lower() == "checkbox")
    label_bits: list[str] = []
    for f in fields:
        label_bits.extend(
            filter(
                None,
                [
                    getattr(f, "label", None),
                    getattr(f, "accessible_name", None),
                    getattr(f, "placeholder", None),
                ],
            )
        )
    labels = " ".join(label_bits)
    return {
        "total": total,
        "filled": filled,
        "filled_ratio": (filled / total) if total else 0.0,
        "password_count": password_count,
        "file_count": file_count,
        "checkbox_ratio": (checkbox_count / total) if total else 0.0,
        "labels": labels,
    }


def _form_in_dialog(form: Any, dialogs: list[Any]) -> Any | None:
    field_ids = {f.element_id for f in (getattr(form, "field_descriptors", None) or []) if f.element_id}
    field_ids.add(form.form_id)
    for dialog in dialogs:
        contained = set(dialog.contained_element_ids or [])
        if field_ids & contained:
            return dialog
    return None


def _submit_action_verb(form: Any, action_semantics_by_id: dict[str, str]) -> str | None:
    if not form.submit_element_id:
        return None
    return action_semantics_by_id.get(form.submit_element_id)


def _score_candidates(
    *,
    summary: dict[str, Any],
    dialog: Any | None,
    submit_verb: str | None,
    heading_and_url: str,
    has_collection_on_page: bool,
) -> dict[str, tuple[float, list[str]]]:
    scores: dict[str, tuple[float, list[str]]] = {}

    def add(intent: str, confidence: float, reason: str) -> None:
        prev = scores.get(intent)
        if prev is None:
            scores[intent] = (confidence, [reason])
        else:
            scores[intent] = (max(prev[0], confidence), [*prev[1], reason])

    if summary["password_count"] > 0 and summary["total"] <= 4:
        add("authentication", 0.85, "form has a password field and few total fields")
    if _AUTH_WORD.search(heading_and_url):
        add("authentication", 0.6, "heading/URL matches an authentication term")

    if dialog is not None and summary["total"] <= 1:
        if submit_verb in {"delete", "remove", "confirm"} or _CONFIRM_WORD.search(dialog.text or ""):
            add("delete_confirmation", 0.8, "near-empty form inside a dialog with a delete/confirm verb or confirmation text")

    if summary["file_count"] > 0:
        add("upload", 0.75, "form has a file-type field")

    if summary["total"] >= 2 and summary["checkbox_ratio"] > 0.5:
        add("bulk_action", 0.55, "majority of fields are checkboxes (bulk selection shape)")

    if summary["total"] and summary["total"] <= 4:
        if _SEARCH_WORD.search(summary["labels"]) or submit_verb == "search":
            add("search", 0.65, "small field count with a search-shaped label or submit verb")
        if _FILTER_WORD.search(summary["labels"]) or submit_verb == "filter":
            add("filter", 0.6, "small field count with a filter-shaped label or submit verb")
        if has_collection_on_page and not scores.get("search") and not scores.get("filter"):
            add("search", 0.35, "small form co-located with a record collection on the same page")

    if _SETTINGS_WORD.search(heading_and_url) and summary["total"] > 0:
        # A specific URL/heading signal outranks the generic "mostly
        # filled -> edit" fallback below (edit tops out at 0.6) — settings
        # pages are structurally edit-shaped (prefilled fields) but the
        # explicit vocabulary signal is stronger evidence than field-fill
        # ratio alone.
        add("settings", 0.65, "heading/URL matches a settings/preferences/configuration term")

    if summary["total"] > 0:
        if summary["filled_ratio"] >= 0.5:
            add("edit", 0.6, "most fields already carry a value (edit-shaped)")
        else:
            add("create", 0.5, "most fields are empty (create-shaped)")

    if not scores:
        add("unknown", 0.2, "no structural evidence available")

    return scores


def classify_form_intents(model: Any, action_semantics_by_id: dict[str, str] | None = None) -> list[FormIntentClassification]:
    """`model`: `CanonicalPageModel`. Returns one `FormIntentClassification`
    per form on the page."""
    semantics = action_semantics_by_id or {}
    heading_and_url = " ".join(
        filter(None, [model.url, model.title, *(h.text or "" for h in (model.headings or [])[:2])])
    )
    has_collection_on_page = bool(model.collections)
    dialogs = list(model.dialogs or [])

    out: list[FormIntentClassification] = []
    for form in model.forms or []:
        summary = _field_summary(form)
        dialog = _form_in_dialog(form, dialogs)
        submit_verb = _submit_action_verb(form, semantics)
        scores = _score_candidates(
            summary=summary, dialog=dialog, submit_verb=submit_verb,
            heading_and_url=heading_and_url, has_collection_on_page=has_collection_on_page,
        )
        ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
        best_intent, (best_confidence, best_reasons) = ranked[0]
        alternatives = [
            FormIntentAlternative(intent=intent, confidence=conf, reason="; ".join(reasons))
            for intent, (conf, reasons) in ranked[1:]
            if conf >= _MIN_CONFIDENCE_FOR_ALTERNATIVE
        ]
        out.append(
            FormIntentClassification(
                form_id=form.form_id,
                intent=best_intent,
                confidence=best_confidence,
                target_entity_hypothesis=(model.headings[0].text if model.headings else None),
                operation_hypothesis=submit_verb,
                evidence=list(best_reasons),
                alternatives=alternatives,
            )
        )
    return out
