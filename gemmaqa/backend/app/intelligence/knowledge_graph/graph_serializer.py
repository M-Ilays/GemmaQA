"""Graph serialisation — every export path here is bounded and safe: no
passwords/tokens/cookies/secret headers ever flow through (none of the
graph schemas carry them in the first place — evidence is always a
reference, `GraphEvidenceReference`, never a raw payload), and large
evidence objects are referenced by id, never duplicated inline.

Graphviz DOT export is dependency-free (plain string formatting) and never
a runtime requirement — nothing else in this package needs a `dot` binary
or the `graphviz` Python package.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.knowledge_graph.knowledge_graph_memory import KnowledgeGraphMemory
from app.intelligence.knowledge_graph.schemas import KnowledgeGraphSnapshot

MAX_DOT_NODES = 300
MAX_DOT_EDGES = 500


class GraphSerializer:
    def __init__(self, memory: KnowledgeGraphMemory) -> None:
        self.memory = memory

    def compact_snapshot(self) -> dict[str, Any]:
        """Node/edge counts + ids only -- cheap enough to include often."""
        return {
            "graph_version": self.memory.graph_version,
            "node_count": len(self.memory.nodes),
            "edge_count": len(self.memory.edges),
            "node_ids": sorted(self.memory.nodes.keys()),
            "edge_ids": sorted(self.memory.edges.keys()),
            "gap_count": len([g for g in self.memory.gaps.values() if g.status == "open"]),
            "consistency_issue_count": len([i for i in self.memory.consistency_issues.values() if i.status == "open"]),
        }

    def full_snapshot(self, statistics: dict[str, Any]) -> KnowledgeGraphSnapshot:
        return KnowledgeGraphSnapshot(
            graph_version=self.memory.graph_version,
            statistics=statistics,
            nodes=[n.model_dump(mode="json") for n in self.memory.nodes.values()],
            edges=[e.model_dump(mode="json") for e in self.memory.edges.values()],
            gaps=[g.model_dump(mode="json") for g in self.memory.gaps.values() if g.status == "open"],
            consistency_issues=[i.model_dump(mode="json") for i in self.memory.consistency_issues.values() if i.status == "open"],
        )

    def bounded_subgraph_export(self, node_ids: list[str], edge_ids: list[str]) -> dict[str, Any]:
        return {
            "nodes": [self.memory.nodes[n].model_dump(mode="json") for n in node_ids if n in self.memory.nodes],
            "edges": [self.memory.edges[e].model_dump(mode="json") for e in edge_ids if e in self.memory.edges],
        }

    def to_dot(self, *, node_ids: list[str] | None = None, edge_ids: list[str] | None = None) -> str:
        node_ids = node_ids if node_ids is not None else list(self.memory.nodes.keys())
        edge_ids = edge_ids if edge_ids is not None else list(self.memory.edges.keys())
        node_ids = node_ids[:MAX_DOT_NODES]
        edge_ids = edge_ids[:MAX_DOT_EDGES]
        node_id_set = set(node_ids)

        lines = ["digraph ApplicationKnowledgeGraph {", '  rankdir="LR";']
        for n_id in node_ids:
            node = self.memory.nodes.get(n_id)
            if node is None:
                continue
            label = _dot_escape(f"{node.canonical_name}\\n({node.node_type})")
            style = "dashed" if node.stale else "solid"
            lines.append(f'  "{_dot_escape(n_id)}" [label="{label}", style={style}];')
        for e_id in edge_ids:
            edge = self.memory.edges.get(e_id)
            if edge is None or edge.source_node_id not in node_id_set or edge.target_node_id not in node_id_set:
                continue
            style = "dotted" if edge.status == "inferred" else ("dashed" if edge.stale else "solid")
            lines.append(f'  "{_dot_escape(edge.source_node_id)}" -> "{_dot_escape(edge.target_node_id)}" [label="{_dot_escape(edge.edge_type)}", style={style}];')
        lines.append("}")
        return "\n".join(lines)

    def human_readable_summary(self, statistics: dict[str, Any]) -> str:
        lines = [
            f"Application Knowledge Graph — version {self.memory.graph_version}",
            f"  Nodes: {statistics.get('total_nodes', 0)} | Edges: {statistics.get('total_edges', 0)}",
            f"  Observed edges: {statistics.get('observed_edge_count', 0)} | Inferred: {statistics.get('inferred_edge_count', 0)} | Contradicted: {statistics.get('contradicted_edge_count', 0)} | Stale: {statistics.get('stale_edge_count', 0)}",
            f"  Unresolved references: {statistics.get('unresolved_reference_count', 0)}",
            f"  Consistency issues: {statistics.get('consistency_issue_count', 0)} | Gaps: {statistics.get('gap_count', 0)}",
            f"  Connected components: {statistics.get('connected_component_count', 0)} | Isolated nodes: {statistics.get('isolated_node_count', 0)}",
        ]
        node_counts = statistics.get("node_counts_by_type", {})
        if node_counts:
            lines.append("  Node types: " + ", ".join(f"{k}={v}" for k, v in sorted(node_counts.items())))
        return "\n".join(lines)


def _dot_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')
