"""Actor Discovery schemas — application-neutral by construction.

No field in this module may name a business role. `canonical_name` and
`aliases` hold whatever terms the TARGET APPLICATION was observed using
(a role dropdown option, a Users-table cell, a settings-page label); they
are data extracted at runtime, never vocabulary shipped in this code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from app.utils.ids import new_id

# Where a piece of actor/role/permission evidence was observed. Structural
# source kinds only — WHERE something appeared, never WHAT it means.
ACTOR_EVIDENCE_SOURCE_KINDS = frozenset(
    {
        "navigation_item",
        "sidebar_region",
        "header_region",
        "breadcrumb",
        "settings_page",
        "user_management_page",
        "roles_page",
        "permissions_page",
        "role_dropdown_option",
        "role_table_cell",
        "user_creation_form",
        "invite_dialog",
        "assignment_dialog",
        "approval_dialog",
        "workflow_ownership",
        "network_403",
        "network_401",
        "jwt_claim",
        "auth_response",
        "profile_menu",
        "account_page",
        "session_info",
        "access_denied_page",
        "visible_text",
        "disabled_control",
        "visible_control",
        "memory",
        "entity_registry",
    }
)

# Lifecycle of a discovered actor in the registry. Mirrors
# app.intelligence.entity_discovery's candidate/confirmed/incomplete/stale
# vocabulary, plus "unverified" — an actor named by evidence (e.g. a Users
# table row) but never actually observed as an authenticated session.
ACTOR_STATUSES = frozenset({"candidate", "unverified", "confirmed", "incomplete", "stale"})

# Deliberately narrow, generic relationship kinds for role hierarchy/multi-
# role — no workflow reconstruction here either (same discipline as
# app.intelligence.entity_discovery.entity_relationship_builder).
ROLE_RELATIONSHIP_KINDS = frozenset({"parent_of", "inherits_from", "co_assigned_with"})


class ActorEvidence(BaseModel):
    """One observation of an actor/role/permission term somewhere in the
    application."""

    evidence_id: str = Field(default_factory=new_id)
    source_kind: str
    observed_text: str = ""
    page_url: str = ""
    state_fingerprint: str = ""
    element_id: Optional[str] = None
    observed_at_iteration: int = 0

    @field_validator("source_kind")
    @classmethod
    def _validate_source_kind(cls, value: str) -> str:
        if value not in ACTOR_EVIDENCE_SOURCE_KINDS:
            raise ValueError(
                f"Invalid actor evidence source_kind: {value!r} (expected one of {sorted(ACTOR_EVIDENCE_SOURCE_KINDS)})"
            )
        return value


class PermissionCandidate(BaseModel):
    """One inferred permission for an actor — a `can_<operation>[_<subject>]`
    id, e.g. `can_create_customer`, `can_access_settings`, `can_approve`.
    Positive (control visible/enabled, page reachable) and negative (control
    disabled, 401/403 hit) evidence are tracked separately so a permission's
    net standing is always explainable, not just asserted."""

    permission_id: str
    positive_evidence: list[ActorEvidence] = Field(default_factory=list)
    negative_evidence: list[ActorEvidence] = Field(default_factory=list)
    confidence: float = 0.3

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))

    @property
    def granted(self) -> bool:
        """Net standing: more positive than negative evidence. Ties (or no
        evidence at all) default to NOT granted — a permission is only
        reported as held once evidence actually favors it."""
        return len(self.positive_evidence) > len(self.negative_evidence)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "permission_id": self.permission_id,
            "granted": self.granted,
            "confidence": round(self.confidence, 3),
            "positive_evidence_count": len(self.positive_evidence),
            "negative_evidence_count": len(self.negative_evidence),
        }


class RoleRelationship(BaseModel):
    """One OBSERVED structural relationship between two discovered actors —
    evidence only, no workflow reconstruction."""

    relationship_id: str = Field(default_factory=new_id)
    subject_actor_id: str
    kind: str
    object_actor_id: str
    evidence: list[ActorEvidence] = Field(default_factory=list)
    confidence: float = 0.4

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        if value not in ROLE_RELATIONSHIP_KINDS:
            raise ValueError(f"Invalid role relationship kind: {value!r} (expected one of {sorted(ROLE_RELATIONSHIP_KINDS)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class ActorSession(BaseModel):
    """Session infrastructure for a known actor — per-actor state so the run
    (or a future run) COULD return to this actor later. This phase only
    prepares the infrastructure: nothing here triggers automatic switching.
    """

    actor_id: str
    credential_profile_id: Optional[str] = None
    login_method: Optional[str] = None  # e.g. "login" | "registration" | "sso" | "anonymous"
    landing_page: Optional[str] = None
    dashboard_url: Optional[str] = None
    known_permission_ids: list[str] = Field(default_factory=list)
    last_explored_url: Optional[str] = None
    last_state_fingerprint: Optional[str] = None
    last_updated_iteration: int = 0
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "credential_profile_id": self.credential_profile_id,
            "login_method": self.login_method,
            "landing_page": self.landing_page,
            "dashboard_url": self.dashboard_url,
            "known_permission_ids": list(self.known_permission_ids),
            "last_explored_url": self.last_explored_url,
            "last_state_fingerprint": self.last_state_fingerprint,
        }


class ActorRecord(BaseModel):
    """A discovered actor. `canonical_name` is the normalized form of
    whatever the application itself calls this identity/role."""

    actor_id: str = Field(default_factory=new_id)
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    status: str = "candidate"
    supporting_evidence: list[ActorEvidence] = Field(default_factory=list)
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    first_seen_iteration: int = 0
    last_seen_iteration: int = 0

    known_pages: list[str] = Field(default_factory=list)
    known_permissions: list[PermissionCandidate] = Field(default_factory=list)
    known_entities: list[str] = Field(default_factory=list)
    known_workflows: list[str] = Field(default_factory=list)
    known_navigation: list[str] = Field(default_factory=list)
    known_dashboards: list[str] = Field(default_factory=list)
    known_home_pages: list[str] = Field(default_factory=list)
    known_assignment_types: list[str] = Field(default_factory=list)
    known_visibility: list[str] = Field(default_factory=list)  # visible region_types
    known_login_methods: list[str] = Field(default_factory=list)
    known_session_types: list[str] = Field(default_factory=list)  # e.g. authenticated | anonymous | guest

    # Descriptive modifiers observed directly in the role's own text
    # ("Guest User", "Default Role", "System Admin", "Temporary Access") —
    # generic English descriptors of a role SLOT's nature, never a business
    # identity name in themselves.
    is_guest: bool = False
    is_system: bool = False
    is_default: bool = False
    is_temporary: bool = False

    relationships: list[RoleRelationship] = Field(default_factory=list)
    session: Optional[ActorSession] = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ACTOR_STATUSES:
            raise ValueError(f"Invalid actor status: {value!r} (expected one of {sorted(ACTOR_STATUSES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))

    def granted_permission_ids(self) -> list[str]:
        return sorted(p.permission_id for p in self.known_permissions if p.granted)

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "actor_id": self.actor_id,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "confidence": round(self.confidence, 3),
            "status": self.status,
            "known_pages": list(self.known_pages),
            "known_permissions": [p.to_summary_dict() for p in self.known_permissions],
            "known_entities": list(self.known_entities),
            "known_workflows": list(self.known_workflows),
            "known_navigation": list(self.known_navigation),
            "known_dashboards": list(self.known_dashboards),
            "known_home_pages": list(self.known_home_pages),
            "known_assignment_types": list(self.known_assignment_types),
            "known_visibility": list(self.known_visibility),
            "known_login_methods": list(self.known_login_methods),
            "known_session_types": list(self.known_session_types),
            "flags": {
                "guest": self.is_guest,
                "system": self.is_system,
                "default": self.is_default,
                "temporary": self.is_temporary,
            },
            "relationship_count": len(self.relationships),
            "evidence_count": len(self.supporting_evidence),
            "has_session": self.session is not None,
        }


class ActorCandidate(BaseModel):
    """A raw candidate actor/role term extracted from one page observation,
    BEFORE classification. Many candidates never become actors."""

    term: str
    raw_term: str
    evidence: ActorEvidence
