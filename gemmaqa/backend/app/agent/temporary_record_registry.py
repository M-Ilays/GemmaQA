"""Temporary Record Registry — the run-scoped store of every record GemmaQA
itself created, and the ONLY authority for whether GemmaQA is allowed to
delete something.

Default policy, enforced in code (not just by convention): a record is
eligible for cleanup ONLY if `entry.run_id == current_run_id`, unless the
caller passes `allow_cross_run_cleanup=True` explicitly — see
`eligible_for_cleanup`. There is no "purge everything" operation anywhere in
this module; cleanup is always per-record, always logged, always bounded.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from app.utils.ids import new_id

# Below this, an identity is too generic to match a record by — a two- or
# three-character value appears in unrelated rows by coincidence, and a
# mis-identified row is the difference between deleting GemmaQA's own test
# record and deleting someone else's data.
MIN_IDENTITY_MATCH_CHARS = 4


def identity_matches_cells(identity: str, cell_values: Iterable[Any]) -> bool:
    """Does any cell of a collection row carry this record's identity?

    THE one rule for "is this the record I created", shared by the frontier
    (which decides whether to open a row) and the cleanup planner (which
    decides whether to delete through it). Two separate implementations of
    this question is not a style problem: the frontier's looser rule found and
    opened the created row while cleanup's stricter one could not see it, so
    delete was discovered every run and performed in none.

    Comparison is case-insensitive and substring in BOTH directions, because a
    collection commonly renders a truncated or reformatted version of what was
    submitted (an email without its domain, a name split across columns).
    """
    needle = (identity or "").strip().lower()
    if len(needle) < MIN_IDENTITY_MATCH_CHARS:
        return False
    for cell in cell_values or []:
        text = str(cell or "").strip().lower()
        if not text:
            continue
        if needle in text or (len(text) >= MIN_IDENTITY_MATCH_CHARS and text in needle):
            return True
    return False

# ---------------------------------------------------------------------------
# Lifecycle: Created -> Verified -> Optionally Updated -> Cleanup Requested
# -> Delete Action Validated -> Deleted -> Absence Verified, plus the two
# failure/terminal side-states every real cleanup attempt needs.
# ---------------------------------------------------------------------------

RECORD_LIFECYCLE_STATES = frozenset(
    {
        "created",
        "verified",
        "updated",
        "cleanup_requested",
        "delete_action_validated",
        "deleted",
        "absence_verified",
        "cleanup_failed",
        "manual_cleanup_required",
    }
)

TERMINAL_RECORD_STATES = frozenset({"absence_verified", "manual_cleanup_required"})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"verified", "cleanup_failed"}),
    # `cleanup_failed` must be reachable from both of these. It was not, so a
    # cleanup attempt that failed from `verified`/`updated` was silently
    # swallowed by `can_transition` and the record kept reporting as merely
    # `pending` — indistinguishable from one nobody had tried to delete.
    "verified": frozenset({"updated", "cleanup_requested", "cleanup_failed"}),
    "updated": frozenset({"updated", "cleanup_requested", "cleanup_failed"}),
    "cleanup_requested": frozenset({"delete_action_validated", "cleanup_failed"}),
    "delete_action_validated": frozenset({"deleted", "cleanup_failed"}),
    "deleted": frozenset({"absence_verified", "cleanup_failed"}),
    "cleanup_failed": frozenset({"cleanup_requested", "manual_cleanup_required"}),
    "absence_verified": frozenset(),
    "manual_cleanup_required": frozenset(),
}

MAX_CLEANUP_ATTEMPTS = 3


class RecordUpdateEvent(BaseModel):
    field_key: str
    before_value: Optional[str] = None
    after_value: Optional[str] = None
    verified: bool = False
    evidence: list[str] = Field(default_factory=list)
    at: datetime = Field(default_factory=datetime.utcnow)


class CleanupPlan(BaseModel):
    delete_control_element_id: Optional[str] = None
    confirmation_required: bool = True
    verification_strategy: str = "absence_from_collection"
    manual_instructions: str = ""


class CleanupResult(BaseModel):
    attempted: bool = False
    succeeded: bool = False
    attempts: int = 0
    last_error: Optional[str] = None
    absence_verified: bool = False
    manual_instructions: Optional[str] = None
    at: Optional[datetime] = None


class TemporaryRecordEntry(BaseModel):
    temporary_record_id: str = Field(default_factory=new_id)
    run_id: str
    record_type: str = "unknown"
    generated_identity: str = ""
    creation_scenario: Optional[str] = None
    creation_action_element_id: Optional[str] = None
    creation_form_id: Optional[str] = None
    verification_evidence: list[str] = Field(default_factory=list)
    current_state: str = "created"
    update_history: list[RecordUpdateEvent] = Field(default_factory=list)
    cleanup_plan: Optional[CleanupPlan] = None
    cleanup_result: CleanupResult = Field(default_factory=CleanupResult)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    collection_element_id: Optional[str] = None
    # The URL where this record was observed in a collection. Cleanup runs at the
    # END of a run, by which time the browser is usually somewhere else entirely
    # — without a way back to the record, delete was discovered every run and
    # performed in none of them.
    list_url: Optional[str] = None

    @field_validator("current_state")
    @classmethod
    def _validate_state(cls, value: str) -> str:
        if value not in RECORD_LIFECYCLE_STATES:
            raise ValueError(f"Invalid record lifecycle state: {value!r} (expected one of {sorted(RECORD_LIFECYCLE_STATES)})")
        return value

    def can_transition(self, target: str) -> bool:
        return target in _ALLOWED_TRANSITIONS.get(self.current_state, frozenset())

    def is_terminal(self) -> bool:
        return self.current_state in TERMINAL_RECORD_STATES


class TemporaryRecordRegistry:
    """One instance per run — see `RunMemory.temporary_record_registry`."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.entries: dict[str, TemporaryRecordEntry] = {}

    # -- creation -------------------------------------------------------------

    def register_created(
        self,
        *,
        record_type: str,
        generated_identity: str,
        creation_scenario: str | None = None,
        creation_action_element_id: str | None = None,
        creation_form_id: str | None = None,
        collection_element_id: str | None = None,
        list_url: str | None = None,
    ) -> TemporaryRecordEntry:
        entry = TemporaryRecordEntry(
            run_id=self.run_id, record_type=record_type, generated_identity=generated_identity,
            creation_scenario=creation_scenario, creation_action_element_id=creation_action_element_id,
            creation_form_id=creation_form_id, collection_element_id=collection_element_id,
            list_url=list_url,
        )
        self.entries[entry.temporary_record_id] = entry
        return entry

    # -- verification / update --------------------------------------------------

    def mark_verified(self, temporary_record_id: str, *, evidence: list[str]) -> None:
        entry = self.entries.get(temporary_record_id)
        if entry is None or not entry.can_transition("verified"):
            return
        entry.verification_evidence.extend(evidence)
        entry.current_state = "verified"

    def mark_updated(
        self, temporary_record_id: str, *, field_key: str, before_value: str | None, after_value: str | None, verified: bool, evidence: list[str]
    ) -> None:
        entry = self.entries.get(temporary_record_id)
        if entry is None or not entry.can_transition("updated"):
            return
        entry.update_history.append(
            RecordUpdateEvent(field_key=field_key, before_value=before_value, after_value=after_value, verified=verified, evidence=evidence)
        )
        # If the edited field WAS the lookup identity (often first name), keep
        # matching the value now on screen — otherwise detail-page delete
        # cannot prove this is still our record.
        before = (before_value or "").strip().lower()
        after = (after_value or "").strip()
        identity = (entry.generated_identity or "").strip()
        if (
            after
            and len(after) >= MIN_IDENTITY_MATCH_CHARS
            and identity
            and before
            and identity.lower() == before
        ):
            entry.generated_identity = after
        entry.current_state = "updated"

    # -- cleanup lifecycle --------------------------------------------------------

    def request_cleanup(self, temporary_record_id: str, *, plan: CleanupPlan) -> bool:
        entry = self.entries.get(temporary_record_id)
        if entry is None or not entry.can_transition("cleanup_requested"):
            return False
        entry.cleanup_plan = plan
        entry.current_state = "cleanup_requested"
        return True

    def mark_delete_action_validated(self, temporary_record_id: str) -> bool:
        entry = self.entries.get(temporary_record_id)
        if entry is None or not entry.can_transition("delete_action_validated"):
            return False
        entry.current_state = "delete_action_validated"
        return True

    def mark_deleted(self, temporary_record_id: str) -> bool:
        entry = self.entries.get(temporary_record_id)
        if entry is None or not entry.can_transition("deleted"):
            return False
        entry.current_state = "deleted"
        entry.cleanup_result.attempted = True
        entry.cleanup_result.attempts += 1
        entry.cleanup_result.at = datetime.utcnow()
        return True

    def mark_absence_verified(self, temporary_record_id: str) -> bool:
        entry = self.entries.get(temporary_record_id)
        if entry is None or not entry.can_transition("absence_verified"):
            return False
        entry.current_state = "absence_verified"
        entry.cleanup_result.succeeded = True
        entry.cleanup_result.absence_verified = True
        return True

    def mark_cleanup_failed(self, temporary_record_id: str, *, error: str) -> None:
        """Never repeatedly attempts indefinitely: once `MAX_CLEANUP_ATTEMPTS`
        is reached, the entry moves to `manual_cleanup_required` with
        instructions instead of retrying forever."""
        entry = self.entries.get(temporary_record_id)
        if entry is None:
            return
        entry.cleanup_result.attempted = True
        entry.cleanup_result.attempts += 1
        entry.cleanup_result.last_error = error
        if entry.cleanup_result.attempts >= MAX_CLEANUP_ATTEMPTS:
            entry.current_state = "manual_cleanup_required"
            entry.cleanup_result.manual_instructions = self._manual_instructions(entry)
        elif entry.can_transition("cleanup_failed"):
            entry.current_state = "cleanup_failed"

    @staticmethod
    def _manual_instructions(entry: TemporaryRecordEntry) -> str:
        return (
            f"GemmaQA could not automatically remove the {entry.record_type} test record "
            f"'{entry.generated_identity}' (temporary_record_id={entry.temporary_record_id}) after "
            f"{entry.cleanup_result.attempts} attempt(s). Last error: {entry.cleanup_result.last_error or 'unknown'}. "
            f"It is clearly tagged as GemmaQA test data — locate it by that identity in the application's "
            f"record-management UI and delete it manually."
        )

    # -- delete-eligibility policy (the hard safety gate) --------------------------

    def eligible_for_cleanup(
        self, temporary_record_id: str, *, current_run_id: str, allow_cross_run_cleanup: bool = False
    ) -> bool:
        """GemmaQA must never delete a record unless it was created in the
        CURRENT run, or the caller explicitly passes
        `allow_cross_run_cleanup=True` — the default is always current-run-
        only, with no code path that silently widens it."""
        entry = self.entries.get(temporary_record_id)
        if entry is None:
            return False
        if entry.is_terminal():
            return False
        if entry.run_id != current_run_id and not allow_cross_run_cleanup:
            return False
        return entry.current_state in {"verified", "updated", "cleanup_failed"}

    def records_pending_cleanup(self) -> list[TemporaryRecordEntry]:
        return [e for e in self.entries.values() if e.current_state in {"verified", "updated", "cleanup_failed"}]

    def records_requiring_manual_cleanup(self) -> list[TemporaryRecordEntry]:
        return [e for e in self.entries.values() if e.current_state == "manual_cleanup_required"]

    def manual_cleanup_report(self) -> list[dict[str, Any]]:
        return [
            {
                "temporary_record_id": e.temporary_record_id,
                "record_type": e.record_type,
                "generated_identity": e.generated_identity,
                "instructions": e.cleanup_result.manual_instructions or self._manual_instructions(e),
            }
            for e in self.records_requiring_manual_cleanup()
        ]

    def snapshot(self) -> dict[str, Any]:
        by_state: dict[str, int] = {}
        for e in self.entries.values():
            by_state[e.current_state] = by_state.get(e.current_state, 0) + 1
        return {
            "run_id": self.run_id,
            "total_records": len(self.entries),
            "by_state": by_state,
            "pending_cleanup": len(self.records_pending_cleanup()),
            "manual_cleanup_required": len(self.records_requiring_manual_cleanup()),
            "records": [e.model_dump(mode="json") for e in self.entries.values()],
        }
