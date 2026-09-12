"""Scans the Application Knowledge Graph and produces `GoalCandidate`
records -- one per distinct investigation. Never rediscovers application
knowledge: every candidate traces back to a KG node, edge, gap,
contradiction, consistency issue, or unresolved reference.

Deduplication happens WHILE scanning, not as an afterthought: every
candidate is keyed by a deterministic `subject_key` (a node, an edge, a
gap, a contradiction, a reference, or an inference rule id), and any
signal that concerns the SAME subject is merged into the SAME candidate's
evidence rather than spawning a second one. This is what satisfies "do not
create multiple goals representing the same investigation" -- a workflow
missing a trigger and that same workflow having low confidence both
enrich exactly one `verify_workflow` candidate, they never produce two.

The builder only decides WHAT the investigation is; it does not score it
(see `goal_priority.py`) or relate it to other goals (see
`goal_dependency_resolver.py` / `goal_grouping.py`).
"""

from __future__ import annotations

from app.intelligence.goal_generation.schemas import GoalCandidate, GoalEvidence

# -- node-type -> goal_type routing (Step 1: one candidate per eligible node) --

_NODE_TYPE_TO_GOAL_TYPE: dict[str, str] = {
    "entity": "verify_entity_lifecycle",
    "entity_state": "verify_state_transition",
    "workflow": "verify_workflow",
    "workflow_branch": "validate_workflow_branch",
    "prerequisite": "validate_prerequisite",
    "permission": "verify_permission",
    "operation": "verify_actor_capability",
    "actor": "verify_actor_capability",
    "counter": "verify_kpi",
    "badge": "validate_dashboard_output",
    "chart": "validate_dashboard_output",
    "queue": "validate_dashboard_output",
    "derived_output": "validate_dashboard_output",
    "report": "validate_report",
    "notification": "validate_notification",
    "alert": "validate_notification",
}

_AGGREGATION_EDGE_TYPES = {"counts", "sums", "averages", "groups_by", "filters_by"}
_OUTPUT_NODE_TYPES = {"derived_output", "counter", "badge", "chart", "report", "queue", "notification", "alert"}
_DEPENDENCY_EDGE_TYPES = {
    "counts", "sums", "averages", "groups_by", "filters_by", "includes_state", "excludes_state",
    "increments", "decrements", "recalculates", "populates", "removes_from", "affects", "depends_on",
    "contributes_to",
}

# -- gap_type routing (Step 2) --------------------------------------------------

_GAP_SKIP = {"unresolved_identity", "unresolved_reference", "contradictory_relationship"}
# NOTE: "isolated_node" is intentionally NOT standalone -- its node_ids is a
# single owning node, so it merges into that node's own Step-1 candidate
# just like any other per-node gap (a zero-relationship entity is still
# investigated as "verify this entity", not as a separate generic gap
# goal). Only gaps spanning a whole disconnected REGION (no single owner)
# go standalone here.
_GAP_STANDALONE_RESOLVE = {"stale_subgraph", "disconnected_module"}
_GAP_STANDALONE_HANDOFF = {"unresolved_actor_hand_off"}
_GAP_LOW_CONFIDENCE = {"low_confidence_bridge"}

_LOW_CONFIDENCE_EDGE_THRESHOLD = 0.4
_LOW_CONFIDENCE_EDGE_TYPES = {
    "acts_on", "performed_by", "participates_in", "produces", "has_state", "transitions", "can_perform",
    "has_permission", "generated_by", "visible_to", "requires", "triggered_by", "branches_to",
    *_DEPENDENCY_EDGE_TYPES,
}
MAX_LOW_CONFIDENCE_EDGE_CANDIDATES = 25


def _goal_type_for_node(node, memory) -> str | None:
    if node.node_type in _OUTPUT_NODE_TYPES:
        incoming = [memory.edges[e] for e in memory.incoming_edge_ids(node.node_id) if e in memory.edges and not memory.edges[e].stale]
        if any(e.edge_type in _AGGREGATION_EDGE_TYPES for e in incoming):
            return "validate_aggregation_rule"
    return _NODE_TYPE_TO_GOAL_TYPE.get(node.node_type)


