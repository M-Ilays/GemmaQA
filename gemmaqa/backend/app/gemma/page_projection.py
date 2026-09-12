"""Bounded, model-facing projection of the observed page.

The audit (docs/MODEL_CONTEXT_AUDIT.md, J-4) found that `CanonicalPageModel` —
the Universal Page Perception output carrying accessibility roles, collections,
form intents, action semantics, visual evidence, and unknown components — was
stored on `RunMemory` and consumed by eleven engines, while every prompt still
saw the much thinner legacy `PageState`. This module is the missing path.

Two rules shape it:

* **Never blindly serialize the canonical model.** It is a superset view built
  for engines, and engines can afford detail a prompt cannot. Everything here is
  a bounded projection with explicit caps, chosen so a 500-control page and a
  5-control page both produce a prompt of predictable size.
* **Always say where the picture came from.** `source` is one of `canonical`,
  `legacy_fallback`, or `mixed`, so a reader of the report can tell whether the
  model reasoned over rich perception or the legacy fallback. Silently degrading
  from one to the other is precisely the kind of invisible behaviour change the
  audit was written to prevent.

Controls are deduplicated across the two sources by `element_id`: the canonical
model and `PageState` describe overlapping sets, and sending the same control
twice both wastes budget and invites the model to treat one control as two.
"""

from __future__ import annotations

from typing import Any

from app.gemma.context_sanitizer import sanitize_page_state_for_model
from app.utils.logging import get_logger
from app.utils.sanitization import sanitize_text, sanitize_url

logger = get_logger("gemma.page_projection")

PAGE_SOURCE_CANONICAL = "canonical"
PAGE_SOURCE_LEGACY = "legacy_fallback"
PAGE_SOURCE_MIXED = "mixed"

# Projection caps. Deliberately the same order of magnitude as the legacy
# projection's caps so switching source does not silently change prompt size.
MAX_CONTROLS = 60
MAX_REGIONS = 12
MAX_HEADINGS = 12
MAX_FORMS = 8
MAX_FIELDS_PER_FORM = 20
MAX_COLLECTIONS = 5
MAX_COLLECTION_COLUMNS = 12
MAX_DIALOGS = 5
MAX_NAV_ITEMS = 20
MAX_UNKNOWN_COMPONENTS = 8
MAX_VISUAL_EVIDENCE = 10
MAX_ALERTS = 10
MAX_TEXT_CHARS = 500


def _confidence(obj: Any) -> float | None:
    score = getattr(obj, "confidence", None)
    value = getattr(score, "value", score)
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _label(obj: Any) -> str:
    for attr in ("accessible_name", "text", "label"):
        value = getattr(obj, attr, None)
        if value:
            return sanitize_text(str(value))[:120]
    return ""


def _control_from_canonical(el: Any) -> dict[str, Any]:
    """One interactive control, described semantically rather than structurally.

    No selector, no class name, no DOM path: the model chooses by `element_id`
    and the runtime resolves it. Keeping selectors out of model context is what
    stops a hallucinated selector from ever reaching the browser.
    """
    data: dict[str, Any] = {
        "element_id": getattr(el, "element_id", None) or getattr(el, "stable_id", None),
        "role": getattr(el, "aria_role", None) or getattr(el, "role", None),
        "tag": getattr(el, "dom_tag", None) or getattr(el, "tag", None),
        "accessible_name": _label(el),
        "input_type": getattr(el, "input_type", None) or getattr(el, "type", None),
        "enabled": bool(getattr(el, "is_enabled", True)),
        "visible": bool(getattr(el, "is_visible", True)),
    }
    for attr, key in (
        ("is_required", "required"),
        ("is_expanded", "expanded"),
        ("is_checked", "checked"),
        ("is_selected", "selected"),
    ):
        value = getattr(el, attr, None)
        if value is not None:
            data[key] = value
    href = getattr(el, "url", None) or getattr(el, "href", None)
    if href:
        data["href"] = sanitize_url(str(href))
    confidence = _confidence(el)
    if confidence is not None and confidence < 1.0:
        data["confidence"] = confidence
    return {k: v for k, v in data.items() if v not in (None, "")}


