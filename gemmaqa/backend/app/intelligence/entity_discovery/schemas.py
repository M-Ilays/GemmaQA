"""Entity Discovery schemas — application-neutral by construction.

No field in this module may name a business concept. `canonical_name` and
`aliases` hold whatever terms the TARGET APPLICATION was observed using;
they are data extracted at runtime, never vocabulary shipped in this code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from app.utils.ids import new_id

# Where a piece of entity evidence was observed. Structural source kinds only
# (the same spirit as app.perception.models.SOURCE_KINDS) — these describe
# WHERE in a page/app a term appeared, never WHAT the term means.
EVIDENCE_SOURCE_KINDS = frozenset(
    {
        "navigation_item",
        "breadcrumb",
        "tab",
        "heading",
        "page_title",
        "url_segment",
        "table_column",
        "table_region",
        "form_field",
        "form_region",
        "button_label",
        "link_label",
        "dialog",
        "card",
        "api_endpoint",
        "network_response",
        "visible_text",
        "aria_label",
        "memory",
    }
)

# Lifecycle of a discovered entity in the registry.
ENTITY_STATUSES = frozenset({"candidate", "confirmed", "incomplete", "stale"})

# Relationship kinds are deliberately few and generic — this phase only
# RECORDS observed structural relationships, it does not reconstruct
# workflows (see docs/ENTITY_DISCOVERY_ENGINE.md §Relationships).
RELATIONSHIP_KINDS = frozenset({"owns", "belongs_to", "references", "assigned_to"})


class EntityEvidence(BaseModel):
    """One observation of an entity term somewhere in the application."""

    evidence_id: str = Field(default_factory=new_id)
    source_kind: str
    # The literal observed text/fragment the term was extracted from
    # (e.g. the nav label, the column header, the URL path segment).
    observed_text: str = ""
    # Where it was seen.
    page_url: str = ""
    state_fingerprint: str = ""
    element_id: Optional[str] = None
    observed_at_iteration: int = 0

    @field_validator("source_kind")
    @classmethod
    def _validate_source_kind(cls, value: str) -> str:
        if value not in EVIDENCE_SOURCE_KINDS:
            raise ValueError(
                f"Invalid evidence source_kind: {value!r} (expected one of {sorted(EVIDENCE_SOURCE_KINDS)})"
            )
        return value


class EntityOperation(BaseModel):
    """One operation observed to be available for an entity — inferred from
    UI verbs / HTTP methods actually seen, never assumed."""

    operation: str  # canonical verb form, lowercased (e.g. "create", "export")
    evidence: list[EntityEvidence] = Field(default_factory=list)
    confidence: float = 0.5

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class EntityRelationship(BaseModel):
    """One OBSERVED structural relationship between two discovered entities.
    Records evidence only — no workflow reconstruction."""

    relationship_id: str = Field(default_factory=new_id)
    subject_entity_id: str
    kind: str
    object_entity_id: str
    evidence: list[EntityEvidence] = Field(default_factory=list)
    confidence: float = 0.4

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        if value not in RELATIONSHIP_KINDS:
            raise ValueError(f"Invalid relationship kind: {value!r} (expected one of {sorted(RELATIONSHIP_KINDS)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class EntityRecord(BaseModel):
    """A discovered business entity. `canonical_name` is the normalized
    singular form of whatever the application itself calls this thing."""

    entity_id: str = Field(default_factory=new_id)
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    status: str = "candidate"
    evidence: list[EntityEvidence] = Field(default_factory=list)
    # Distinct structural places this entity has been seen (source kinds).
    discovered_in: list[str] = Field(default_factory=list)
    operations: list[EntityOperation] = Field(default_factory=list)
    related_pages: list[str] = Field(default_factory=list)
    related_forms: list[str] = Field(default_factory=list)
    related_tables: list[str] = Field(default_factory=list)
    related_workflows: list[str] = Field(default_factory=list)
    # Observed value-states for this entity (e.g. status-column values seen
    # in its tables) — recorded verbatim from the application, never invented.
    known_states: list[str] = Field(default_factory=list)
    relationships: list[EntityRelationship] = Field(default_factory=list)
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    first_seen_iteration: int = 0
    last_seen_iteration: int = 0

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ENTITY_STATUSES:
            raise ValueError(f"Invalid entity status: {value!r} (expected one of {sorted(ENTITY_STATUSES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))

    def operation_names(self) -> list[str]:
        return sorted({op.operation for op in self.operations})

    def to_summary_dict(self) -> dict[str, Any]:
        """Compact, prompt/trace-safe summary (no raw evidence bodies)."""
        return {
            "entity_id": self.entity_id,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "confidence": round(self.confidence, 3),
            "status": self.status,
            "discovered_in": list(self.discovered_in),
            "operations": self.operation_names(),
            "related_pages": list(self.related_pages),
            "related_forms": list(self.related_forms),
            "related_tables": list(self.related_tables),
            "known_states": list(self.known_states),
            "relationship_count": len(self.relationships),
            "evidence_count": len(self.evidence),
        }


class EntityCandidate(BaseModel):
    """A raw candidate term extracted from one page observation, BEFORE
    classification. Many candidates never become entities."""

    term: str  # normalized singular, lowercased
    raw_term: str  # as observed
    evidence: EntityEvidence
