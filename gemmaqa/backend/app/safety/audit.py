"""Safety decision audit trail (no secrets)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.utils.ids import new_id
from app.utils.sanitization import sanitize_dict


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class SafetyAuditRecord:
    """One proposed-action safety decision."""

    audit_id: str
    run_id: str
    timestamp: datetime
    proposed_action: str
    action_level: str
    safety_decision: str  # allow | block | warn
    execution_decision: str  # execute | skip | rewrite
    block_reason: str = ""
    matched_pattern: str = ""
    element_id: str | None = None
    target_url: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat() + "Z"
        return sanitize_dict(data)


class SafetyAuditLog:
    """In-memory per-run audit buffer (also emitted as events)."""

    def __init__(self) -> None:
        self._by_run: dict[str, list[SafetyAuditRecord]] = {}

    def record(
        self,
        *,
        run_id: str,
        proposed_action: str,
        action_level: str,
        safety_decision: str,
        execution_decision: str,
        block_reason: str = "",
        matched_pattern: str = "",
        element_id: str | None = None,
        target_url: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> SafetyAuditRecord:
        rec = SafetyAuditRecord(
            audit_id=new_id(),
            run_id=run_id,
            timestamp=_utc_now(),
            proposed_action=proposed_action,
            action_level=action_level,
            safety_decision=safety_decision,
            execution_decision=execution_decision,
            block_reason=block_reason,
            matched_pattern=matched_pattern,
            element_id=element_id,
            target_url=target_url,
            details=sanitize_dict(details or {}),
        )
        self._by_run.setdefault(run_id, []).append(rec)
        # Cap memory
        if len(self._by_run[run_id]) > 2000:
            self._by_run[run_id] = self._by_run[run_id][-1000:]
        return rec

    def for_run(self, run_id: str) -> list[SafetyAuditRecord]:
        return list(self._by_run.get(run_id, []))

    def clear(self, run_id: str) -> None:
        self._by_run.pop(run_id, None)


safety_audit = SafetyAuditLog()
