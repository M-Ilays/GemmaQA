"""Workflow Registry — the central, queryable catalog of discovered
workflows, mirroring Entity/Actor Registry's shape and Planner-facing role.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.workflow_discovery.schemas import (
    WorkflowDescriptor,
    WorkflowGap,
    WorkflowRegistrySnapshot,
)
from app.intelligence.workflow_discovery.workflow_memory import WorkflowMemory

HIGH_CONFIDENCE_THRESHOLD = 0.6
LOW_CONFIDENCE_THRESHOLD = 0.3


class WorkflowRegistry:
    def __init__(self, memory: WorkflowMemory | None = None) -> None:
        self.memory = memory or WorkflowMemory()
        self.gaps: list[WorkflowGap] = []

    @property
    def records(self) -> dict[str, WorkflowDescriptor]:
        return self.memory.records

    def all_workflows(self) -> list[WorkflowDescriptor]:
        return sorted(self.records.values(), key=lambda w: (-w.confidence, w.canonical_name))

    def get(self, anchor_or_id: str) -> WorkflowDescriptor | None:
        resolved = self.memory.resolve_anchor(anchor_or_id)
        if resolved:
            return self.records[resolved]
        return next((w for w in self.records.values() if w.workflow_id == anchor_or_id), None)

    # -- Planner / reporting queries ------------------------------------------

    def known_workflows(self) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if w.status in {"confirmed", "partial"}]

    def incomplete_workflows(self) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if w.status == "partial"]

    def high_confidence_workflows(self) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if w.confidence >= HIGH_CONFIDENCE_THRESHOLD]

    def low_confidence_workflows(self) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if w.confidence < LOW_CONFIDENCE_THRESHOLD]

    def workflows_by_entity(self, entity_id: str) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if any(p.entity_id == entity_id for p in w.entities)]

    def workflows_by_actor(self, actor_id: str) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if any(p.actor_id == actor_id for p in w.actors)]

    def workflows_by_state(self, state_label: str) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if state_label in {s.label for s in w.states}]

    def workflows_by_confidence(self, *, min_confidence: float = 0.0, max_confidence: float = 1.0) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if min_confidence <= w.confidence <= max_confidence]

    def cross_role_workflows(self) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if w.is_cross_role()]

    def workflows_requiring_another_actor(self) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if w.requires_another_actor()]

    def workflows_with_unresolved_prerequisites(self) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if any(p.satisfied is False for p in w.prerequisites)]

    def workflows_with_visible_outcomes_but_unknown_producers(self) -> list[WorkflowDescriptor]:
        return [w for w in self.all_workflows() if any(o.produced_by_step_id is None for o in w.outcomes)]

    # -- gaps -------------------------------------------------------------

    def add_gap(self, gap: WorkflowGap) -> None:
        existing = next(
            (g for g in self.gaps if g.gap_type == gap.gap_type and g.workflow_id == gap.workflow_id and g.description == gap.description),
            None,
        )
        if existing is not None:
            return
        self.gaps.append(gap)
        if gap.workflow_id:
            workflow = self.get(gap.workflow_id) or next((w for w in self.records.values() if w.workflow_id == gap.workflow_id), None)
            if workflow is not None and gap.gap_id not in workflow.unresolved_gaps:
                workflow.unresolved_gaps.append(gap.gap_id)

    def workflow_gaps(self) -> list[WorkflowGap]:
        return list(self.gaps)

    # -- serialization / reporting ---------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "workflow_count": len(self.records),
            "known": [w.canonical_name for w in self.known_workflows()],
            "incomplete": [w.canonical_name for w in self.incomplete_workflows()],
            "high_confidence": [w.canonical_name for w in self.high_confidence_workflows()],
            "low_confidence": [w.canonical_name for w in self.low_confidence_workflows()],
            "cross_role": [w.canonical_name for w in self.cross_role_workflows()],
            "requiring_another_actor": [w.canonical_name for w in self.workflows_requiring_another_actor()],
            "gap_count": len(self.gaps),
            "workflows": [w.to_summary_dict() for w in self.known_workflows()],
        }

    def snapshot(self) -> WorkflowRegistrySnapshot:
        return WorkflowRegistrySnapshot(**self.summary())

    def to_dict(self) -> dict[str, Any]:
        return {anchor: w.model_dump(mode="json") for anchor, w in sorted(self.records.items())}
