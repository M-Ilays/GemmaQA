"""CRUD Workflow Discovery schemas — application-neutral by construction.

A `CRUDWorkflowHypothesis` is a HYPOTHESIS, never an assertion that the
operation has been proven to work: `status` distinguishes an "entry point
seen" candidate (an Add/Edit/Delete-labelled control exists) from a
"supported" hypothesis (a before/after pair corroborated it — a create-
shaped form appeared, a row disappeared, ...) from an "executed"/"verified"
one (a runtime action actually ran and its result was checked) — the same
observed/inferred discipline every other intelligence package in this
codebase already follows.

Per the commissioning task's explicit constraint, a DELETE hypothesis must
never be inferred merely because a trash-can-labelled control exists: it
requires at least one corroborating signal (a confirmation dialog, or an
observed row-count decrease) before `status` can advance past
`"entry_point"` — see `crud_candidate_builder.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.utils.ids import new_id

CRUD_OPERATIONS = frozenset({"create", "edit", "delete"})

CRUD_HYPOTHESIS_STATUSES = frozenset({"entry_point", "supported", "executed", "verified", "contradicted"})

SAFETY_CLASSIFICATIONS = frozenset({"read_only", "controlled_write", "destructive", "unknown"})

CLEANUP_POSSIBILITIES = frozenset({"possible", "not_possible", "unknown"})

CRUD_EVIDENCE_SOURCE_KINDS = frozenset(
    {
        "record_collection",
        "global_action_control",
        "row_action_control",
        "form_intent",
        "confirmation_dialog",
        "row_count_decrease",
        "row_count_increase",
        "navigation_transition",
        "field_prepopulation",
        # A per-record control on a record-DETAIL state, which no collection
        # contains. Update and delete controls usually live there rather than in
        # the list, so without this kind of evidence those operations could never
        # be recorded at all.
        "record_detail_control",
        # A form appearing where there was none — the corroboration that an edit
        # control really opened an edit form.
        "form_appeared",
    }
)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


class CRUDEvidence(BaseModel):
    evidence_id: str = Field(default_factory=new_id)
    source_kind: str
    observed_text: str = ""
    page_url: str = ""
    element_id: Optional[str] = None

    @field_validator("source_kind")
    @classmethod
    def _validate_source_kind(cls, value: str) -> str:
        if value not in CRUD_EVIDENCE_SOURCE_KINDS:
            raise ValueError(
                f"Invalid CRUD evidence source_kind: {value!r} (expected one of {sorted(CRUD_EVIDENCE_SOURCE_KINDS)})"
            )
        return value


class CRUDWorkflowHypothesis(BaseModel):
    hypothesis_id: str = Field(default_factory=new_id)
    operation: str
    entity_hypothesis: Optional[str] = None
    actor_hypothesis: Optional[str] = None
    source_state: str = ""
    target_state: str = ""
    required_controls: list[str] = Field(default_factory=list)
    required_form_id: Optional[str] = None
    expected_transition: str = ""
    verification_surface: Optional[str] = None
    cleanup_possibility: str = "unknown"
    confidence: float = 0.3
    evidence: list[CRUDEvidence] = Field(default_factory=list)
    safety_classification: str = "unknown"
    status: str = "entry_point"
    source_page_url: str = ""
    collection_id: Optional[str] = None
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    observation_count: int = 1

    @field_validator("operation")
    @classmethod
    def _validate_operation(cls, value: str) -> str:
        if value not in CRUD_OPERATIONS:
            raise ValueError(f"Invalid CRUD operation: {value!r} (expected one of {sorted(CRUD_OPERATIONS)})")
        return value

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in CRUD_HYPOTHESIS_STATUSES:
            raise ValueError(f"Invalid status: {value!r} (expected one of {sorted(CRUD_HYPOTHESIS_STATUSES)})")
        return value

    @field_validator("safety_classification")
    @classmethod
    def _validate_safety(cls, value: str) -> str:
        if value not in SAFETY_CLASSIFICATIONS:
            raise ValueError(f"Invalid safety_classification: {value!r} (expected one of {sorted(SAFETY_CLASSIFICATIONS)})")
        return value

    @field_validator("cleanup_possibility")
    @classmethod
    def _validate_cleanup(cls, value: str) -> str:
        if value not in CLEANUP_POSSIBILITIES:
            raise ValueError(f"Invalid cleanup_possibility: {value!r} (expected one of {sorted(CLEANUP_POSSIBILITIES)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp_confidence(cls, value: float) -> float:
        return _clamp(value)

    def dedup_key(self) -> str:
        """Deterministic identity: same operation on the same entity/collection
        is the SAME hypothesis observed again, never a duplicate."""
        entity = (self.entity_hypothesis or "unknown").strip().lower()
        scope = self.collection_id or self.required_form_id or "unscoped"
        return f"{self.operation}:{entity}:{scope}"

    def to_summary_dict(self) -> dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "operation": self.operation,
            "entity_hypothesis": self.entity_hypothesis,
            "actor_hypothesis": self.actor_hypothesis,
            "status": self.status,
            "confidence": round(self.confidence, 3),
            "safety_classification": self.safety_classification,
            "cleanup_possibility": self.cleanup_possibility,
            "required_controls": list(self.required_controls),
            "required_form_id": self.required_form_id,
            "expected_transition": self.expected_transition,
            "verification_surface": self.verification_surface,
            "observation_count": self.observation_count,
        }


class CRUDRegistrySnapshot(BaseModel):
    hypothesis_count: int = 0
    by_operation: dict[str, int] = Field(default_factory=dict)
    entry_point_only: list[str] = Field(default_factory=list)
    supported_or_better: list[str] = Field(default_factory=list)
    destructive: list[str] = Field(default_factory=list)
    hypotheses: list[dict] = Field(default_factory=list)
