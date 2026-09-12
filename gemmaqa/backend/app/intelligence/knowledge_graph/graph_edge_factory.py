"""Deterministic edge identity + generic evidence conversion.

`edge_id()` is a pure formatter: the same (edge_type, source, target,
discriminator) always yields the same id, so re-synchronising the same
registry state never creates a duplicate edge — it upserts the existing
one. `discriminator` exists because this is a MULTIGRAPH: two nodes may
have several distinct edges of possibly the same type (e.g. a workflow
step "acts_on" two different entities) — pass the natural key that
distinguishes them (a step_id, a state label, a role) whenever more than
one such edge could otherwise collide.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.knowledge_graph.schemas import GraphEvidenceReference


def edge_id(edge_type: str, source_node_id: str, target_node_id: str, discriminator: str = "") -> str:
    suffix = f":{discriminator}" if discriminator else ""
    return f"edge:{edge_type}:{source_node_id}->{target_node_id}{suffix}"


def to_evidence_reference(source_evidence: Any, source_registry: str) -> GraphEvidenceReference:
    kind = getattr(source_evidence, "source_kind", "") or ""
    return GraphEvidenceReference(
        source_kind=f"{source_registry}:{kind}" if kind else source_registry,
        observed_text=getattr(source_evidence, "observed_text", "") or "",
        page_url=getattr(source_evidence, "page_url", "") or "",
        element_id=getattr(source_evidence, "element_id", None),
    )


def to_evidence_references(source_evidence_list: list[Any] | None, source_registry: str) -> list[GraphEvidenceReference]:
    return [to_evidence_reference(ev, source_registry) for ev in (source_evidence_list or [])]
