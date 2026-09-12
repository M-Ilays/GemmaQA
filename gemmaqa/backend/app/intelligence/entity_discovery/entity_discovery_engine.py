"""Entity Discovery Engine — the orchestrator.

One call per observation: takes the CanonicalPageModel the Perception Engine
just produced, extracts/classifies candidates, merges them into the registry,
records observed relationships, and emits a structured trace explaining WHAT
was discovered and WHY (evidence, confidence, merged aliases, operations,
relationship candidates).

Deterministic, read-only over its inputs, and — like the Perception Engine —
never touches the browser and never calls an LLM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.intelligence.entity_discovery.entity_classifier import EntityClassifier
from app.intelligence.entity_discovery.entity_registry import EntityRegistry
from app.intelligence.entity_discovery.entity_relationship_builder import EntityRelationshipBuilder
from app.utils.exploration_trace import record as trace_record
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from app.perception.models import CanonicalPageModel

logger = get_logger("intelligence.entity_discovery")


class EntityDiscoveryEngine:
    def __init__(self, registry: EntityRegistry | None = None) -> None:
        self.registry = registry or EntityRegistry()
        self.classifier = EntityClassifier()
        self.relationship_builder = EntityRelationshipBuilder()

    def observe(self, model: "CanonicalPageModel", *, iteration: int = 0) -> dict[str, Any]:
        """Process one page observation. Returns the change summary (also
        emitted as an `entity_discovery.observation` trace event)."""
        discovery = self.classifier.classify(model, iteration=iteration)
        changes = self.registry.memory.merge_discovery(discovery, iteration=iteration)

        relationship_findings = self.relationship_builder.build(
            model,
            discovery,
            known_terms=self.registry.records.keys(),
            iteration=iteration,
        )
        new_relationships = self.registry.memory.merge_relationships(relationship_findings)

        summary = self._summarize(model, discovery, changes, new_relationships)
        self._trace(summary)
        return summary

    # -- tracing --------------------------------------------------------------

    def _summarize(
        self,
        model: "CanonicalPageModel",
        discovery: Any,
        changes: dict[str, list[str]],
        new_relationships: list[str],
    ) -> dict[str, Any]:
        touched = sorted(set(changes["created"]) | set(changes["updated"]))
        entity_details = []
        for term in touched:
            record = self.registry.records.get(term)
            if record is None:
                continue
            entity_details.append(
                {
                    "canonical_name": record.canonical_name,
                    "status": record.status,
                    "confidence": round(record.confidence, 3),
                    "discovered_in": record.discovered_in,
                    "operations": record.operation_names(),
                    "aliases": record.aliases,
                    # WHY: the distinct evidence kinds + a sample observed text
                    # per kind, so the trace explains the discovery.
                    "why": self._why(record),
                }
            )
        return {
            "url": model.url,
            "state_fingerprint": model.state_fingerprint,
            "page_context_entity": discovery.page_context_term,
            "created": changes["created"],
            "updated": changes["updated"],
            "alias_merged": changes["alias_merged"],
            "new_relationships": new_relationships,
            "entities": entity_details,
            "registry_totals": {
                "total": len(self.registry.records),
                "confirmed": len([r for r in self.registry.records.values() if r.status == "confirmed"]),
                "incomplete": len(self.registry.incomplete_entities()),
                "candidates": len(self.registry.unknown_entities()),
            },
        }

    @staticmethod
    def _why(record: Any) -> list[str]:
        seen_kinds: set[str] = set()
        why: list[str] = []
        for ev in record.evidence:
            if ev.source_kind in seen_kinds:
                continue
            seen_kinds.add(ev.source_kind)
            why.append(f"{ev.source_kind}: {ev.observed_text!r}")
            if len(why) >= 6:
                break
        return why

    @staticmethod
    def _trace(summary: dict[str, Any]) -> None:
        trace_record("entity_discovery.observation", **summary)
