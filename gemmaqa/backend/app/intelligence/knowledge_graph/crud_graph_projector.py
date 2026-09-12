"""CRUD Registry -> graph projection — list/form/record relationship
discovery (task section 6).

Reuses the SAME deterministic-id / upsert_node / upsert_edge primitives
`GraphSynchronizer` already uses for Entity/Actor/Workflow/Dependency
projection — this is a fifth, narrower projector for exactly one source:
`CRUDRegistry`. New edge types, all lowercase snake_case per this package's
existing convention (`has_step`, `acts_on`, ...):

- lists_entity            collection  -> entity
- opens_create_form       collection  -> form
- creates_entity          form        -> entity   (create hypotheses only)
- edits_entity            form        -> entity   (edit hypotheses only)
- deletes_entity          control     -> entity   (delete hypotheses only,
                                                      and ONLY once a hypothesis
                                                      has corroborating
                                                      evidence — never for a
                                                      bare "entry_point")
- returns_to_list         form        -> collection
- verifies_in_collection  form        -> collection
"""

from __future__ import annotations

from typing import Any

from app.intelligence.knowledge_graph.graph_edge_factory import edge_id, to_evidence_references
from app.intelligence.knowledge_graph.graph_node_factory import (
    collection_node_id,
    crud_control_node_id,
    crud_form_node_id,
    entity_node_id,
)
from app.intelligence.knowledge_graph.knowledge_graph_memory import KnowledgeGraphMemory
from app.intelligence.knowledge_graph.relationship_resolver import RelationshipResolver

_CRUD_REGISTRY = "crud_registry"

# A hypothesis with only entry-point evidence is a POSSIBLE control, not a
# proven relationship — CREATES/EDITS/DELETES edges require at least
# "supported" (a before/after pair corroborated it).
_MIN_STATUS_FOR_MUTATION_EDGE = {"supported", "executed", "verified"}


def sync_crud_hypotheses(
    memory: KnowledgeGraphMemory,
    resolver: RelationshipResolver,
    crud_registry: Any,
    iteration: int,
    seen_ids: set[str],
) -> None:
    for hyp in crud_registry.all_hypotheses():
        if not hyp.entity_hypothesis:
            continue
        entity_id = entity_node_id(hyp.entity_hypothesis)
        # Never invents an entity node — CRUD projection only links to an
        # entity Entity Discovery (or a prior CRUD observation) already
        # named; if it doesn't exist yet, defer rather than guess.
        entity_target = resolver.resolve(hyp.entity_hypothesis, "entity") or (
            entity_id if entity_id in memory.nodes else None
        )
        if entity_target is None:
            continue

        collection_target = None
        if hyp.collection_id:
            collection_target = collection_node_id(hyp.collection_id)
            seen_ids.add(collection_target)
            memory.upsert_node(
                collection_target, node_type="table", canonical_name=hyp.entity_hypothesis,
                status="observed", confidence=max(hyp.confidence, 0.3), source_registry=_CRUD_REGISTRY,
                source_record_id=hyp.collection_id, evidence_references=to_evidence_references(hyp.evidence, _CRUD_REGISTRY),
                iteration=iteration,
            )
            e_id = edge_id("lists_entity", collection_target, entity_target, discriminator=hyp.hypothesis_id[:8])
            seen_ids.add(e_id)
            memory.upsert_edge(
                e_id, edge_type="lists_entity", source_node_id=collection_target, target_node_id=entity_target,
                status="observed", confidence=max(hyp.confidence, 0.3), source_registry=_CRUD_REGISTRY,
                source_record_ids=[hyp.hypothesis_id], iteration=iteration,
            )

        form_target = None
        if hyp.required_form_id:
            form_target = crud_form_node_id(hyp.required_form_id)
            seen_ids.add(form_target)
            memory.upsert_node(
                form_target, node_type="form", canonical_name=f"{hyp.operation} form",
                status="observed", confidence=hyp.confidence, source_registry=_CRUD_REGISTRY,
                source_record_id=hyp.required_form_id, evidence_references=to_evidence_references(hyp.evidence, _CRUD_REGISTRY),
                attributes={"operation": hyp.operation, "safety_classification": hyp.safety_classification},
                iteration=iteration,
            )
            if collection_target is not None:
                e_id2 = edge_id("opens_create_form", collection_target, form_target, discriminator=hyp.hypothesis_id[:8])
                seen_ids.add(e_id2)
                memory.upsert_edge(
                    e_id2, edge_type="opens_create_form", source_node_id=collection_target, target_node_id=form_target,
                    status="observed", confidence=hyp.confidence, source_registry=_CRUD_REGISTRY,
                    source_record_ids=[hyp.hypothesis_id], iteration=iteration,
                )

        if hyp.status not in _MIN_STATUS_FOR_MUTATION_EDGE:
            continue

        mutation_edge_type = {"create": "creates_entity", "edit": "edits_entity", "delete": "deletes_entity"}[hyp.operation]
        mutation_source = form_target
        if mutation_source is None and hyp.required_controls:
            control_id = hyp.required_controls[0]
            mutation_source = crud_control_node_id(control_id)
            seen_ids.add(mutation_source)
            memory.upsert_node(
                mutation_source, node_type="unknown", canonical_name=f"{hyp.operation} control",
                status="observed", confidence=hyp.confidence, source_registry=_CRUD_REGISTRY,
                source_record_id=control_id, iteration=iteration,
            )
        if mutation_source is None:
            continue

        e_id3 = edge_id(mutation_edge_type, mutation_source, entity_target, discriminator=hyp.hypothesis_id[:8])
        seen_ids.add(e_id3)
        memory.upsert_edge(
            e_id3, edge_type=mutation_edge_type, source_node_id=mutation_source, target_node_id=entity_target,
            status="observed", confidence=hyp.confidence, source_registry=_CRUD_REGISTRY,
            source_record_ids=[hyp.hypothesis_id], evidence_references=to_evidence_references(hyp.evidence, _CRUD_REGISTRY),
            attributes={"safety_classification": hyp.safety_classification, "cleanup_possibility": hyp.cleanup_possibility},
            iteration=iteration,
        )

        if collection_target is not None and mutation_source is not None and hyp.operation in {"create", "edit"}:
            e_id4 = edge_id("returns_to_list", mutation_source, collection_target, discriminator=hyp.hypothesis_id[:8])
            seen_ids.add(e_id4)
            memory.upsert_edge(
                e_id4, edge_type="returns_to_list", source_node_id=mutation_source, target_node_id=collection_target,
                status="observed" if "list" in (hyp.expected_transition or "") else "inferred",
                confidence=hyp.confidence * 0.8, source_registry=_CRUD_REGISTRY,
                source_record_ids=[hyp.hypothesis_id], iteration=iteration,
            )
            e_id5 = edge_id("verifies_in_collection", mutation_source, collection_target, discriminator=hyp.hypothesis_id[:8])
            seen_ids.add(e_id5)
            memory.upsert_edge(
                e_id5, edge_type="verifies_in_collection", source_node_id=mutation_source, target_node_id=collection_target,
                status="observed" if hyp.verification_surface == hyp.collection_id else "inferred",
                confidence=hyp.confidence * 0.7, source_registry=_CRUD_REGISTRY,
                source_record_ids=[hyp.hypothesis_id], iteration=iteration,
            )