class GoalCandidateBuilder:
    def __init__(self, graph) -> None:
        self.graph = graph
        self.memory = graph.memory
        self.query = graph.query_engine
        self._candidates: dict[str, GoalCandidate] = {}
        self._truncated_low_confidence = False

    def build(self) -> list[GoalCandidate]:
        self._candidates = {}
        self._truncated_low_confidence = False
        self._build_node_candidates()
        self._apply_gaps()
        self._apply_unresolved_references()
        self._apply_contradictions()
        self._apply_consistency_issues()
        self._apply_low_confidence_edges()
        self._apply_dependency_edges()
        self._apply_inferred_relationships()
        self._apply_business_rule_ownership_scope_edges()
        self._prune_clean_verified()
        return [self._candidates[k] for k in sorted(self._candidates.keys())]

    @property
    def truncated_low_confidence(self) -> bool:
        return self._truncated_low_confidence

    # -- Step 1: one candidate per eligible node ---------------------------------

    def _build_node_candidates(self) -> None:
        for node_id in sorted(self.memory.nodes.keys()):
            node = self.memory.nodes[node_id]
            if node.stale:
                continue
            goal_type = _goal_type_for_node(node, self.memory)
            if goal_type is None:
                continue
            subject_key = f"{goal_type}:{node_id}"
            candidate = GoalCandidate(
                subject_key=subject_key, goal_type=goal_type,
                title=f"{_TITLE_VERB.get(goal_type, 'Verify')} '{node.canonical_name}'",
                description=f"Confirm the current understanding of {node.node_type} '{node.canonical_name}' against live application behaviour.",
                supporting_graph_nodes=[node_id], source_confidence=node.confidence,
                already_verified=(node.status == "verified"),
            )
            self._populate_required(candidate, node_id)
            if node.node_type == "prerequisite" and node.attributes.get("satisfied") is False:
                # The gap analyzer's `prerequisite_without_satisfaction_path`
                # only fires when NOTHING requires this prerequisite (an
                # orphaned node) -- a prerequisite a workflow correctly
                # references is, by the graph's own definition, not
                # "gapped". But "known to be unsatisfied" is itself
                # investigation-worthy evidence regardless of gap status.
                candidate.supporting_evidence.append(
                    GoalEvidence(evidence_type="unverified_node", source_id=node_id, description=f"Prerequisite '{node.canonical_name}' is currently unsatisfied.", confidence=node.confidence)
                )
            self._candidates[subject_key] = candidate

    # -- Step 2: gaps merge into (or stand alone from) node candidates -----------

    def _apply_gaps(self) -> None:
        for gap_id in sorted(self.memory.gaps.keys()):
            gap = self.memory.gaps[gap_id]
            if gap.status != "open":
                continue
            if gap.gap_type in _GAP_SKIP:
                continue

            evidence = GoalEvidence(evidence_type="gap", source_id=gap.gap_id, description=gap.description, confidence=gap.confidence)

            if gap.gap_type in _GAP_LOW_CONFIDENCE and gap.edge_ids:
                self._merge_edge_evidence(gap.edge_ids[0], "increase_confidence", evidence, gap_id=gap.gap_id, blocking=(gap.risk == "high" or gap.blocking))
                continue
            if gap.gap_type in _GAP_STANDALONE_HANDOFF:
                self._standalone(f"validate_actor_hand_off:gap:{gap_id}", "validate_actor_hand_off", f"Validate actor hand-off ({gap.description})", evidence, gap.node_ids, gap_id=gap_id, blocking=gap.blocking)
                continue
            if gap.gap_type in _GAP_STANDALONE_RESOLVE:
                self._standalone(f"resolve_graph_gap:gap:{gap_id}", "resolve_graph_gap", f"Resolve graph gap: {gap.description}", evidence, gap.node_ids, gap_id=gap_id, blocking=gap.blocking)
                continue

            owner = self.memory.nodes.get(gap.node_ids[0]) if gap.node_ids else None
            goal_type = _goal_type_for_node(owner, self.memory) if owner is not None else None
            if owner is not None and goal_type is not None:
                subject_key = f"{goal_type}:{owner.node_id}"
                candidate = self._candidates.get(subject_key)
                if candidate is not None:
                    candidate.supporting_evidence.append(evidence)
                    candidate.source_gap_ids.append(gap_id)
                    if gap.risk == "high" or gap.blocking:
                        candidate.blocking_gaps.append(gap_id)
                    continue
            # Owning node isn't (or is no longer) a tracked candidate -- don't
            # drop the signal, represent it as its own generic gap-resolution goal.
            self._standalone(f"resolve_graph_gap:gap:{gap_id}", "resolve_graph_gap", f"Resolve graph gap: {gap.description}", evidence, gap.node_ids, gap_id=gap_id, blocking=gap.blocking)

    # -- Step 3: unresolved references -------------------------------------------

    def _apply_unresolved_references(self) -> None:
        for ref in sorted(self.memory.unresolved_references(), key=lambda r: r.reference_id):
            evidence = GoalEvidence(evidence_type="unresolved_reference", source_id=ref.reference_id, description=f"Reference to '{ref.target_hint}' ({ref.edge_type}) is unresolved: {ref.reason}")
            subject_key = f"resolve_unresolved_reference:reference:{ref.reference_id}"
            candidate = GoalCandidate(
                subject_key=subject_key, goal_type="resolve_unresolved_reference",
                title=f"Resolve unresolved reference to '{ref.target_hint}'",
                description=f"A '{ref.edge_type}' relationship from '{ref.source_node_id}' names '{ref.target_hint}', which the graph has not yet confirmed.",
                supporting_graph_nodes=[ref.source_node_id] if ref.source_node_id in self.memory.nodes else [],
                supporting_evidence=[evidence], source_reference_ids=[ref.reference_id], source_confidence=0.2,
            )
            self._candidates[subject_key] = candidate

    # -- Step 4: contradictions ---------------------------------------------------

    def _apply_contradictions(self) -> None:
        for c_id in sorted(self.memory.contradictions.keys()):
            c = self.memory.contradictions[c_id]
            evidence = GoalEvidence(evidence_type="contradiction", source_id=c_id, description=c.description, confidence=c.confidence)
            subject_key = f"resolve_contradiction:contradiction:{c_id}"
            candidate = GoalCandidate(
                subject_key=subject_key, goal_type="resolve_contradiction", title="Resolve contradictory graph evidence",
                description=c.description, supporting_graph_nodes=list(c.node_ids), supporting_graph_edges=list(c.edge_ids),
                contradictions=[c_id], supporting_evidence=[evidence], source_contradiction_ids=[c_id], source_confidence=c.confidence,
            )
            self._candidates[subject_key] = candidate

    # -- Step 5: consistency issues (merge into node candidate when possible) ----

    def _apply_consistency_issues(self) -> None:
        for issue_id in sorted(self.memory.consistency_issues.keys()):
            issue = self.memory.consistency_issues[issue_id]
            if issue.status != "open":
                continue
            goal_type = _ISSUE_TYPE_TO_GOAL_TYPE.get(issue.issue_type, "resolve_contradiction")
            evidence = GoalEvidence(evidence_type="consistency_issue", source_id=issue_id, description=issue.explanation)

            merged = False
            for node_id in issue.node_ids:
                owner = self.memory.nodes.get(node_id)
                if owner is not None and owner.node_type == "workflow_step":
                    owner = self._owning_workflow(owner.node_id)
                if owner is None:
                    continue
                owner_goal_type = _goal_type_for_node(owner, self.memory)
                if owner_goal_type is None:
                    continue
                subject_key = f"{owner_goal_type}:{owner.node_id}"
                candidate = self._candidates.get(subject_key)
                if candidate is not None:
                    candidate.supporting_evidence.append(evidence)
                    candidate.source_consistency_issue_ids.append(issue_id)
                    if issue.severity == "high":
                        candidate.blocking_gaps.append(issue_id)
                    merged = True
                    break
            if merged:
                continue
            self._standalone(f"{goal_type}:issue:{issue_id}", goal_type, f"Resolve consistency issue: {issue.explanation}", evidence, issue.node_ids, gap_id=None, blocking=(issue.severity == "high"), consistency_issue_id=issue_id)

    # -- Step 6: low-confidence edges ---------------------------------------------

    def _apply_low_confidence_edges(self) -> None:
        low_conf = sorted(
            (e for e in self.memory.edges.values() if not e.stale and e.edge_type in _LOW_CONFIDENCE_EDGE_TYPES and e.confidence < _LOW_CONFIDENCE_EDGE_THRESHOLD),
            key=lambda e: (e.confidence, e.edge_id),
        )
        if len(low_conf) > MAX_LOW_CONFIDENCE_EDGE_CANDIDATES:
            self._truncated_low_confidence = True
        for edge in low_conf[:MAX_LOW_CONFIDENCE_EDGE_CANDIDATES]:
            evidence = GoalEvidence(evidence_type="low_confidence_edge", source_id=edge.edge_id, description=f"'{edge.edge_type}' relationship has low confidence ({round(edge.confidence, 2)}).", confidence=edge.confidence)
            self._merge_edge_evidence(edge.edge_id, "increase_confidence", evidence, gap_id=None, blocking=False)

    # -- Step 6b: dependency edges (source -> derived output) not yet verified ---

    def _apply_dependency_edges(self) -> None:
        for edge_id in sorted(self.memory.edges.keys()):
            edge = self.memory.edges[edge_id]
            if edge.stale or edge.status == "verified" or edge.edge_type not in _DEPENDENCY_EDGE_TYPES:
                continue
            source = self.memory.nodes.get(edge.source_node_id)
            target = self.memory.nodes.get(edge.target_node_id)
            if source is None or target is None or target.node_type not in _OUTPUT_NODE_TYPES:
                continue
            subject_key = f"verify_dependency:edge:{edge_id}"
            evidence = GoalEvidence(
                evidence_type="unverified_node", source_id=edge_id,
                description=f"Dependency '{edge.edge_type}' from '{source.canonical_name}' to '{target.canonical_name}' is only {edge.status}.",
                confidence=edge.confidence,
            )
            self._candidates[subject_key] = GoalCandidate(
                subject_key=subject_key, goal_type="verify_dependency",
                title=f"Verify dependency: '{source.canonical_name}' -> '{target.canonical_name}'",
                description=f"'{target.canonical_name}' depends on '{source.canonical_name}' via '{edge.edge_type}', but this has not been before/after verified.",
                required_outputs=[target.canonical_name],
                required_entities=[source.canonical_name] if source.node_type == "entity" else [],
                required_workflows=[source.canonical_name] if source.node_type == "workflow" else [],
                required_actors=[source.canonical_name] if source.node_type == "actor" else [],
                supporting_graph_nodes=[source.node_id, target.node_id], supporting_graph_edges=[edge_id],
                supporting_evidence=[evidence], source_confidence=edge.confidence, estimated_browser_actions=2,
            )

    # -- Step 7: inferred relationships, grouped by rule ---------------------------

    def _apply_inferred_relationships(self) -> None:
        by_rule: dict[str, list] = {}
        for inference in self.memory.inferences.values():
            if inference.invalidated:
                continue
            edge = self.memory.edges.get(inference.edge_id)
            if edge is None or edge.stale:
                continue
            by_rule.setdefault(inference.rule_id, []).append((inference, edge))

        for rule_id in sorted(by_rule.keys()):
            items = sorted(by_rule[rule_id], key=lambda pair: pair[1].edge_id)
            goal_type = "validate_actor_hand_off" if rule_id == "G_cross_role_handoff" else "validate_inferred_relationship"
            subject_key = f"{goal_type}:inference_rule:{rule_id}"
            edges = [edge for _, edge in items]
            nodes: list[str] = []
            for edge in edges:
                nodes.extend([edge.source_node_id, edge.target_node_id])
            avg_conf = sum(e.confidence for e in edges) / len(edges)
            evidence = [GoalEvidence(evidence_type="inferred_edge", source_id=edge.edge_id, description=f"Inferred '{edge.edge_type}' relationship (rule {rule_id}).", confidence=edge.confidence) for edge in edges[:10]]
            candidate = GoalCandidate(
                subject_key=subject_key, goal_type=goal_type,
                title=f"Validate {len(edges)} relationship(s) inferred by rule {rule_id}",
                description=f"Rule {rule_id} inferred {len(edges)} '{edges[0].edge_type}' relationship(s) that have never been directly observed.",
                supporting_graph_nodes=sorted(set(nodes)), supporting_graph_edges=[e.edge_id for e in edges],
                supporting_evidence=evidence, source_inference_rule_ids=[rule_id], source_confidence=avg_conf,
            )
            self._candidates[subject_key] = candidate

    # -- Step 8: business rule / ownership / scope edges ---------------------------

    def _apply_business_rule_ownership_scope_edges(self) -> None:
        for edge_id in sorted(self.memory.edges.keys()):
            edge = self.memory.edges[edge_id]
            if edge.stale or edge.status == "verified":
                continue
            source = self.memory.nodes.get(edge.source_node_id)
            target = self.memory.nodes.get(edge.target_node_id)
            if source is None or target is None:
                continue

            # Entity-relationship and actor->entity cross-reference edges are
            # always synced with status="observed" (the source registries
            # don't carry a separate confidence-vs-status split for these) --
            # so confidence, not status, is the signal that distinguishes a
            # well-corroborated relationship from a merely heuristic one.
            if (
                edge.edge_type in {"relates_to", "parent_of", "child_of", "references"}
                and source.node_type == "entity" and target.node_type == "entity" and edge.confidence < 0.6
            ):
                self._business_rule_candidate(edge, source, target)
            elif edge.edge_type in {"owns", "assigned_to", "manages"} and source.node_type == "actor" and target.node_type == "entity" and edge.confidence < 0.6:
                self._ownership_candidate(edge, source, target)
            elif any([edge.temporal_scope, edge.actor_scope, edge.tenant_scope, edge.filter_scope]) and edge.status in {"candidate", "inferred", "partially_observed"}:
                self._scope_candidate(edge, source, target)

    def _business_rule_candidate(self, edge, source, target) -> None:
        subject_key = f"verify_business_rule:edge:{edge.edge_id}"
        evidence = GoalEvidence(evidence_type="low_confidence_node" if edge.confidence < 0.5 else "unverified_node", source_id=edge.edge_id, description=f"'{source.canonical_name}' {edge.edge_type} '{target.canonical_name}' is not yet confirmed.", confidence=edge.confidence)
        self._candidates[subject_key] = GoalCandidate(
            subject_key=subject_key, goal_type="verify_business_rule",
            title=f"Verify business rule between '{source.canonical_name}' and '{target.canonical_name}'",
            description=f"The relationship '{edge.edge_type}' between entities '{source.canonical_name}' and '{target.canonical_name}' is only {edge.status}.",
            required_entities=sorted({source.canonical_name, target.canonical_name}),
            supporting_graph_nodes=[source.node_id, target.node_id], supporting_graph_edges=[edge.edge_id],
            supporting_evidence=[evidence], source_confidence=edge.confidence,
        )

    def _ownership_candidate(self, edge, source, target) -> None:
        subject_key = f"validate_ownership:edge:{edge.edge_id}"
        evidence = GoalEvidence(evidence_type="unverified_node", source_id=edge.edge_id, description=f"Ownership of '{target.canonical_name}' by '{source.canonical_name}' is {edge.status}.", confidence=edge.confidence)
        self._candidates[subject_key] = GoalCandidate(
            subject_key=subject_key, goal_type="validate_ownership",
            title=f"Validate ownership of '{target.canonical_name}' by '{source.canonical_name}'",
            description=f"'{source.canonical_name}' {edge.edge_type} '{target.canonical_name}' but this has not been independently confirmed.",
            required_actors=[source.canonical_name], required_entities=[target.canonical_name],
            supporting_graph_nodes=[source.node_id, target.node_id], supporting_graph_edges=[edge.edge_id],
            supporting_evidence=[evidence], source_confidence=edge.confidence,
        )

    def _scope_candidate(self, edge, source, target) -> None:
        subject_key = f"validate_scope:edge:{edge.edge_id}"
        scope_desc = ", ".join(f"{k}={v}" for k, v in (("temporal", edge.temporal_scope), ("actor", edge.actor_scope), ("tenant", edge.tenant_scope), ("filter", edge.filter_scope)) if v)
        evidence = GoalEvidence(evidence_type="unverified_node", source_id=edge.edge_id, description=f"Scope-qualified relationship ({scope_desc}) is only {edge.status}.", confidence=edge.confidence)
        self._candidates[subject_key] = GoalCandidate(
            subject_key=subject_key, goal_type="validate_scope",
            title=f"Validate scope on '{edge.edge_type}' relationship",
            description=f"'{source.canonical_name}' {edge.edge_type} '{target.canonical_name}' is scoped ({scope_desc}) but not yet confirmed.",
            supporting_graph_nodes=[source.node_id, target.node_id], supporting_graph_edges=[edge.edge_id],
            supporting_evidence=[evidence], source_confidence=edge.confidence,
        )

    # -- final pruning: drop node-subject candidates with nothing to say --------

    def _prune_clean_verified(self) -> None:
        """A Step-1 node candidate with ZERO accumulated evidence means no
        gap, contradiction, consistency issue, or low-confidence signal ever
        touched that node -- there is genuinely nothing to investigate, so
        no goal should exist for it at all (independent of whether its
        graph status happens to be "verified"; nothing in the current
        pipeline ever sets that status today, but a future engine might)."""
        for key in list(self._candidates.keys()):
            candidate = self._candidates[key]
            if not candidate.supporting_evidence:
                del self._candidates[key]

    def _owning_workflow(self, step_node_id: str):
        for edge_id in self.memory.incoming_edge_ids(step_node_id):
            edge = self.memory.edges.get(edge_id)
            if edge is not None and not edge.stale and edge.edge_type == "has_step":
                return self.memory.nodes.get(edge.source_node_id)
        return None

    # -- helpers ------------------------------------------------------------------

    def _merge_edge_evidence(self, edge_id: str, goal_type: str, evidence: GoalEvidence, *, gap_id: str | None, blocking: bool) -> None:
        edge = self.memory.edges.get(edge_id)
        if edge is None:
            return
        subject_key = f"{goal_type}:edge:{edge_id}"
        candidate = self._candidates.get(subject_key)
        if candidate is None:
            candidate = GoalCandidate(
                subject_key=subject_key, goal_type=goal_type,
                title=f"Increase confidence in '{edge.edge_type}' relationship",
                description=f"The '{edge.edge_type}' relationship from '{edge.source_node_id}' to '{edge.target_node_id}' has low confidence and no corroborating evidence.",
                supporting_graph_nodes=[n for n in (edge.source_node_id, edge.target_node_id) if n in self.memory.nodes],
                supporting_graph_edges=[edge_id], source_confidence=edge.confidence,
            )
            self._candidates[subject_key] = candidate
        candidate.supporting_evidence.append(evidence)
        if gap_id:
            candidate.source_gap_ids.append(gap_id)
            if blocking:
                candidate.blocking_gaps.append(gap_id)

    def _standalone(self, subject_key: str, goal_type: str, title: str, evidence: GoalEvidence, node_ids: list[str], *, gap_id: str | None, blocking: bool, consistency_issue_id: str | None = None) -> None:
        candidate = self._candidates.get(subject_key)
        if candidate is None:
            candidate = GoalCandidate(
                subject_key=subject_key, goal_type=goal_type, title=title, description=evidence.description,
                supporting_graph_nodes=[n for n in node_ids if n in self.memory.nodes], source_confidence=evidence.confidence or 0.3,
            )
            self._candidates[subject_key] = candidate
        candidate.supporting_evidence.append(evidence)
        if gap_id:
            candidate.source_gap_ids.append(gap_id)
            if blocking:
                candidate.blocking_gaps.append(gap_id)
        if consistency_issue_id:
            candidate.source_consistency_issue_ids.append(consistency_issue_id)

    def _populate_required(self, candidate: GoalCandidate, node_id: str) -> None:
        node = self.memory.nodes[node_id]
        required_context = ["requires_authenticated_session"]
        entities: set[str] = set()
        actors: set[str] = set()
        workflows: set[str] = set()
        outputs: set[str] = set()
        permissions: set[str] = set()
        states: set[str] = set()

        if node.node_type == "entity":
            entities.add(node.canonical_name)
        elif node.node_type == "actor":
            actors.add(node.canonical_name)
        elif node.node_type == "workflow":
            workflows.add(node.canonical_name)
        elif node.node_type in _OUTPUT_NODE_TYPES:
            outputs.add(node.canonical_name)
        elif node.node_type == "permission":
            permissions.add(node.canonical_name)
        elif node.node_type == "entity_state":
            states.add(node.canonical_name)

        neighbour_ids = self.memory.outgoing_edge_ids(node_id) | self.memory.incoming_edge_ids(node_id)
        for edge_id in neighbour_ids:
            edge = self.memory.edges.get(edge_id)
            if edge is None:
                continue
            other_id = edge.target_node_id if edge.source_node_id == node_id else edge.source_node_id
            other = self.memory.nodes.get(other_id)
            if other is None:
                continue
            if other.node_type == "actor":
                actors.add(other.canonical_name)
            elif other.node_type == "entity":
                entities.add(other.canonical_name)
            elif other.node_type == "workflow":
                workflows.add(other.canonical_name)
            elif other.node_type == "permission":
                permissions.add(other.canonical_name)
            elif other.node_type in _OUTPUT_NODE_TYPES:
                outputs.add(other.canonical_name)
            elif other.node_type == "entity_state":
                states.add(other.canonical_name)

        for actor_term in sorted(actors):
            required_context.append(f"requires_actor:{actor_term}")

        candidate.required_entities = sorted(entities)
        candidate.required_actors = sorted(actors)
        candidate.required_workflows = sorted(workflows)
        candidate.required_outputs = sorted(outputs)
        candidate.required_permissions = sorted(permissions)
        candidate.required_states = sorted(states)
        candidate.required_context = required_context

        if node.node_type == "workflow":
            candidate.estimated_workflow_depth = len(self.query.workflow_steps(node_id))
            candidate.estimated_actor_count = len(actors) or 1
            candidate.estimated_browser_actions = max(1, candidate.estimated_workflow_depth * 2)
        else:
            candidate.estimated_actor_count = len(actors) or (1 if node.node_type == "actor" else 0)
            candidate.estimated_browser_actions = 2 if node.node_type in _OUTPUT_NODE_TYPES else 1


_TITLE_VERB = {
    "verify_entity_lifecycle": "Verify entity lifecycle for",
    "verify_state_transition": "Verify state transition for",
    "verify_workflow": "Verify workflow",
    "validate_workflow_branch": "Validate workflow branch",
    "validate_prerequisite": "Validate prerequisite",
    "verify_permission": "Verify permission",
    "verify_actor_capability": "Verify actor capability for",
    "verify_kpi": "Verify KPI",
    "validate_dashboard_output": "Validate dashboard output",
    "validate_report": "Validate report",
    "validate_notification": "Validate notification",
    "validate_aggregation_rule": "Validate aggregation rule for",
}

_ISSUE_TYPE_TO_GOAL_TYPE = {
    "permission_conflict": "verify_permission",
    "performed_despite_denial": "verify_permission",
    "dangling_reference": "resolve_unresolved_reference",
    "verified_dependency_contradicted_source": "resolve_contradiction",
    "equivalence_conflict": "resolve_contradiction",
    "stale_edge_endpoint": "resolve_graph_gap",
    "cyclic_workflow_ordering": "verify_workflow",
}
