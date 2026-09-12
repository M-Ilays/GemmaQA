"""Compact context projection — the input boundary for the Goal Generation
Engine, and (since the model-context upgrade) the central bounded projection
and relevance-ranking component for every model call.

Returns a BOUNDED slice of the graph around one focus node: neighbours,
relationships, confidence, status, evidence SUMMARIES (never full payloads),
contradictions, gaps, and unresolved references — never the complete graph,
never raw secrets, screenshots, or network bodies.

`project_model_context()` extends the same discipline from "a slice of the
graph" to "a slice of everything an engine knows". docs/MODEL_CONTEXT_AUDIT.md
identified this class as the only component in the repository already doing
bounded, secret-free, evidence-summarizing projection (section F, item 3) —
while never being wired to a prompt. Rather than build a second projector
alongside it, the existing one grew the responsibility.

It emits STRUCTURED DATA ONLY. No prose, no prompt text, no model-specific
phrasing: rendering stays in `app/gemma/prompts.py`, which is the single source
of prompt templates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.intelligence.knowledge_graph.graph_query_engine import GraphQueryEngine
from app.intelligence.knowledge_graph.knowledge_graph_memory import KnowledgeGraphMemory
from app.intelligence.knowledge_graph.schemas import GraphTraversalConstraint

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from app.gemma.model_context import ModelContext

DEFAULT_MAX_NEIGHBOURS = 20
DEFAULT_MAX_GAPS = 10
DEFAULT_MAX_EVIDENCE = 5
DEFAULT_DEPTH = 1

# Model-context projection caps. Separate from the graph-slice caps above
# because they bound a different thing: how much of the whole run's knowledge
# reaches one prompt, not how far a graph traversal walks.
MODEL_MAX_GRAPH_NODES = 12
MODEL_MAX_GRAPH_EDGES = 15
MODEL_MAX_GAPS = 8
MODEL_MAX_CONTRADICTIONS = 5
MODEL_MAX_WORKFLOW_HYPOTHESES = 8
MODEL_MAX_RECENT_ACTIONS = 12

# Below this confidence a hypothesis is ranked last: it is worth telling the
# model that an uncertainty exists, but never at the cost of a confirmed fact.
LOW_CONFIDENCE_THRESHOLD = 0.4


class GraphContextProjector:
    def __init__(self, memory: KnowledgeGraphMemory, query_engine: GraphQueryEngine) -> None:
        self.memory = memory
        self.query = query_engine

    def context_for_node(
        self,
        node_id: str,
        *,
        depth: int = DEFAULT_DEPTH,
        max_neighbours: int = DEFAULT_MAX_NEIGHBOURS,
        max_gaps: int = DEFAULT_MAX_GAPS,
        max_evidence: int = DEFAULT_MAX_EVIDENCE,
    ) -> dict[str, Any]:
        node = self.memory.nodes.get(node_id)
        if node is None:
            return {"focus_node_id": node_id, "found": False, "graph_version": self.memory.graph_version}

        constraint = GraphTraversalConstraint(max_depth=depth, max_results=max_neighbours + 1)
        neighbourhood = self.query.neighbourhood(node_id, constraint=constraint)

        neighbour_ids = [n for n in neighbourhood.node_ids if n != node_id][:max_neighbours]
        relationships = [self._edge_summary(e) for e in neighbourhood.edge_ids]

        contradictions = [self._contradiction_summary(c) for c in self.memory.contradictions.values() if node_id in c.node_ids]
        gaps = [self._gap_summary(g) for g in self.memory.gaps.values() if node_id in g.node_ids and g.status == "open"][:max_gaps]
        unresolved = [r.target_hint for r in self.memory.unresolved_references() if r.source_node_id == node_id]

        return {
            "focus_node": self._node_summary(node),
            "neighbours": [self._node_summary(self.memory.nodes[n]) for n in neighbour_ids if n in self.memory.nodes],
            "relationships": relationships,
            "evidence_summary": self._evidence_summary(node, max_evidence),
            "contradictions": contradictions,
            "gaps": gaps,
            "unresolved_references": unresolved,
            "graph_version": self.memory.graph_version,
            "truncated": neighbourhood.truncated,
        }

    def context_for_entity(self, entity_id: str, **kwargs) -> dict[str, Any]:
        return self.context_for_node(entity_id, **kwargs)

    def context_for_actor(self, actor_id: str, **kwargs) -> dict[str, Any]:
        return self.context_for_node(actor_id, **kwargs)

    def context_for_workflow(self, workflow_id: str, **kwargs) -> dict[str, Any]:
        return self.context_for_node(workflow_id, **kwargs)

    def context_for_output(self, output_id: str, **kwargs) -> dict[str, Any]:
        return self.context_for_node(output_id, **kwargs)

    # -- summaries (bounded, secret-free) ----------------------------------------

    @staticmethod
    def _node_summary(node) -> dict[str, Any]:
        return {
            "node_id": node.node_id, "node_type": node.node_type, "canonical_name": node.canonical_name,
            "status": node.status, "confidence": round(node.confidence, 3), "stale": node.stale,
        }

    def _edge_summary(self, edge_id: str) -> dict[str, Any]:
        edge = self.memory.edges.get(edge_id)
        if edge is None:
            return {}
        return {
            "edge_id": edge.edge_id, "edge_type": edge.edge_type, "source_node_id": edge.source_node_id,
            "target_node_id": edge.target_node_id, "status": edge.status, "confidence": round(edge.confidence, 3),
            "stale": edge.stale,
        }

    @staticmethod
    def _contradiction_summary(contradiction) -> dict[str, Any]:
        return {"contradiction_id": contradiction.contradiction_id, "description": contradiction.description}

    @staticmethod
    def _gap_summary(gap) -> dict[str, Any]:
        return {
            "gap_id": gap.gap_id, "gap_type": gap.gap_type, "description": gap.description,
            "exploration_value": round(gap.exploration_value, 3), "risk": gap.risk,
        }

    @staticmethod
    def _evidence_summary(node, limit: int) -> list[str]:
        return [f"{ev.source_kind}: {ev.observed_text[:80]}" for ev in node.evidence_references[:limit]]


# ---------------------------------------------------------------------------
# Model-context projection
# ---------------------------------------------------------------------------
#
# Deliberately module-level functions rather than methods: a projection must be
# producible for a run that has no knowledge graph at all (early iterations,
# graph disabled, engine mid-build). `project_model_context` uses a live
# projector when one exists and degrades cleanly when it does not — the graph
# is one input among several, never a precondition.


def _dedupe(items: list[dict[str, Any]], *key_fields: str) -> list[dict[str, Any]]:
    """Drop repeated facts. Two records describing the same thing waste budget
    and invite the model to treat one fact as corroboration of itself."""
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = tuple(str(item.get(f, "")) for f in key_fields) if key_fields else (str(sorted(item.items())),)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _relevance(item: dict[str, Any]) -> float:
    """Rank by how much acting on this would change what the run knows.

    Higher exploration value and higher confidence rank first; an explicitly
    low-confidence record is pushed to the tail rather than dropped, because
    "we are unsure about X" is itself actionable for a QA agent.
    """
    value = item.get("exploration_value")
    confidence = item.get("confidence")
    score = 0.0
    if isinstance(value, (int, float)):
        score += float(value)
    if isinstance(confidence, (int, float)):
        score += float(confidence) if float(confidence) >= LOW_CONFIDENCE_THRESHOLD else -0.5
    if item.get("risk") in {"high", "critical"}:
        score += 0.25
    return score


def project_model_context(
    context: "ModelContext",
    *,
    projector: GraphContextProjector | None = None,
    max_graph_nodes: int = MODEL_MAX_GRAPH_NODES,
    max_gaps: int = MODEL_MAX_GAPS,
) -> dict[str, Any]:
    """Turn a normalized `ModelContext` aggregate into the bounded, ranked,
    deduplicated structure a prompt renders.

    The return shape is stable and section-oriented so
    `app/gemma/prompts.py` can map sections directly onto reduction priorities
    (see `app/gemma/context_budget.py`).
    """
    task_context: dict[str, Any] = {}
    if context.testing_objective_provided and context.testing_objective:
        task_context["operator_testing_objective"] = context.testing_objective[:1000]
    else:
        # An absent objective is stated as absent. Inventing a generic one would
        # be indistinguishable, to the model, from an operator who asked for it.
        task_context["operator_testing_objective"] = None
        task_context["objective_status"] = "not_provided_by_operator"
    if context.active_goal:
        task_context["active_goal"] = context.active_goal
    if context.active_scenario:
        task_context["active_scenario"] = context.active_scenario
    if context.current_step:
        task_context["current_step"] = context.current_step
    if context.qa_strategy:
        task_context["qa_strategy"] = context.qa_strategy
    if context.investigation_state:
        task_context["investigation_state"] = context.investigation_state
    task_context["mode"] = context.mode

    page_context = dict(context.page)
    if context.readiness:
        page_context["readiness"] = context.readiness

    engine_context: dict[str, Any] = {}
    contradictions = _dedupe(context.contradictions, "contradiction_type", "description")[
        :MODEL_MAX_CONTRADICTIONS
    ]
    if contradictions:
        engine_context["unresolved_contradictions"] = contradictions

    gaps = _dedupe(context.coverage_gaps, "gap_id", "description")
    gaps.sort(key=_relevance, reverse=True)
    if gaps:
        # Exact gap detail, never a bare count — audit finding J-8.
        engine_context["coverage_gaps"] = gaps[:max_gaps]

    hypotheses = _dedupe(context.workflow_hypotheses, "name")
    hypotheses.sort(key=_relevance, reverse=True)
    if hypotheses:
        engine_context["workflow_hypotheses"] = hypotheses[:MODEL_MAX_WORKFLOW_HYPOTHESES]

    if context.engine_diagnostics:
        engine_context["diagnostics"] = context.engine_diagnostics

    graph_context: dict[str, Any] = {}
    active_projector = projector
    if active_projector is None and context.knowledge_graph is not None:
        active_projector = getattr(context.knowledge_graph, "context_projector", None)
    if active_projector is not None and context.graph_focus_node_ids:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        for node_id in context.graph_focus_node_ids[:3]:
            try:
                slice_ = active_projector.context_for_node(node_id, max_neighbours=max_graph_nodes)
            except Exception:  # pragma: no cover - defensive
                continue
            if not slice_ or slice_.get("found") is False:
                continue
            focus = slice_.get("focus_node")
            if focus:
                nodes.append(focus)
            nodes.extend(slice_.get("neighbours") or [])
            edges.extend(e for e in (slice_.get("relationships") or []) if e)
        if nodes:
            graph_context["nodes"] = _dedupe(nodes, "node_id")[:max_graph_nodes]
        if edges:
            graph_context["relationships"] = _dedupe(
                edges, "source_node_id", "target_node_id", "edge_type"
            )[:MODEL_MAX_GRAPH_EDGES]
    if context.knowledge_graph is not None and not graph_context:
        # No focus node yet — still tell the model the graph exists and how big
        # it is, so "no relationships shown" is not read as "none exist".
        try:
            stats = context.knowledge_graph.statistics()
            graph_context["summary"] = {
                "graph_version": stats.graph_version,
                "total_nodes": stats.total_nodes,
                "total_edges": stats.total_edges,
            }
        except Exception:  # pragma: no cover - defensive
            pass

    constraints = dict(context.constraints)
    constraints.update(context.budgets)

    included = [
        name
        for name, value in (
            ("task_context", task_context),
            ("page_context", page_context),
            ("engine_context", engine_context),
            ("graph_context", graph_context),
            ("technical_evidence", context.technical_evidence),
            ("constraints", constraints),
            ("evidence_registry", context.evidence.ids),
        )
        if value
    ]

    return {
        "task_context": task_context,
        "page_context": page_context,
        "engine_context": engine_context,
        "graph_context": graph_context,
        "technical_evidence": dict(context.technical_evidence),
        "application_memory": dict(context.application_memory),
        "constraints": constraints,
        "evidence_registry": context.evidence.to_payload(),
        "recent_actions": list(context.recent_actions)[-MODEL_MAX_RECENT_ACTIONS:],
        "visited_states": list(context.visited_states),
        "unexplored_navigation": list(context.unexplored),
        "blocked_reasons": list(context.blocked_reasons),
        "context_provenance": context.provenance_payload(),
        "projection_metadata": {
            "included_sections": included,
            "reduced_sections": [],
            "omitted_sections": [],
            "estimated_tokens": 0,
            "page_source": context.page_source or page_context.get("source") or "unknown",
            "engine_outputs_considered": len(context.provenance),
            "graph_nodes_projected": len(graph_context.get("nodes") or []),
            "graph_edges_projected": len(graph_context.get("relationships") or []),
            "gaps_included": len(engine_context.get("coverage_gaps") or []),
            "evidence_items": len(context.evidence),
        },
    }
