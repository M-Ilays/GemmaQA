"""Deterministic before/after CanonicalPageModel comparison.

Pure structural diffing — no LLM, no browser access, no business
vocabulary. Every signal is a fixed rule over two already-observed
`CanonicalPageModel` snapshots (before an action, after it). State LABELS
are taken from the application's own observed text when one exists (a
status badge, a dropdown's current value); when no explicit label exists, a
structural placeholder is synthesized and marked `is_explicit=False` with
correspondingly lower confidence — never asserted as if it were a real
application label.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.intelligence.actor_discovery.actor_candidate_builder import ASSIGNMENT_HINTS, ROLE_FIELD_HINTS

# "Owner"/"Owned by" columns are ownership-transfer evidence, structurally
# identical to an assignment column — extends (never replaces)
# actor_discovery's own ASSIGNMENT_HINTS for this module's use.
_OWNERSHIP_HINTS = ASSIGNMENT_HINTS + ("owner", "owned by", "ownership")

if TYPE_CHECKING:
    from app.perception.models import CanonicalPageModel

_STATUS_COLUMN_HEADS = frozenset({"status", "state", "stage"})
_NUMERIC_TOKEN_RE = re.compile(r"\b\d[\d,]*\b")


@dataclass
class TransitionSignal:
    kind: str
    description: str = ""
    evidence_text: str = ""
    confidence: float = 0.5
    is_explicit: bool = False
    # Populated only for row-identity-based signals (status/ownership/
    # assignment change, row appeared/disappeared).
    table_id: str | None = None
    row_identity: str | None = None
    before_value: str | None = None
    after_value: str | None = None


@dataclass
class DetectedTransitions:
    url_changed: bool = False
    fingerprint_changed: bool = False
    tab_changed: bool = False
    dialog_opened: bool = False
    dialog_closed: bool = False
    form_count_changed: bool = False
    signals: list[TransitionSignal] = field(default_factory=list)

    def any_detected(self) -> bool:
        return bool(
            self.url_changed
            or self.fingerprint_changed
            or self.tab_changed
            or self.dialog_opened
            or self.dialog_closed
            or self.form_count_changed
            or self.signals
        )


def _hint_match(text: str, hints: tuple[str, ...]) -> bool:
    text = (text or "").lower()
    return any(h in text for h in hints)


def _row_identity(row_cells: list[str], skip_index: int | None = None) -> str:
    for i, cell in enumerate(row_cells):
        if i == skip_index:
            continue
        if cell and cell.strip():
            return cell.strip()
    return ""


class StateTransitionDetector:
    """Stateless: `detect()` takes two independent snapshots and returns
    every structural difference found. Callers (workflow_step_extractor,
    workflow_reconstructor) interpret WHICH differences matter for a
    specific step/transition."""

    def detect(self, before: "CanonicalPageModel", after: "CanonicalPageModel") -> DetectedTransitions:
        result = DetectedTransitions()
        result.url_changed = before.url != after.url
        result.fingerprint_changed = (before.state_fingerprint or "") != (after.state_fingerprint or "")

        self._detect_tab_change(before, after, result)
        self._detect_dialog_change(before, after, result)
        self._detect_form_progression(before, after, result)
        self._detect_table_row_transitions(before, after, result)
        self._detect_badge_or_counter_change(before, after, result)
        self._detect_notification_change(before, after, result)
        self._detect_enabled_actions_change(before, after, result)
        self._detect_network_resource_change(before, after, result)
        self._detect_success_or_failure_feedback(before, after, result)
        return result

    # -- tabs / dialogs -------------------------------------------------------

    @staticmethod
    def _detect_tab_change(before, after, result: DetectedTransitions) -> None:
        before_selected = {tg.selected_tab_id for tg in (before.tabs or []) if tg.selected_tab_id}
        after_selected = {tg.selected_tab_id for tg in (after.tabs or []) if tg.selected_tab_id}
        if before_selected != after_selected:
            result.tab_changed = True
            result.signals.append(
                TransitionSignal(
                    kind="tab_changed", description="selected tab changed",
                    evidence_text=f"{sorted(before_selected)} -> {sorted(after_selected)}",
                    confidence=0.7, is_explicit=True,
                )
            )

    @staticmethod
    def _detect_dialog_change(before, after, result: DetectedTransitions) -> None:
        before_ids = {d.element_id or d.stable_id for d in (before.dialogs or [])}
        after_ids = {d.element_id or d.stable_id for d in (after.dialogs or [])}
        if after_ids - before_ids:
            result.dialog_opened = True
            result.signals.append(
                TransitionSignal(kind="dialog_opened", description="a new dialog appeared", confidence=0.75, is_explicit=True)
            )
        if before_ids - after_ids:
            result.dialog_closed = True
            result.signals.append(
                TransitionSignal(kind="dialog_closed", description="a dialog closed", confidence=0.65, is_explicit=True)
            )

    # -- forms ----------------------------------------------------------------

    @staticmethod
    def _detect_form_progression(before, after, result: DetectedTransitions) -> None:
        before_ids = {f.form_id for f in (before.forms or [])}
        after_ids = {f.form_id for f in (after.forms or [])}
        if before_ids != after_ids:
            result.form_count_changed = True
            result.signals.append(
                TransitionSignal(
                    kind="form_progression",
                    description="the set of forms on the page changed (multi-step form or submission)",
                    confidence=0.6, is_explicit=True,
                )
            )

    # -- tables: row appear/disappear + status/ownership/assignment change ---

    def _detect_table_row_transitions(self, before, after, result: DetectedTransitions) -> None:
        before_by_id = {t.table_id: t for t in (before.tables or []) if t.table_id}
        after_by_id = {t.table_id: t for t in (after.tables or []) if t.table_id}
        for table_id, after_table in after_by_id.items():
            before_table = before_by_id.get(table_id)
            if before_table is None:
                continue
            if (before_table.row_count or 0) != (after_table.row_count or 0):
                result.signals.append(
                    TransitionSignal(
                        kind="table_count_changed", table_id=table_id,
                        description=f"table row count changed ({before_table.row_count} -> {after_table.row_count})",
                        confidence=0.6, is_explicit=True,
                    )
                )
            headers = [h or "" for h in (after_table.headers or [])]
            status_idx = next((i for i, h in enumerate(headers) if h.lower() in _STATUS_COLUMN_HEADS), None)
            assign_idx = next(
                (i for i, h in enumerate(headers) if _hint_match(h, _OWNERSHIP_HINTS) or _hint_match(h, ROLE_FIELD_HINTS)),
                None,
            )
            before_rows = {
                _row_identity(list(r), skip_index=status_idx): list(r) for r in (before_table.sample_rows or [])
            }
            after_rows = {
                _row_identity(list(r), skip_index=status_idx): list(r) for r in (after_table.sample_rows or [])
            }
            new_ids = set(after_rows) - set(before_rows)
            gone_ids = set(before_rows) - set(after_rows)
            for identity in new_ids:
                if not identity:
                    continue
                result.signals.append(
                    TransitionSignal(
                        kind="row_appeared", table_id=table_id, row_identity=identity,
                        description=f"a new row appeared: {identity}", confidence=0.55, is_explicit=True,
                    )
                )
            for identity in gone_ids:
                if not identity:
                    continue
                result.signals.append(
                    TransitionSignal(
                        kind="row_disappeared", table_id=table_id, row_identity=identity,
                        description=f"a row disappeared: {identity}", confidence=0.55, is_explicit=True,
                    )
                )
            for identity in set(before_rows) & set(after_rows):
                if not identity:
                    continue
                b_row, a_row = before_rows[identity], after_rows[identity]
                if status_idx is not None and status_idx < len(b_row) and status_idx < len(a_row):
                    if b_row[status_idx] != a_row[status_idx]:
                        result.signals.append(
                            TransitionSignal(
                                kind="entity_status_changed", table_id=table_id, row_identity=identity,
                                before_value=b_row[status_idx], after_value=a_row[status_idx],
                                description=f"{identity}: status {b_row[status_idx]!r} -> {a_row[status_idx]!r}",
                                confidence=0.75, is_explicit=True,
                            )
                        )
                if assign_idx is not None and assign_idx < len(b_row) and assign_idx < len(a_row):
                    if b_row[assign_idx] != a_row[assign_idx]:
                        result.signals.append(
                            TransitionSignal(
                                kind="assignment_changed", table_id=table_id, row_identity=identity,
                                before_value=b_row[assign_idx], after_value=a_row[assign_idx],
                                description=f"{identity}: assignment {b_row[assign_idx]!r} -> {a_row[assign_idx]!r}",
                                confidence=0.7, is_explicit=True,
                            )
                        )

    # -- badges / counters ----------------------------------------------------

    @staticmethod
    def _detect_badge_or_counter_change(before, after, result: DetectedTransitions) -> None:
        def short_numeric_texts(model) -> dict[str, str]:
            out: dict[str, str] = {}
            for h in (model.headings or []):
                text = (h.text or "").strip()
                if text and len(text) <= 40 and _NUMERIC_TOKEN_RE.search(text):
                    out[h.stable_id or h.element_id or text] = text
            return out

        before_map, after_map = short_numeric_texts(before), short_numeric_texts(after)
        for key, after_text in after_map.items():
            before_text = before_map.get(key)
            if before_text is not None and before_text != after_text:
                result.signals.append(
                    TransitionSignal(
                        kind="counter_changed", before_value=before_text, after_value=after_text,
                        description=f"counter/badge changed: {before_text!r} -> {after_text!r}",
                        confidence=0.45, is_explicit=True,
                    )
                )

    # -- notifications / alerts -----------------------------------------------

    @staticmethod
    def _detect_notification_change(before, after, result: DetectedTransitions) -> None:
        before_texts = {(a.text or "").strip() for a in (before.alerts or [])}
        after_texts = {(a.text or "").strip() for a in (after.alerts or [])}
        new_alerts = [a for a in (after.alerts or []) if (a.text or "").strip() and (a.text or "").strip() not in before_texts]
        for alert in new_alerts:
            result.signals.append(
                TransitionSignal(
                    kind="notification_appeared", evidence_text=alert.text or "",
                    description=f"a new alert appeared: {alert.text!r}", confidence=0.6, is_explicit=True,
                )
            )

    @staticmethod
    def _detect_success_or_failure_feedback(before, after, result: DetectedTransitions) -> None:
        before_sev = [(a.text or "", a.severity) for a in (before.alerts or [])]
        after_sev = [(a.text or "", a.severity) for a in (after.alerts or [])]
        new_feedback = [pair for pair in after_sev if pair not in before_sev and pair[1] in {"success", "error"}]
        for text, severity in new_feedback:
            result.signals.append(
                TransitionSignal(
                    kind="success_feedback" if severity == "success" else "failure_feedback",
                    evidence_text=text, description=f"{severity} feedback appeared: {text!r}",
                    confidence=0.65, is_explicit=True,
                )
            )

    # -- interactive elements: enabled/disabled flips -------------------------

    @staticmethod
    def _detect_enabled_actions_change(before, after, result: DetectedTransitions) -> None:
        before_by_id = {el.element_id: el for el in (before.interactive_elements or []) if el.element_id}
        for el in after.interactive_elements or []:
            if not el.element_id:
                continue
            prior = before_by_id.get(el.element_id)
            if prior is None:
                continue
            if bool(prior.is_enabled) != bool(el.is_enabled):
                result.signals.append(
                    TransitionSignal(
                        kind="enabled_actions_changed", evidence_text=el.accessible_name or el.text or el.element_id,
                        before_value=str(prior.is_enabled), after_value=str(el.is_enabled),
                        description=f"control {el.element_id} enabled state changed", confidence=0.55, is_explicit=True,
                    )
                )

    # -- network ----------------------------------------------------------

    @staticmethod
    def _detect_network_resource_change(before, after, result: DetectedTransitions) -> None:
        before_keys = {(e.method, e.text) for e in (before.network_evidence or [])}
        for entry in after.network_evidence or []:
            key = (entry.method, entry.text)
            if key in before_keys:
                continue
            result.signals.append(
                TransitionSignal(
                    kind="api_resource_changed", evidence_text=entry.text or "",
                    description=f"new network evidence: {entry.method} {entry.text}",
                    confidence=0.5 if not entry.failed else 0.6, is_explicit=True,
                )
            )
