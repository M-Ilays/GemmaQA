"""Universal record-collection extraction — the direct answer to "detects no
tables on div-based data grids" from the CRUD surface discovery task.

Builds one `RecordCollection` per detected data set, from THREE raw sources
that `dom_extractor` already collected:

- `raw.tables`      — native `<table>` (kept for `TableDescriptor` backward
                       compatibility too; wrapped here as `collection_type
                       ="native_table"`).
- `raw.collections` — ARIA grid/treegrid (`role="grid"`/`"treegrid"` with
                       `role="row"` children) and div/card-based repeated-row
                       groups, both detected structurally in the single DOM
                       pass (see `dom_extractor.RAW_EXTRACTION_SCRIPT`).

Never keyed off a framework name: React/Vue/Angular all render the same
observable ARIA-role or repeated-sibling structure this module reads.
Global actions (an "Add"/"Create" toolbar button placed above the grid) and
search/filter controls are attached by POSITION relative to the collection's
bounding box — a structural signal, never a business word.
"""

from __future__ import annotations

import re
from typing import Any

from app.perception.models import CollectionAction, CollectionColumn, CollectionRow, RecordCollection

_SEARCH_HINTS = re.compile(r"search|lookup|find", re.IGNORECASE)
_FILTER_HINTS = re.compile(r"filter|status|category|type|show\s|view\s|sort\s", re.IGNORECASE)
_GLOBAL_ACTION_Y_MARGIN = 240.0
_SEARCH_CONTROL_Y_MARGIN = 160.0


def _box(el: dict[str, Any]) -> dict[str, float]:
    return el.get("bounding_box") or {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}


def _row_identity_hint(cells: list[str]) -> str | None:
    for cell in cells:
        if cell and cell.strip():
            return cell.strip()[:80]
    return None


def _rows_from_raw(raw_rows: list[dict[str, Any]], *, prefix: str, base_confidence: float) -> list[CollectionRow]:
    rows: list[CollectionRow] = []
    for idx, r in enumerate(raw_rows):
        cells = list(r.get("cell_values") or [])
        row_dom_id = r.get("dom_id") or f"{prefix}_row_{r.get('row_index', idx)}"
        rows.append(
            CollectionRow(
                stable_id=row_dom_id,
                element_id=r.get("dom_id"),
                row_index=int(r.get("row_index", idx)),
                is_activatable=bool(r.get("activatable", False)),
                activation_evidence=list(r.get("activation_evidence") or []),
                activation_basis=str(r.get("activation_basis") or "none"),
                cell_values=cells,
                identity_hint=_row_identity_hint(cells),
                row_action_ids=list(r.get("row_action_ids") or []),
                source="dom",
                confidence=base_confidence,
            )
        )
    return rows


def _row_actions(rows: list[CollectionRow], action_semantics_by_id: dict[str, str]) -> list[CollectionAction]:
    actions: list[CollectionAction] = []
    for row in rows:
        for action_id in row.row_action_ids:
            actions.append(
                CollectionAction(
                    stable_id=action_id,
                    element_id=action_id,
                    scope="row",
                    row_id=row.stable_id,
                    semantic_action=action_semantics_by_id.get(action_id),
                    source="dom",
                    confidence=0.7,
                )
            )
    return actions


def _global_actions(
    raw: Any, *, collection_box: dict[str, float], claimed_ids: set[str], action_semantics_by_id: dict[str, str]
) -> list[CollectionAction]:
    """A toolbar-style control (Add/Create/New/Export/...) placed ABOVE the
    collection and roughly within its horizontal span — position is the
    only signal used, never button text content, so this stays
    application-neutral."""
    global_actions: list[CollectionAction] = []
    top = float(collection_box.get("y", 0) or 0)
    left = float(collection_box.get("x", 0) or 0)
    width = float(collection_box.get("width", 0) or 0)
    for el in raw.elements:
        dom_id = el.get("dom_id")
        if not dom_id or dom_id in claimed_ids:
            continue
        category = el.get("category")
        if category not in {"button", "link"}:
            continue
        if not el.get("is_visible", True):
            continue
        box = _box(el)
        ey = float(box.get("y", 0) or 0)
        ex = float(box.get("x", 0) or 0)
        if ey > top or ey < top - _GLOBAL_ACTION_Y_MARGIN:
            continue
        if width and not (left - 40 <= ex <= left + width + 40):
            continue
        global_actions.append(
            CollectionAction(
                stable_id=dom_id,
                element_id=dom_id,
                scope="global",
                semantic_action=action_semantics_by_id.get(dom_id),
                source="heuristic",
                confidence=0.5,
            )
        )
    return global_actions