def _semantic_actions(model: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in getattr(model, "action_semantics", None) or []:
        element_id = getattr(item, "element_id", None)
        if not element_id:
            continue
        entry: dict[str, Any] = {"semantic_action": getattr(item, "semantic_action", "unknown")}
        confidence = _confidence(item)
        if confidence is not None:
            entry["semantic_action_confidence"] = confidence
        out[str(element_id)] = entry
    return out


def _form_projection(form: Any, intents: dict[str, dict[str, Any]]) -> dict[str, Any]:
    form_id = getattr(form, "form_id", None)
    fields = []
    for field_obj in (getattr(form, "fields", None) or [])[:MAX_FIELDS_PER_FORM]:
        entry = {
            "element_id": getattr(field_obj, "element_id", None),
            "name": getattr(field_obj, "name", None),
            "label": _label(field_obj) or getattr(field_obj, "label", None),
            "field_type": getattr(field_obj, "field_type", None),
            "required": bool(getattr(field_obj, "required", False) or getattr(field_obj, "is_required", False)),
            "disabled": bool(getattr(field_obj, "disabled", False)),
        }
        # Values are never forwarded — see context_sanitizer; a field's CONTENT
        # is exactly where credentials live.
        fields.append({k: v for k, v in entry.items() if v not in (None, "", False)} or {"name": "unnamed"})
    projection: dict[str, Any] = {
        "form_id": form_id,
        "fields": fields,
        "submit_element_id": getattr(form, "submit_element_id", None),
    }
    action = getattr(form, "action", None)
    if action:
        projection["action"] = sanitize_url(str(action))
    intent = intents.get(str(form_id or ""))
    if intent:
        projection.update(intent)
    return {k: v for k, v in projection.items() if v not in (None, "", [])}


def _form_intents(model: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in getattr(model, "form_intents", None) or []:
        form_id = getattr(item, "form_id", None)
        if not form_id:
            continue
        entry: dict[str, Any] = {"intent": getattr(item, "intent", "unknown")}
        confidence = _confidence(item)
        if confidence is not None:
            entry["intent_confidence"] = confidence
        target = getattr(item, "target_entity_hypothesis", None)
        if target:
            entry["target_entity_hypothesis"] = str(target)[:80]
        out[str(form_id)] = entry
    return out


def _collection_projection(collection: Any) -> dict[str, Any]:
    return {
        k: v
        for k, v in {
            "collection_id": getattr(collection, "stable_id", None) or getattr(collection, "element_id", None),
            "collection_type": getattr(collection, "collection_type", None),
            "columns": [
                str(getattr(c, "header_text", "") or f"column_{getattr(c, 'column_index', 0)}")[:60]
                for c in (getattr(collection, "columns", None) or [])[:MAX_COLLECTION_COLUMNS]
            ],
            "row_count": getattr(collection, "row_count", 0),
            "row_action_ids": [
                getattr(a, "element_id", None)
                for a in (getattr(collection, "row_actions", None) or [])[:8]
                if getattr(a, "element_id", None)
            ],
            "global_action_ids": [
                getattr(a, "element_id", None)
                for a in (getattr(collection, "global_actions", None) or [])[:8]
                if getattr(a, "element_id", None)
            ],
            "has_pagination": bool(getattr(collection, "has_pagination", False)),
            "has_search_control": bool(getattr(collection, "has_search_control", False)),
            "has_filter_controls": bool(getattr(collection, "has_filter_controls", False)),
        }.items()
        if v not in (None, "", [], False, 0)
    }


def project_canonical_page(model: Any, *, max_controls: int = MAX_CONTROLS) -> dict[str, Any]:
    """Bounded projection of a `CanonicalPageModel`."""
    intents = _form_intents(model)
    semantics = _semantic_actions(model)

    controls: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for el in (getattr(model, "interactive_elements", None) or [])[: max_controls * 2]:
        control = _control_from_canonical(el)
        element_id = str(control.get("element_id") or "")
        if not element_id or element_id in seen_ids:
            continue
        seen_ids.add(element_id)
        control.update(semantics.get(element_id, {}))
        controls.append(control)
        if len(controls) >= max_controls:
            break

    projection: dict[str, Any] = {
        "source": PAGE_SOURCE_CANONICAL,
        "url": sanitize_url(str(getattr(model, "url", "") or "")),
        "title": sanitize_text(str(getattr(model, "title", "") or ""))[:200],
        "state_fingerprint": getattr(model, "state_fingerprint", "") or "",
        "observation_version": str(getattr(model, "page_id", "") or ""),
        "headings": [
            _label(h) for h in (getattr(model, "headings", None) or [])[:MAX_HEADINGS] if _label(h)
        ],
        "semantic_regions": [
            {
                "region_type": getattr(r, "region_type", "unknown"),
                "landmark_role": getattr(r, "landmark_role", None),
                "label": _label(r),
            }
            for r in (getattr(model, "regions", None) or [])[:MAX_REGIONS]
        ],
        "navigation": [
            {"label": _label(item), "element_id": getattr(item, "element_id", None)}
            for region in (getattr(model, "navigation_regions", None) or [])[:3]
            for item in (getattr(region, "items", None) or [])[:MAX_NAV_ITEMS]
            if _label(item)
        ][:MAX_NAV_ITEMS],
        "forms": [_form_projection(f, intents) for f in (getattr(model, "forms", None) or [])[:MAX_FORMS]],
        "collections": [
            _collection_projection(c) for c in (getattr(model, "collections", None) or [])[:MAX_COLLECTIONS]
        ],
        "dialogs": [
            {
                "dialog_type": getattr(d, "dialog_type", "dialog"),
                "label": _label(d),
                "is_open": bool(getattr(d, "is_open", True)),
            }
            for d in (getattr(model, "dialogs", None) or [])[:MAX_DIALOGS]
        ],
        "alerts": [
            {"severity": getattr(a, "severity", "info"), "text": _label(a)}
            for a in (getattr(model, "alerts", None) or [])[:MAX_ALERTS]
            if _label(a)
        ],
        "controls": controls,
        "unknown_regions": [
            {
                "element_id": getattr(u, "element_id", None) or getattr(u, "stable_id", None),
                "detection_reason": str(getattr(u, "detection_reason", "") or "")[:120],
            }
            for u in (getattr(model, "unknown_components", None) or [])[:MAX_UNKNOWN_COMPONENTS]
        ],
        "visual_evidence": [
            {
                "element_id": getattr(v, "element_id", ""),
                "visual_semantic_type": getattr(v, "visual_semantic_type", "unknown"),
                "description": sanitize_text(str(getattr(v, "description", "") or ""))[:160],
                "confidence": _confidence(v),
            }
            for v in (getattr(model, "visual_evidence", None) or [])[:MAX_VISUAL_EVIDENCE]
        ],
        "network_failures": [
            {
                "status": getattr(n, "http_status", None),
                "method": getattr(n, "method", None),
                "summary": sanitize_text(str(getattr(n, "failure_text", "") or ""))[:160],
            }
            for n in (getattr(model, "network_evidence", None) or [])
            if getattr(n, "failed", False)
        ][:10],
    }
    return {k: v for k, v in projection.items() if v not in (None, "", [])}


def project_legacy_page(page_state: Any) -> dict[str, Any]:
    """Legacy projection — the existing, already-hardened sanitizer, unchanged.

    Reused rather than reimplemented: `sanitize_page_state_for_model` is where
    every credential-masking rule lives, and a second copy of those rules is a
    second place for them to drift.
    """
    projection = dict(sanitize_page_state_for_model(page_state))
    projection["source"] = PAGE_SOURCE_LEGACY
    # Normalize the key the rest of the pipeline reads, without losing the
    # original for callers that still expect it.
    projection["controls"] = projection.get("interactive_elements") or []
    return projection


def project_page(
    *,
    page_state: Any,
    canonical_model: Any = None,
    max_controls: int = MAX_CONTROLS,
) -> dict[str, Any]:
    """Project the page for the model, preferring canonical perception.

    Falls back to the legacy `PageState` projection when no canonical model is
    available, or when the canonical model describes a DIFFERENT page than the
    one being reasoned about — a stale model is worse than no model, because it
    is confidently wrong about which controls exist.
    """
    if canonical_model is None:
        return project_legacy_page(page_state)

    canonical_url = str(getattr(canonical_model, "url", "") or "")
    state_url = str(getattr(page_state, "url", "") or "")
    if canonical_url and state_url and canonical_url != state_url:
        logger.debug("Canonical model is for a different URL; using legacy page projection")
        projection = project_legacy_page(page_state)
        projection["canonical_rejected_reason"] = "url_mismatch"
        return projection

    try:
        projection = project_canonical_page(canonical_model, max_controls=max_controls)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Canonical page projection failed (%s); using legacy", type(exc).__name__)
        projection = project_legacy_page(page_state)
        projection["canonical_rejected_reason"] = type(exc).__name__
        return projection

    # Console errors have no canonical equivalent, so they are always taken from
    # PageState. Taking anything from the legacy source makes this `mixed`.
    legacy_console = [sanitize_text(e) for e in (getattr(page_state, "console_errors", None) or [])[:10]]
    if legacy_console:
        projection["console_errors"] = legacy_console
        projection["source"] = PAGE_SOURCE_MIXED

    if not projection.get("network_failures"):
        legacy_network = [sanitize_text(e) for e in (getattr(page_state, "network_failures", None) or [])[:10]]
        if legacy_network:
            projection["network_failures"] = legacy_network
            projection["source"] = PAGE_SOURCE_MIXED

    # Legacy controls that canonical perception did not surface are additive,
    # never duplicative — keyed by element_id so a control present in both
    # appears exactly once, described by the richer canonical source.
    if len(projection.get("controls") or []) < max_controls:
        known = {str(c.get("element_id")) for c in projection.get("controls") or []}
        extra: list[dict[str, Any]] = []
        for el in (getattr(page_state, "interactive_elements", None) or []):
            element_id = str(getattr(el, "element_id", "") or "")
            if not element_id or element_id in known:
                continue
            known.add(element_id)
            extra.append(_control_from_canonical(el))
            if len(projection.get("controls") or []) + len(extra) >= max_controls:
                break
        if extra:
            projection["controls"] = list(projection.get("controls") or []) + extra
            projection["source"] = PAGE_SOURCE_MIXED

    return projection


def control_element_ids(projection: dict[str, Any]) -> set[str]:
    """Every element_id the model is permitted to reference from this projection."""
    ids: set[str] = set()
    for control in projection.get("controls") or []:
        element_id = control.get("element_id")
        if element_id:
            ids.add(str(element_id))
    for form in projection.get("forms") or []:
        if form.get("form_id"):
            ids.add(str(form["form_id"]))
        if form.get("submit_element_id"):
            ids.add(str(form["submit_element_id"]))
        for field_obj in form.get("fields") or []:
            if field_obj.get("element_id"):
                ids.add(str(field_obj["element_id"]))
    for collection in projection.get("collections") or []:
        if collection.get("collection_id"):
            ids.add(str(collection["collection_id"]))
        for key in ("row_action_ids", "global_action_ids"):
            for element_id in collection.get(key) or []:
                ids.add(str(element_id))
    for table in projection.get("tables") or []:
        if isinstance(table, dict) and table.get("table_id"):
            ids.add(str(table["table_id"]))
    for item in projection.get("navigation") or []:
        if isinstance(item, dict) and item.get("element_id"):
            ids.add(str(item["element_id"]))
    return ids
