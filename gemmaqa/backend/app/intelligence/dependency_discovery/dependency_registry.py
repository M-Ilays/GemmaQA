"""The queryable Dependency Registry — Planner/report-facing, mirrors
`EntityRegistry`/`ActorRegistry`/`WorkflowRegistry`'s query-API shape
exactly so callers already familiar with those don't need a new mental
model here.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.dependency_discovery.dependency_memory import DependencyMemory
from app.intelligence.dependency_discovery.schemas import DependencyDescriptor, DependencyGap, DependencyRegistrySnapshot, DerivedOutputDescriptor

HIGH_CONFIDENCE_THRESHOLD = 0.6
LOW_CONFIDENCE_THRESHOLD = 0.3


class DependencyRegistry:
    def __init__(self, memory: DependencyMemory | None = None) -> None:
        self.memory = memory or DependencyMemory()
        self.gaps: list[DependencyGap] = []

    @property
    def records(self) -> dict[str, DependencyDescriptor]:
        return self.memory.records

    @property
    def outputs(self) -> dict[str, DerivedOutputDescriptor]:
        return self.memory.outputs

    def all_dependencies(self) -> list[DependencyDescriptor]:
        return list(self.memory.records.values())

    def all_outputs(self) -> list[DerivedOutputDescriptor]:
        return list(self.memory.outputs.values())

    def get(self, anchor_or_id: str) -> DependencyDescriptor | None:
        if anchor_or_id in self.memory.records:
            return self.memory.records[anchor_or_id]
        return next((d for d in self.memory.records.values() if d.dependency_id == anchor_or_id), None)

    # -- query API --------------------------------------------------------------

    def known_dependencies(self) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if d.status in {"observed", "partially_observed", "verified"}]

    def unresolved_dependencies(self) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if d.status in {"candidate", "inferred", "partially_observed", "blocked"}]

    def unresolved_kpi_dependencies(self) -> list[DependencyDescriptor]:
        kpi_output_ids = {o.output_id for o in self.all_outputs() if o.output_type == "kpi_card"}
        return [d for d in self.unresolved_dependencies() if kpi_output_ids & set(d.target_output_ids)]

    def dependencies_by_entity(self, entity_id: str) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if entity_id in d.source_entity_ids]

    def dependencies_by_workflow(self, workflow_id: str) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if workflow_id in d.source_workflow_ids]

    def dependencies_by_actor(self, actor_id: str) -> list[DependencyDescriptor]:
        return [
            d for d in self.all_dependencies()
            if actor_id in d.source_actor_ids or actor_id in d.known_producers or actor_id in d.known_consumers
        ]

    def cross_role_dependencies(self) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if d.is_cross_role()]

    def dependencies_requiring_actor_switch(self) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if d.requires_actor_switch()]

    def dependencies_with_compatible_before_after_evidence(self) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if d.status == "verified"]

    def dependencies_with_contradictions(self) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if d.contradictions or d.status == "contradicted"]

    def dependencies_with_unknown_producer(self) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if not d.known_producers]

    def dependencies_with_unknown_consumer(self) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if not d.known_consumers]

    def high_value_verification_requirements(self) -> list[DependencyDescriptor]:
        return sorted(
            (d for d in self.all_dependencies() if d.verification_requirements and d.status != "verified"),
            key=lambda d: d.confidence,
            reverse=True,
        )

    def outputs_by_page(self, page_url: str) -> list[DerivedOutputDescriptor]:
        return [o for o in self.all_outputs() if o.page_url == page_url]

    def outputs_by_actor(self, actor_term: str) -> list[DerivedOutputDescriptor]:
        return [o for o in self.all_outputs() if o.current_actor_term == actor_term]

    def known_outputs(self) -> list[DerivedOutputDescriptor]:
        return [o for o in self.all_outputs() if o.confidence >= LOW_CONFIDENCE_THRESHOLD]

    def high_confidence_dependencies(self) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if d.confidence >= HIGH_CONFIDENCE_THRESHOLD]

    def low_confidence_dependencies(self) -> list[DependencyDescriptor]:
        return [d for d in self.all_dependencies() if d.confidence < LOW_CONFIDENCE_THRESHOLD]

    def add_gap(self, gap: DependencyGap) -> None:
        key = (gap.gap_type, gap.dependency_id, tuple(sorted(gap.related_output_ids)))
        for existing in self.gaps:
            existing_key = (existing.gap_type, existing.dependency_id, tuple(sorted(existing.related_output_ids)))
            if existing_key == key:
                return
        self.gaps.append(gap)

    def dependency_gaps(self) -> list[DependencyGap]:
        return list(self.gaps)

    # -- serialization ----------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "dependency_count": len(self.records),
            "known": [d.canonical_name for d in self.known_dependencies()],
            "unresolved": [d.canonical_name for d in self.unresolved_dependencies()],
            "high_confidence": [d.canonical_name for d in self.high_confidence_dependencies()],
            "low_confidence": [d.canonical_name for d in self.low_confidence_dependencies()],
            "cross_role": [d.canonical_name for d in self.cross_role_dependencies()],
            "requiring_actor_switch": [d.canonical_name for d in self.dependencies_requiring_actor_switch()],
            "gap_count": len(self.gaps),
            "dependencies": [d.to_summary_dict() for d in self.known_dependencies()],
        }

    def snapshot(self) -> DependencyRegistrySnapshot:
        return DependencyRegistrySnapshot(**self.summary())

    def to_dict(self) -> dict[str, Any]:
        return {anchor: d.model_dump(mode="json") for anchor, d in sorted(self.records.items())}