def _search_and_filter_controls(raw: Any, *, collection_box: dict[str, float]) -> tuple[list[str], list[str]]:
    top = float(collection_box.get("y", 0) or 0)
    search_ids: list[str] = []
    filter_ids: list[str] = []
    for el in raw.elements:
        dom_id = el.get("dom_id")
        if not dom_id or el.get("category") not in {"input", "select"}:
            continue
        box = _box(el)
        ey = float(box.get("y", 0) or 0)
        if ey > top or ey < top - _SEARCH_CONTROL_Y_MARGIN:
            continue
        haystack = " ".join(
            filter(None, [el.get("accessible_name"), el.get("placeholder"), el.get("name"), el.get("aria_label")])
        )
        if _SEARCH_HINTS.search(haystack):
            search_ids.append(dom_id)
        elif el.get("category") == "select" and _FILTER_HINTS.search(haystack):
            filter_ids.append(dom_id)
    return search_ids, filter_ids


def extract_collections(raw: Any, action_semantics_by_id: dict[str, str] | None = None) -> list[RecordCollection]:
    """`raw`: `dom_extractor.RawObservation`. `action_semantics_by_id`: an
    optional `{element_id: semantic_action}` map (from `action_semantics.py`)
    — when supplied, row/global actions carry their classified verb; when
    omitted, actions are still represented, just without a resolved verb."""
    semantics = action_semantics_by_id or {}
    collections: list[RecordCollection] = []
    claimed_action_ids: set[str] = set()

    for table in raw.tables:
        dom_id = table.get("dom_id")
        if not dom_id:
            continue
        headers = list(table.get("headers") or [])
        raw_rows = table.get("rows") or []
        rows = _rows_from_raw(raw_rows, prefix=dom_id, base_confidence=0.9 if headers else 0.6)
        row_action_ids = {a for row in rows for a in row.row_action_ids}
        claimed_action_ids |= row_action_ids
        box = table.get("bounding_box") or {}
        search_ids, filter_ids = _search_and_filter_controls(raw, collection_box=box)
        row_actions = _row_actions(rows, semantics)
        global_actions = _global_actions(
            raw, collection_box=box, claimed_ids=claimed_action_ids | {dom_id}, action_semantics_by_id=semantics
        )
        claimed_action_ids |= {a.element_id for a in global_actions if a.element_id}
        collections.append(
            RecordCollection(
                stable_id=dom_id,
                element_id=dom_id,
                collection_type="native_table",
                columns=[
                    CollectionColumn(stable_id=f"{dom_id}_col_{i}", column_index=i, header_text=h, confidence=0.8)
                    for i, h in enumerate(headers)
                ],
                row_count=int(table.get("row_count") or len(raw_rows)),
                visible_rows=rows,
                row_actions=row_actions,
                global_actions=global_actions,
                has_search_control=bool(search_ids),
                search_control_ids=search_ids,
                has_filter_controls=bool(filter_ids),
                filter_control_ids=filter_ids,
                bounding_box=box,
                structural_signals=["native_table_tag"],
                source="dom",
                confidence=0.9,
            )
        )

    for coll in raw.collections:
        dom_id = coll.get("dom_id")
        if not dom_id:
            continue
        headers = list(coll.get("headers") or [])
        raw_rows = coll.get("rows") or []
        collection_type = coll.get("collection_type") or "unknown_collection"
        base_confidence = 0.75 if collection_type in {"aria_grid", "aria_treegrid"} else 0.55
        rows = _rows_from_raw(raw_rows, prefix=dom_id, base_confidence=base_confidence if headers else base_confidence - 0.1)
        row_action_ids = {a for row in rows for a in row.row_action_ids}
        claimed_action_ids |= row_action_ids
        box = coll.get("bounding_box") or {}
        search_ids, filter_ids = _search_and_filter_controls(raw, collection_box=box)
        row_actions = _row_actions(rows, semantics)
        global_actions = _global_actions(
            raw, collection_box=box, claimed_ids=claimed_action_ids | {dom_id}, action_semantics_by_id=semantics
        )
        claimed_action_ids |= {a.element_id for a in global_actions if a.element_id}
        collections.append(
            RecordCollection(
                stable_id=dom_id,
                element_id=dom_id,
                collection_type=collection_type,
                columns=[
                    CollectionColumn(stable_id=f"{dom_id}_col_{i}", column_index=i, header_text=h, confidence=base_confidence)
                    for i, h in enumerate(headers)
                ],
                row_count=int(coll.get("row_count") or len(raw_rows)),
                visible_rows=rows,
                row_actions=row_actions,
                global_actions=global_actions,
                has_search_control=bool(search_ids),
                search_control_ids=search_ids,
                has_filter_controls=bool(filter_ids),
                filter_control_ids=filter_ids,
                bounding_box=box,
                structural_signals=list(coll.get("structural_signals") or []),
                source="heuristic" if collection_type in {"div_row_group", "card_group"} else "dom",
                confidence=base_confidence,
            )
        )

    return collections
