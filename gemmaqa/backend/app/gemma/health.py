"""Provider health tracking (no secrets)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Closed vocabulary for the connection state a client may render. Exists because
# `reachable` is a tri-state (True / False / None) and a UI that treats None as
# falsy shows a correctly-configured provider as broken. This collapses the
# (configured, reachable) pair into one label that cannot be misread.
CONNECTION_STATUSES = frozenset({"not_checked", "reachable", "unreachable", "misconfigured"})


def connection_status(*, configured: bool, reachable: bool | None) -> str:
    """The single rule mapping (configured, reachable) to a renderable label.

    One implementation, used by `to_public_dict` and therefore by every provider,
    so the API and any future UI cannot disagree about what "unknown" looks like.
    """
    if not configured:
        return "misconfigured"
    if reachable is None:
        return "not_checked"
    return "reachable" if reachable else "unreachable"


@dataclass
class ProviderHealth:
    """Mutable health snapshot for the active Gemma provider."""

    provider_type: str
    model_id: str = ""
    multimodal_support: bool = False
    configured: bool = False
    reachable: bool | None = None
    last_error: str = ""
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_checked_at: datetime | None = None
    config_error: str = ""

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.total_successes += 1
        self.reachable = True
        self.last_error = ""
        self.last_checked_at = _utc_now()

    def record_failure(self, error: str) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_error = (error or "unknown error")[:300]
        self.last_checked_at = _utc_now()

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "provider_type": self.provider_type,
            "configured": self.configured,
            # Unchanged meaning: True only after a real success, False only after
            # a real failure, None when never exercised.
            "reachable": self.reachable,
            # Additive. Same information, in a form a client cannot misread.
            "connection_status": connection_status(
                configured=self.configured, reachable=self.reachable
            ),
            "model_identifier": self.model_id or None,
            "multimodal_support": self.multimodal_support,
            "last_error_summary": self.last_error or None,
            "config_error": self.config_error or None,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "last_checked_at": self.last_checked_at.isoformat() + "Z"
            if self.last_checked_at
            else None,
        }


# Process-wide handle updated by the active provider
_ACTIVE_HEALTH: ProviderHealth | None = None


def set_active_health(health: ProviderHealth) -> None:
    global _ACTIVE_HEALTH
    _ACTIVE_HEALTH = health


def get_active_health() -> ProviderHealth | None:
    return _ACTIVE_HEALTH
