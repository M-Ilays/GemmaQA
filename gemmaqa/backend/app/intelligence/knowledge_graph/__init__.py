"""Application Knowledge Graph.

Unifies the Entity/Actor/Workflow/Dependency Registries into one evidence-
backed, queryable, directed typed multigraph — the semantic memory layer
future Goal Generation, Scenario Planning, QA Strategy, and Autonomous
Investigation engines will consume. Contains NO application-specific role/
entity/workflow/KPI/module name; every name comes from the observed
application's own already-discovered registry records.

This is a PROJECTION, never a rediscovery: it consumes registry records and
links them into graph nodes/edges. It does not independently re-derive
entities/actors/workflows/outputs from raw page observations, and it never
mutates the source registries.

This milestone implements graph representation, synchronisation, bounded
deterministic inference, contradiction/consistency tracking, gap analysis,
bounded query/traversal, and compact context projection ONLY. It does NOT
implement goal generation, scenario planning, QA strategy selection,
automatic actor switching, or autonomous browser execution — see
docs/APPLICATION_KNOWLEDGE_GRAPH.md.
"""

from app.intelligence.knowledge_graph.knowledge_graph import ApplicationKnowledgeGraph
from app.intelligence.knowledge_graph.schemas import KnowledgeEdge, KnowledgeNode

__all__ = ["ApplicationKnowledgeGraph", "KnowledgeNode", "KnowledgeEdge"]
