"""Diffs the Knowledge Graph's and Scenario Planning's own already-computed
statistics from before to after an investigation -- never a new, separate
coverage/confidence subsystem. `KnowledgeUpdates`/`CoverageUpdates`/
`ConfidenceUpdates` are honest summaries of a diff, not new measurements.
"""

from __future__ import annotations

from app.intelligence.autonomous_investigation.schemas import ConfidenceUpdates, CoverageUpdates, KnowledgeUpdates


def build_knowledge_updates(investigation_id: str, *, before_graph_stats, after_graph_stats) -> KnowledgeUpdates:
    resolved_gaps = max(0, before_graph_stats.gap_count - after_graph_stats.gap_count)
    new_gaps = max(0, after_graph_stats.gap_count - before_graph_stats.gap_count)
    return KnowledgeUpdates(
        update_id=f"knowledge-update:{investigation_id}", investigation_id=investigation_id,
        graph_version_before=before_graph_stats.graph_version, graph_version_after=after_graph_stats.graph_version,
        node_count_before=before_graph_stats.total_nodes, node_count_after=after_graph_stats.total_nodes,
        edge_count_before=before_graph_stats.total_edges, edge_count_after=after_graph_stats.total_edges,
        resolved_gap_count=resolved_gaps, new_gap_count=new_gaps,
        new_contradiction_count=max(0, after_graph_stats.contradicted_edge_count - before_graph_stats.contradicted_edge_count),
        explanation=(
            f"graph_version {before_graph_stats.graph_version}->{after_graph_stats.graph_version}; "
            f"nodes {before_graph_stats.total_nodes}->{after_graph_stats.total_nodes}; gaps resolved={resolved_gaps}, new={new_gaps}"
        ),
    )


def build_coverage_updates(investigation_id: str, *, before_graph_stats, after_graph_stats, before_scenario_stats=None, after_scenario_stats=None) -> CoverageUpdates:
    feasible_before = before_scenario_stats.scenarios_by_feasibility.get("feasible", 0) if before_scenario_stats else 0
    feasible_after = after_scenario_stats.scenarios_by_feasibility.get("feasible", 0) if after_scenario_stats else 0
    return CoverageUpdates(
        update_id=f"coverage-update:{investigation_id}", investigation_id=investigation_id,
        gap_count_before=before_graph_stats.gap_count, gap_count_after=after_graph_stats.gap_count,
        feasible_scenario_count_before=feasible_before, feasible_scenario_count_after=feasible_after,
        explanation=f"open graph gaps {before_graph_stats.gap_count}->{after_graph_stats.gap_count}",
    )


def build_confidence_updates(investigation_id: str, *, before_graph_stats, after_graph_stats, before_scenario_stats=None, after_scenario_stats=None) -> ConfidenceUpdates:
    return ConfidenceUpdates(
        update_id=f"confidence-update:{investigation_id}", investigation_id=investigation_id,
        consistency_issue_count_before=before_graph_stats.consistency_issue_count,
        consistency_issue_count_after=after_graph_stats.consistency_issue_count,
        average_scenario_confidence_before=before_scenario_stats.average_confidence_gain if before_scenario_stats else 0.0,
        average_scenario_confidence_after=after_scenario_stats.average_confidence_gain if after_scenario_stats else 0.0,
        explanation=f"open consistency issues {before_graph_stats.consistency_issue_count}->{after_graph_stats.consistency_issue_count}",
    )
