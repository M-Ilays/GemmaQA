"""Table extraction — builds the (reused) `TableDescriptor` plus the richer
`TableColumnDescriptor`/`TableRowDescriptor` companions, including the direct
fix for docs/PAGE_PERCEPTION_AUDIT.md's "row actions are never linked to their
row" finding: `TableRowDescriptor.row_action_element_ids`.

Column data-type inference is a simple, deterministic heuristic over sample
cell values (numeric / date-like / text) — never a guess about business
meaning.
"""

from __future__ import annotations

import re

from app.schemas import TableColumnDescriptor, TableDescriptor, TableRowDescriptor

_NUMERIC_RE = re.compile(r"^[\s$€£]*-?[\d,]+(\.\d+)?%?\s*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}$")


def _infer_column_type(values: list[str]) -> str | None:
    sample = [v for v in values if v]
    if not sample:
        return None
    if all(_NUMERIC_RE.match(v.strip()) for v in sample):
        return "numeric"
    if all(_DATE_RE.match(v.strip()) for v in sample):
        return "date"
    return "text"


def extract_tables(raw) -> list[TableDescriptor]:  # raw: RawObservation
    tables: list[TableDescriptor] = []
    for table in raw.tables:
        dom_id = table.get("dom_id")
        if not dom_id:
            continue
        headers = list(table.get("headers") or [])
        raw_rows = table.get("rows") or []
        sample_rows = [list(r.get("cell_values") or []) for r in raw_rows[:3]]

        columns = [
            TableColumnDescriptor(
                stable_id=f"{dom_id}_col_{i}",
                column_index=i,
                header_text=header,
                accessible_name=header,
                data_type=_infer_column_type([r[i] for r in sample_rows if len(r) > i]),
                confidence=0.7 if headers else 0.4,
            )
            for i, header in enumerate(headers)
        ]

        rows = [
            TableRowDescriptor(
                stable_id=f"{dom_id}_row_{r.get('row_index', i)}",
                row_index=r.get("row_index", i),
                cell_values=list(r.get("cell_values") or []),
                row_action_element_ids=list(r.get("row_action_ids") or []),
                confidence=1.0,
            )
            for i, r in enumerate(raw_rows)
        ]

        tables.append(
            TableDescriptor(
                table_id=dom_id,
                headers=headers,
                row_count=int(table.get("row_count") or len(raw_rows)),
                sample_rows=sample_rows,
                stable_id=dom_id,
                source="dom",
                bounding_box=table.get("bounding_box"),
                confidence=1.0,
                columns=columns,
                rows=rows,
            )
        )
    return tables
