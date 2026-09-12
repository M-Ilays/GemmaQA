"""Actor Discovery Engine — the orchestrator.

One call per observation: takes the CanonicalPageModel the Perception Engine
just produced (plus lightweight session context: is this an authenticated
observation, and how did the session authenticate), extracts/classifies
actor and permission candidates, merges them into the registry, cross-
references the Entity Registry for entities touched on this page, records
role relationships (co-assignment + hierarchy-by-permission-comparison), and
emits a structured trace explaining WHAT was discovered and WHY.

Deterministic, read-only over its inputs, never touches the browser, never
calls an LLM — same discipline as the Perception Engine and the Entity
Discovery Engine it sits alongside.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.intelligence.actor_discovery.actor_classifier import ActorClassifier
from app.intelligence.actor_discovery.actor_registry import ActorRegistry
from app.intelligence.actor_discovery.role_relationships import RoleRelationshipBuilder
from app.utils.exploration_trace import record as trace_record
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from app.intelligence.entity_discovery import EntityRegistry
    from app.perception.models import CanonicalPageModel

logger = get_logger("intelligence.actor_discovery")


class ActorDiscoveryEngine:
    def __init__(self, registry: ActorRegistry | None = None) -> None:
        self.registry = registry or ActorRegistry()
        self.classifier = ActorClassifier()
        self.relationship_builder = RoleRelationshipBuilder()

    def observe(
        self,
        model: "CanonicalPageModel",
        *,
        iteration: int = 0,
        authenticated: bool = False,
        login_method: str | None = None,
        entity_registry: "EntityRegistry | None" = None,
    ) -> dict[str, Any]:
        discovery = self.classifier.classify(
            model,
            iteration=iteration,
            authenticated=authenticated,
            login_method=login_method,
            known_role_terms=frozenset(self.registry.records.keys()),
        )
        changes = self.registry.memory.merge_discovery(discovery, iteration=iteration)

        if entity_registry is not None:
            self._attach_entities(discovery.current_session_term, model.url, entity_registry)
            # known_entities changed after merge_discovery already computed
            # confidence — recompute so the richness bonus reflects it.
            record = self.registry.get(discovery.current_session_term)
            if record is not None:
                self.registry.memory.finalize(record)

        relationship_findings = self.relationship_builder.build_co_assignments(discovery, iteration=iteration)
        relationship_findings.extend(self.relationship_builder.infer_hierarchy(self.registry.all_actors()))
        new_relationships = self.registry.memory.merge_relationships(relationship_findings)

        summary = self._summarize(model, discovery, changes, new_relationships)
        self._trace(summary)
        return summary

    # -- entity cross-reference ---------------------------------------------

    def _attach_entities(self, session_term: str, page_url: str, entity_registry: "EntityRegistry") -> None:
        record = self.registry.get(session_term)
        if record is None:
            return
        for entity in entity_registry.all_entities():
            if page_url in entity.related_pages and entity.canonical_name not in record.known_entities:
                record.known_entities.append(entity.canonical_name)

    # -- tracing --------------------------------------------------------------

    def _summarize(
        self,
        model: "CanonicalPageModel",
        discovery: Any,
        changes: dict[str, list[str]],
        new_relationships: list[str],
    ) -> dict[str, Any]:
        touched = sorted(set(changes["created"]) | set(changes["updated"]))
        actor_details = []
        for term in touched:
            record = self.registry.records.get(term)
            if record is None:
                continue
            actor_details.append(
                {
                    "canonical_name": record.canonical_name,
                    "status": record.status,
                    "confidence": round(record.confidence, 3),
                    "aliases": record.aliases,
                    "permission_evidence": [p.to_summary_dict() for p in record.known_permissions],
                    "why": self._why(record),
                }
            )
        return {
            "url": model.url,
            "state_fingerprint": model.state_fingerprint,
            "session_actor": discovery.current_session_term,
            "created": changes["created"],
            "updated": changes["updated"],
            "new_relationships": new_relationships,
            "navigation_differences": [d.to_dict() for d in self.registry.all_pairwise_differences()],
            "actors": actor_details,
            "registry_totals": {
                "total": len(self.registry.records),
                "known": len(self.registry.known_actors()),
                "unverified": len(self.registry.unverified_actors()),
                "candidates": len(self.registry.unknown_actors()),
            },
        }

    @staticmethod
    def _why(record: Any) -> list[str]:
        seen_kinds: set[str] = set()
        why: list[str] = []
        for ev in record.supporting_evidence:
            if ev.source_kind in seen_kinds:
                continue
            seen_kinds.add(ev.source_kind)
            why.append(f"{ev.source_kind}: {ev.observed_text!r}")
            if len(why) >= 6:
                break
        return why

    @staticmethod
    def _trace(summary: dict[str, Any]) -> None:
        trace_record("actor_discovery.observation", **summary)
