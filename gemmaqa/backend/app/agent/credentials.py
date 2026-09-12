"""In-run credential vault — secrets never enter Gemma prompts or reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CredentialProfile:
    profile_id: str
    username: str
    password: str
    source: str = "supplied"  # supplied | generated_registration
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None


@dataclass
class CredentialVault:
    """Ephemeral run-scoped secrets. Never serialize into LLM context."""

    profiles: dict[str, CredentialProfile] = field(default_factory=dict)
    active_profile_id: str | None = None

    def store(self, profile: CredentialProfile, *, make_active: bool = True) -> None:
        self.profiles[profile.profile_id] = profile
        if make_active:
            self.active_profile_id = profile.profile_id

    def get(self, profile_id: str | None = None) -> CredentialProfile | None:
        pid = profile_id or self.active_profile_id
        if not pid:
            return None
        return self.profiles.get(pid)

    def resolve_ref(self, ref: str) -> str | None:
        """
        Resolve structured refs like 'primary.username' or 'primary.password'.
        Unknown refs return None (never invent secrets).
        """
        if not ref or "." not in ref:
            return None
        profile_id, field_name = ref.split(".", 1)
        profile = self.profiles.get(profile_id)
        if not profile:
            return None
        return getattr(profile, field_name, None)

    def public_flags(self) -> dict[str, Any]:
        """Safe planner context — no secret values."""
        active = self.get()
        return {
            "credential_profile_available": active is not None,
            "credential_profile_id": active.profile_id if active else None,
            "credential_source": active.source if active else None,
            "profile_ids": list(self.profiles.keys()),
        }

    def clear(self) -> None:
        self.profiles.clear()
        self.active_profile_id = None
