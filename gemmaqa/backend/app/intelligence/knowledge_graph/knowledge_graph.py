"""ApplicationKnowledgeGraph — the orchestrator.

Wires together synchronisation, bounded inference, consistency checking,
gap analysis, bounded query/traversal, context projection, and
serialisation into one class. This is the ONLY class other GemmaQA code
(`RunMemory`, the controller) talks to — everything else in this package
is an internal collaborator.

One `synchronize()` call runs the FULL pipeline (registries -> nodes/edges
-> inference -> consistency -> gaps) as a single version-tracked pass, so
`graph_version` increments at most once per call, only when something
actually changed anywhere in that pipeline.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Any

from app.intelligence.knowledge_graph.graph_consistency_checker import GraphConsistencyChecker
from app.intelligence.knowledge_graph.graph_context_projector import GraphContextProjector
from app.intelligence.knowledge_graph.graph_gap_analyzer import GraphGapAnalyzer
from app.intelligence.knowledge_graph.graph_inference_engine import GraphInferenceEngine
from app.intelligence.knowledge_graph.graph_query_engine import GraphQueryEngine
from app.intelligence.knowledge_graph.graph_serializer import GraphSerializer
from app.intelligence.knowledge_graph.graph_synchronizer import GraphSynchronizer
from app.intelligence.knowledge_graph.knowledge_graph_memory import KnowledgeGraphMemory
from app.intelligence.knowledge_graph.schemas import GraphQuery, GraphQueryResult, GraphStatistics, GraphTraversalConstraint, GraphVersion
from app.utils.exploration_trace import record as trace_record
from app.utils.logging import get_logger

logger = get_logger("intelligence.knowledge_graph")


class ApplicationKnowledgeGraph:
    def __init__(self, memory: KnowledgeGraphMemory | None = None) -> None:
        self.memory = memory or KnowledgeGraphMemory()
        self.synchronizer = GraphSynchronizer(self.memory)
        self.inference_engine = GraphInferenceEngine(self.memory)
        self.consistency_checker = GraphConsistencyChecker(self.memory)
        self.gap_analyzer = GraphGapAnalyzer(self.memory)
        self.query_engine = GraphQueryEngine(self.memory)
        self.context_projector = GraphContextProjector(self.memory, self.query_engine)
        self.serializer = GraphSerializer(self.memory)
        self.source_registry_versions: dict[str, int] = {}

    # -- the full pipeline --------------------------------------------------------

    def synchronize(
        self,
        *,
        entity_registry: Any = None,
        actor_registry: Any = None,
        workflow_registry: Any = None,
        dependency_registry: Any = None,
        crud_registry: Any = None,
        iteration: int = 0,
    ) -> dict[str, Any]:
        self.memory.begin_pass()

        sync_summary = self.synchronizer.synchronize(
            entity_registry=entity_registry, actor_registry=actor_registry,
            workflow_registry=workflow_registry, dependency_registry=dependency_registry,
            crud_registry=crud_registry, iteration=iteration,
        )
        inference_counts = self.inference_engine.run_all(iteration=iteration)
        consistency_counts = self.consistency_checker.check_all()
        gaps = self.gap_analyzer.analyze()
        self.memory.close_gaps_not_in({g.gap_id for g in gaps})

        version_bumped = self.memory.end_pass()
        version_record = None
        if version_bumped:
            version_record = self._record_version(iteration=iteration)

        summary = {
            "graph_version": self.memory.graph_version,
            "version_bumped": version_bumped,
            "sync": sync_summary,
            "inference_counts": inference_counts,
            "consistency_counts": consistency_counts,
            "gap_count": len(gaps),
        }
        trace_record("knowledge_graph.synchronization", **{k: v for k, v in summary.items() if k != "sync"})
        return summary

    def _record_version(self, *, iteration: int) -> GraphVersion:
        m = self.memory
        version = GraphVersion(
            graph_version=m.graph_version, source_registry_versions=dict(self.source_registry_versions),
            synchronized_at=datetime.utcnow(),
            change_summary=f"+{len(m.added_node_ids)}n +{len(m.added_edge_ids)}e ~{len(m.updated_node_ids)}n ~{len(m.updated_edge_ids)}e stale:{len(m.stale_node_ids)}n/{len(m.stale_edge_ids)}e",
            added_node_ids=list(m.added_node_ids), updated_node_ids=list(m.updated_node_ids), stale_node_ids=list(m.stale_node_ids),
            added_edge_ids=list(m.added_edge_ids), updated_edge_ids=list(m.updated_edge_ids), stale_edge_ids=list(m.stale_edge_ids),
            resolved_reference_ids=list(m.resolved_reference_ids), new_contradiction_ids=list(m.new_contradiction_ids),
            resolved_contradiction_ids=[], new_gap_ids=list(m.new_gap_ids), resolved_gap_ids=list(m.resolved_gap_ids_this_pass),
        )
        m.version_history.append(version)
        return version

    # -- statistics / snapshot ----------------------------------------------------

    def statistics(self) -> GraphStatistics:
        node_counts: dict[str, int] = {}
        for n in self.memory.nodes.values():
            if n.stale:
                continue
            node_counts[n.node_type] = node_counts.get(n.node_type, 0) + 1

        edge_counts: dict[str, int] = {}
        observed = inferred = contradicted = stale_edges = 0
        for e in self.memory.edges.values():
            if e.stale:
                stale_edges += 1
                continue
            edge_counts[e.edge_type] = edge_counts.get(e.edge_type, 0) + 1
            if e.status == "inferred":
                inferred += 1
            elif e.status == "contradicted":
                contradicted += 1
            else:
                observed += 1

        components = self.gap_analyzer.connected_components()
        isolated = len(self.query_engine.isolated_nodes())

        return GraphStatistics(
            total_nodes=sum(node_counts.values()), total_edges=sum(edge_counts.values()),
            node_counts_by_type=node_counts, edge_counts_by_type=edge_counts,
            observed_edge_count=observed, inferred_edge_count=inferred, contradicted_edge_count=contradicted,
            stale_edge_count=stale_edges, unresolved_reference_count=len(self.memory.unresolved_references()),
            consistency_issue_count=len([i for i in self.memory.consistency_issues.values() if i.status == "open"]),
            gap_count=len([g for g in self.memory.gaps.values() if g.status == "open"]),
            connected_component_count=len(components), isolated_node_count=isolated, graph_version=self.memory.graph_version,
        )

    def snapshot(self) -> dict[str, Any]:
        return self.serializer.full_snapshot(self.statistics().model_dump()).model_dump(mode="json")

    def compact_snapshot(self) -> dict[str, Any]:
        return self.serializer.compact_snapshot()

    def gaps(self) -> list[Any]:
        return [g for g in self.memory.gaps.values() if g.status == "open"]

    def consistency_issues(self) -> list[Any]:
        return [i for i in self.memory.consistency_issues.values() if i.status == "open"]

    # -- query dispatch -------------------------------------------------------------

    def query(self, query: GraphQuery) -> GraphQueryResult:
        method = getattr(self.query_engine, query.query_type, None)
        if method is None:
            return GraphQueryResult(query_type=query.query_type, node_ids=[], edge_ids=[])
        accepts_constraint = "constraint" in inspect.signature(method).parameters
        kwargs = dict(query.parameters)
        if accepts_constraint:
            kwargs["constraint"] = query.constraint
        result = method(**kwargs)
        node_ids: list[str] = []
        edge_ids: list[str] = []
        if isinstance(result, list):
            for item in result:
                if hasattr(item, "node_id"):
                    node_ids.append(item.node_id)
                elif hasattr(item, "edge_id"):
                    edge_ids.append(item.edge_id)
        elif hasattr(result, "node_ids"):
            node_ids = list(result.node_ids)
            edge_ids = list(getattr(result, "edge_ids", []))
        constraint_limit = query.constraint.max_results
        truncated = len(node_ids) > constraint_limit
        return GraphQueryResult(
            query_type=query.query_type, node_ids=node_ids[:constraint_limit], edge_ids=edge_ids[:constraint_limit],
            truncated=truncated, total_before_truncation=len(node_ids),
        )

    # -- context projection ---------------------------------------------------------

    def context_for_node(self, node_id: str, **kwargs) -> dict[str, Any]:
        return self.context_projector.context_for_node(node_id, **kwargs)

    def context_for_entity(self, entity_id: str, **kwargs) -> dict[str, Any]:
        from app.intelligence.knowledge_graph.graph_node_factory import entity_node_id
        return self.context_projector.context_for_node(entity_node_id(entity_id), **kwargs)

    def context_for_actor(self, actor_id: str, **kwargs) -> dict[str, Any]:
        from app.intelligence.knowledge_graph.graph_node_factory import actor_node_id
        return self.context_projector.context_for_node(actor_node_id(actor_id), **kwargs)

    def context_for_workflow(self, workflow_id: str, **kwargs) -> dict[str, Any]:
        from app.intelligence.knowledge_graph.graph_node_factory import workflow_node_id
        return self.context_projector.context_for_node(workflow_node_id(workflow_id), **kwargs)

    def context_for_output(self, output_id: str, **kwargs) -> dict[str, Any]:
        from app.intelligence.knowledge_graph.graph_node_factory import derived_output_node_id
        node_id = output_id if output_id.startswith("derived_output:") else derived_output_node_id(output_id)
        return self.context_projector.context_for_node(node_id, **kwargs)

    # -- versioning / diff ------------------------------------------------------------

    def changes_since(self, version: int) -> dict[str, Any]:
        relevant = [v for v in self.memory.version_history if v.graph_version > version]
        return self._merge_versions(relevant)

    def diff_snapshots(self, old_version: int, new_version: int) -> dict[str, Any]:
        relevant = [v for v in self.memory.version_history if old_version < v.graph_version <= new_version]
        return self._merge_versions(relevant)

    @staticmethod
    def _merge_versions(versions: list[GraphVersion]) -> dict[str, Any]:
        merged: dict[str, list[str]] = {
            "added_node_ids": [], "updated_node_ids": [], "stale_node_ids": [],
            "added_edge_ids": [], "updated_edge_ids": [], "stale_edge_ids": [],
            "resolved_reference_ids": [], "new_contradiction_ids": [], "resolved_contradiction_ids": [],
            "new_gap_ids": [], "resolved_gap_ids": [],
        }
        for v in versions:
            for key in merged:
                merged[key] = list(dict.fromkeys([*merged[key], *getattr(v, key)]))
        merged["from_version"] = versions[0].graph_version - 1 if versions else None
        merged["to_version"] = versions[-1].graph_version if versions else None
        return merged
