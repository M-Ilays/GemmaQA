"""Registry-to-graph synchronisation — the ONLY place that reads Entity/
Actor/Workflow/Dependency Registry records and turns them into graph nodes
and edges. Never rediscovers anything from raw page observations; every
node/edge here traces back to an already-existing registry record.

Sync order matters for same-pass resolution (entities -> actors ->
workflows -> dependencies): a workflow step's `entity_id`/`actor_id`
reference usually resolves immediately because the entity/actor node
already exists by the time workflow projection runs. When it can't (the
referenced term genuinely isn't in that registry yet), `RelationshipResolver`
defers it as a `PendingReference` rather than guessing or dropping it.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term
from app.intelligence.knowledge_graph import graph_confidence
from app.intelligence.knowledge_graph.crud_graph_projector import sync_crud_hypotheses
from app.intelligence.knowledge_graph.graph_edge_factory import edge_id, to_evidence_reference, to_evidence_references
from app.intelligence.knowledge_graph.graph_node_factory import (
    derived_output_node_id,
    entity_node_id,
    entity_state_node_id,
    node_type_for_output,
    operation_node_id,
    outcome_node_id,
    page_node_id,
    permission_node_id,
    prerequisite_node_id,
    trigger_node_id,
    workflow_branch_node_id,
    workflow_node_id,
    workflow_step_node_id,
    actor_node_id,
    transition_node_id,
)
from app.intelligence.knowledge_graph.knowledge_graph_memory import KnowledgeGraphMemory
from app.intelligence.knowledge_graph.relationship_resolver import RelationshipResolver
from app.intelligence.knowledge_graph.schemas import PendingReference

_ENTITY_REGISTRY = "entity_registry"
_ACTOR_REGISTRY = "actor_registry"
_WORKFLOW_REGISTRY = "workflow_registry"
_DEPENDENCY_REGISTRY = "dependency_registry"
_CRUD_REGISTRY = "crud_registry"


class GraphSynchronizer:
    def __init__(self, memory: KnowledgeGraphMemory) -> None:
        self.memory = memory
        self.resolver = RelationshipResolver(memory)

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
        """Assumes the caller (`ApplicationKnowledgeGraph.synchronize()`)
        has already called `memory.begin_pass()` and will call
        `memory.end_pass()` once the whole pipeline (this sync, then
        inference, consistency checking, and gap analysis) has run — a
        single graph_version bump covers all of it, not just this step."""
        seen_ids: dict[str, set[str]] = {
            _ENTITY_REGISTRY: set(), _ACTOR_REGISTRY: set(), _WORKFLOW_REGISTRY: set(), _DEPENDENCY_REGISTRY: set(),
            _CRUD_REGISTRY: set(),
        }

        if entity_registry is not None:
            self._sync_entities(entity_registry, iteration, seen_ids)
        if actor_registry is not None:
            self._sync_actors(actor_registry, iteration, seen_ids)
        if workflow_registry is not None:
            self._sync_workflows(workflow_registry, iteration, seen_ids)
        if dependency_registry is not None:
            self._sync_dependencies(dependency_registry, iteration, seen_ids)
        if crud_registry is not None:
            # Runs LAST: CRUD projection only links to entity nodes Entity
            # Discovery has already created this pass (see
            # crud_graph_projector.sync_crud_hypotheses) — never invents one.
            sync_crud_hypotheses(self.memory, self.resolver, crud_registry, iteration, seen_ids[_CRUD_REGISTRY])

        resolved = self.resolver.try_resolve_pending()
        for ref, target in resolved:
            e_id = edge_id(ref.edge_type, ref.source_node_id, target, discriminator=ref.reference_id[:8])
            self.memory.upsert_edge(
                e_id, edge_type=ref.edge_type, source_node_id=ref.source_node_id, target_node_id=target,
                status="observed", confidence=0.4, source_registry="resolved_reference",
                evidence_references=ref.evidence_references, iteration=iteration,
            )
            ref.resolved_edge_id = e_id
            self.memory.resolved_reference_ids.append(ref.reference_id)

        self._mark_stale_projections(seen_ids)

        return {
            "graph_version": self.memory.graph_version,
            "added_nodes": list(self.memory.added_node_ids),
            "updated_nodes": list(self.memory.updated_node_ids),
            "stale_nodes": list(self.memory.stale_node_ids),
            "added_edges": list(self.memory.added_edge_ids),
            "updated_edges": list(self.memory.updated_edge_ids),
            "stale_edges": list(self.memory.stale_edge_ids),
            "resolved_references": [r.reference_id for r, _ in resolved],
        }

    # -- entity projection ------------------------------------------------------

    def _sync_entities(self, entity_registry: Any, iteration: int, seen_ids: dict[str, set[str]]) -> None:
        for record in entity_registry.all_entities():
            node_id = entity_node_id(record.canonical_name)
            seen_ids[_ENTITY_REGISTRY].add(node_id)
            self.memory.upsert_node(
                node_id, node_type="entity", canonical_name=record.canonical_name, aliases=record.aliases,
                status=graph_confidence.status_for_projection(record.status), confidence=graph_confidence.node_confidence_from_source(record.confidence),
                source_registry=_ENTITY_REGISTRY, source_record_id=record.entity_id, source_record_version=len(record.evidence),
                evidence_references=to_evidence_references(record.evidence, _ENTITY_REGISTRY),
                attributes={"related_forms": list(record.related_forms), "related_tables": list(record.related_tables), "operations": record.operation_names()},
                iteration=iteration,
            )

            for state in record.known_states:
                state_id = entity_state_node_id(record.canonical_name, state)
                seen_ids[_ENTITY_REGISTRY].add(state_id)
                self.memory.upsert_node(
                    state_id, node_type="entity_state", canonical_name=state, status="observed", confidence=record.confidence,
                    source_registry=_ENTITY_REGISTRY, source_record_id=f"{record.entity_id}:{normalize_term(state)}",
                    iteration=iteration,
                )
                e_id = edge_id("has_state", node_id, state_id)
                seen_ids[_ENTITY_REGISTRY].add(e_id)
                self.memory.upsert_edge(
                    e_id, edge_type="has_state", source_node_id=node_id, target_node_id=state_id, status="observed",
                    confidence=record.confidence, source_registry=_ENTITY_REGISTRY, source_record_ids=[record.entity_id], iteration=iteration,
                )

            for operation in record.operations:
                op_id = operation_node_id(record.canonical_name, operation.operation)
                seen_ids[_ENTITY_REGISTRY].add(op_id)
                self.memory.upsert_node(
                    op_id, node_type="operation", canonical_name=operation.operation, status="observed", confidence=operation.confidence,
                    source_registry=_ENTITY_REGISTRY, source_record_id=f"{record.entity_id}:{operation.operation}",
                    evidence_references=to_evidence_references(operation.evidence, _ENTITY_REGISTRY), iteration=iteration,
                )
                e_id = edge_id("acts_on", op_id, node_id)
                seen_ids[_ENTITY_REGISTRY].add(e_id)
                self.memory.upsert_edge(
                    e_id, edge_type="acts_on", source_node_id=op_id, target_node_id=node_id, status="observed",
                    confidence=operation.confidence, source_registry=_ENTITY_REGISTRY, source_record_ids=[record.entity_id], iteration=iteration,
                )

            for page_url in record.related_pages:
                p_id = page_node_id(page_url)
                seen_ids[_ENTITY_REGISTRY].add(p_id)
                self.memory.upsert_node(p_id, node_type="page", canonical_name=page_url, status="observed", confidence=0.5, source_registry=_ENTITY_REGISTRY, source_record_id=page_url, iteration=iteration)
                e_id = edge_id("appears_on", node_id, p_id)
                seen_ids[_ENTITY_REGISTRY].add(e_id)
                self.memory.upsert_edge(e_id, edge_type="appears_on", source_node_id=node_id, target_node_id=p_id, status="observed", confidence=0.5, source_registry=_ENTITY_REGISTRY, source_record_ids=[record.entity_id], iteration=iteration)

            for rel in record.relationships:
                subject_id = entity_node_id(rel.subject_entity_id)
                object_id = entity_node_id(rel.object_entity_id)
                e_id = edge_id(rel.kind, subject_id, object_id)
                seen_ids[_ENTITY_REGISTRY].add(e_id)
                self.memory.upsert_edge(
                    e_id, edge_type=rel.kind, source_node_id=subject_id, target_node_id=object_id, status="observed",
                    confidence=rel.confidence, source_registry=_ENTITY_REGISTRY, source_record_ids=[rel.relationship_id],
                    evidence_references=to_evidence_references(rel.evidence, _ENTITY_REGISTRY), iteration=iteration,
                )

    # -- actor projection ---------------------------------------------------------

    def _sync_actors(self, actor_registry: Any, iteration: int, seen_ids: dict[str, set[str]]) -> None:
        for record in actor_registry.all_actors():
            node_id = actor_node_id(record.canonical_name)
            seen_ids[_ACTOR_REGISTRY].add(node_id)
            self.memory.upsert_node(
                node_id, node_type="actor", canonical_name=record.canonical_name, aliases=record.aliases,
                status=graph_confidence.status_for_projection(record.status), confidence=graph_confidence.node_confidence_from_source(record.confidence),
                source_registry=_ACTOR_REGISTRY, source_record_id=record.actor_id, source_record_version=len(record.supporting_evidence),
                evidence_references=to_evidence_references(record.supporting_evidence, _ACTOR_REGISTRY),
                attributes={"known_dashboards": list(record.known_dashboards), "known_session_types": list(record.known_session_types)},
                iteration=iteration,
            )

            for permission in record.known_permissions:
                perm_id = permission_node_id(record.canonical_name, permission.permission_id)
                seen_ids[_ACTOR_REGISTRY].add(perm_id)
                net_positive = len(permission.positive_evidence) > len(permission.negative_evidence)
                self.memory.upsert_node(
                    perm_id, node_type="permission", canonical_name=permission.permission_id, status="observed",
                    confidence=permission.confidence, source_registry=_ACTOR_REGISTRY, source_record_id=f"{record.actor_id}:{permission.permission_id}",
                    evidence_references=to_evidence_references([*permission.positive_evidence, *permission.negative_evidence], _ACTOR_REGISTRY),
                    iteration=iteration,
                )
                perm_edge_type = "has_permission" if net_positive else "lacks_permission"
                # Negative edges require explicit negative evidence -- never
                # inferred from mere absence of a positive observation.
                if not net_positive and not permission.negative_evidence:
                    continue
                e_id = edge_id(perm_edge_type, node_id, perm_id)
                seen_ids[_ACTOR_REGISTRY].add(e_id)
                self.memory.upsert_edge(
                    e_id, edge_type=perm_edge_type, source_node_id=node_id, target_node_id=perm_id,
                    status="observed", confidence=permission.confidence, source_registry=_ACTOR_REGISTRY,
                    source_record_ids=[record.actor_id], iteration=iteration,
                )

                # Permission -> enables -> Operation (best-effort resolution;
                # a permission_id like "can_create_customer" -> verb
                # "create" on entity "customer").
                parsed = _parse_permission_id(permission.permission_id)
                if parsed is not None:
                    verb, subject = parsed
                    op_hint = f"{subject}:{verb}" if subject else verb
                    op_node_id = self.resolver.resolve(op_hint, "operation") if subject else None
                    if op_node_id is None and subject:
                        op_node_id = operation_node_id(subject, verb) if operation_node_id(subject, verb) in self.memory.nodes else None
                    if op_node_id is not None:
                        e_id2 = edge_id("enables", perm_id, op_node_id)
                        seen_ids[_ACTOR_REGISTRY].add(e_id2)
                        self.memory.upsert_edge(
                            e_id2, edge_type="enables", source_node_id=perm_id, target_node_id=op_node_id,
                            status="observed", confidence=permission.confidence * 0.8, source_registry=_ACTOR_REGISTRY,
                            source_record_ids=[record.actor_id], iteration=iteration,
                        )
                    else:
                        self.memory.add_pending_reference(
                            PendingReference(source_node_id=perm_id, edge_type="enables", target_hint=op_hint, target_node_type="operation", reason="permission_id not yet matched to a known operation")
                        )

            for dashboard_url in record.known_dashboards:
                p_id = page_node_id(dashboard_url)
                seen_ids[_ACTOR_REGISTRY].add(p_id)
                self.memory.upsert_node(p_id, node_type="page", canonical_name=dashboard_url, status="observed", confidence=0.5, source_registry=_ACTOR_REGISTRY, source_record_id=dashboard_url, iteration=iteration)
                e_id = edge_id("views", node_id, p_id)
                seen_ids[_ACTOR_REGISTRY].add(e_id)
                self.memory.upsert_edge(e_id, edge_type="views", source_node_id=node_id, target_node_id=p_id, status="observed", confidence=0.5, source_registry=_ACTOR_REGISTRY, source_record_ids=[record.actor_id], iteration=iteration)

            for entity_term in record.known_entities:
                target = self.resolver.resolve_or_defer(source_node_id=node_id, edge_type="manages", hint=entity_term, node_type_hint="entity", reason="actor known_entities cross-reference")
                if target is not None:
                    e_id = edge_id("manages", node_id, target)
                    seen_ids[_ACTOR_REGISTRY].add(e_id)
                    self.memory.upsert_edge(e_id, edge_type="manages", source_node_id=node_id, target_node_id=target, status="observed", confidence=0.4, source_registry=_ACTOR_REGISTRY, source_record_ids=[record.actor_id], iteration=iteration)

    # -- workflow projection --------------------------------------------------------

    def _sync_workflows(self, workflow_registry: Any, iteration: int, seen_ids: dict[str, set[str]]) -> None:
        for wf in workflow_registry.all_workflows():
            wf_node_id = workflow_node_id(wf.canonical_name)
            seen_ids[_WORKFLOW_REGISTRY].add(wf_node_id)
            self.memory.upsert_node(
                wf_node_id, node_type="workflow", canonical_name=wf.canonical_name, aliases=wf.aliases,
                status=graph_confidence.status_for_projection(wf.status), confidence=graph_confidence.node_confidence_from_source(wf.confidence),
                source_registry=_WORKFLOW_REGISTRY, source_record_id=wf.workflow_id, source_record_version=wf.version,
                evidence_references=to_evidence_references(wf.supporting_evidence, _WORKFLOW_REGISTRY),
                attributes={"known_entry_points": list(wf.known_entry_points), "known_exit_points": list(wf.known_exit_points)},
                iteration=iteration,
            )

            step_ids_by_step: dict[str, str] = {}
            for step in sorted(wf.steps, key=lambda s: s.sequence_hint):
                step_node_id = workflow_step_node_id(step.step_id)
                step_ids_by_step[step.step_id] = step_node_id
                seen_ids[_WORKFLOW_REGISTRY].add(step_node_id)
                # WorkflowStep's own STEP_STATUSES vocabulary ("unverified",
                # "completed", ...) is NOT the same closed set as the
                # graph's GRAPH_STATUSES -- every use below must go through
                # this mapping, never the raw `step.status`.
                step_graph_status = graph_confidence.status_for_projection(step.status)
                self.memory.upsert_node(
                    step_node_id, node_type="workflow_step", canonical_name=step.semantic_action or step.action_verb, status=step_graph_status,
                    confidence=step.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_id=step.step_id,
                    evidence_references=to_evidence_references(step.evidence, _WORKFLOW_REGISTRY),
                    attributes={"sequence_hint": step.sequence_hint}, iteration=iteration,
                )
                e_id = edge_id("has_step", wf_node_id, step_node_id)
                seen_ids[_WORKFLOW_REGISTRY].add(e_id)
                self.memory.upsert_edge(e_id, edge_type="has_step", source_node_id=wf_node_id, target_node_id=step_node_id, status=step_graph_status, confidence=step.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[wf.workflow_id], iteration=iteration)

                if step.actor_id:
                    actor_target = self.resolver.resolve_or_defer(source_node_id=step_node_id, edge_type="performed_by", hint=step.actor_id, node_type_hint="actor", reason="workflow step actor reference")
                    if actor_target is not None:
                        e_id2 = edge_id("performed_by", step_node_id, actor_target)
                        seen_ids[_WORKFLOW_REGISTRY].add(e_id2)
                        self.memory.upsert_edge(e_id2, edge_type="performed_by", source_node_id=step_node_id, target_node_id=actor_target, status=step_graph_status, confidence=step.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[step.step_id], iteration=iteration)

                if step.entity_id:
                    entity_target = self.resolver.resolve_or_defer(source_node_id=step_node_id, edge_type="acts_on", hint=step.entity_id, node_type_hint="entity", reason="workflow step entity reference")
                    if entity_target is not None:
                        e_id3 = edge_id("acts_on", step_node_id, entity_target)
                        seen_ids[_WORKFLOW_REGISTRY].add(e_id3)
                        self.memory.upsert_edge(e_id3, edge_type="acts_on", source_node_id=step_node_id, target_node_id=entity_target, status=step_graph_status, confidence=step.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[step.step_id], iteration=iteration)

                    # Step-level source/target state (distinct from the
                    # workflow-level Transition nodes below) -- this is what
                    # lets inference rule D ("compatible continuation")
                    # chain two step's states without needing a separate
                    # WorkflowTransition record for every step.
                    if step.source_state:
                        from_state_id = entity_state_node_id(step.entity_id, step.source_state)
                        if from_state_id in self.memory.nodes:
                            e_id_from = edge_id("from_state", step_node_id, from_state_id)
                            seen_ids[_WORKFLOW_REGISTRY].add(e_id_from)
                            self.memory.upsert_edge(e_id_from, edge_type="from_state", source_node_id=step_node_id, target_node_id=from_state_id, status=step_graph_status, confidence=step.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[step.step_id], iteration=iteration)
                    if step.target_state:
                        to_state_id = entity_state_node_id(step.entity_id, step.target_state)
                        if to_state_id in self.memory.nodes:
                            e_id_to = edge_id("to_state", step_node_id, to_state_id)
                            seen_ids[_WORKFLOW_REGISTRY].add(e_id_to)
                            self.memory.upsert_edge(e_id_to, edge_type="to_state", source_node_id=step_node_id, target_node_id=to_state_id, status=step_graph_status, confidence=step.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[step.step_id], iteration=iteration)

            ordered_steps = sorted(wf.steps, key=lambda s: s.sequence_hint)
            for prev_step, next_step in zip(ordered_steps, ordered_steps[1:]):
                if prev_step.sequence_hint == next_step.sequence_hint:
                    continue
                a_id, b_id = step_ids_by_step[prev_step.step_id], step_ids_by_step[next_step.step_id]
                e_id = edge_id("precedes", a_id, b_id)
                seen_ids[_WORKFLOW_REGISTRY].add(e_id)
                self.memory.upsert_edge(
                    e_id, edge_type="precedes", source_node_id=a_id, target_node_id=b_id, status="observed", confidence=min(prev_step.confidence, next_step.confidence),
                    source_registry=_WORKFLOW_REGISTRY, source_record_ids=[wf.workflow_id],
                    qualifiers={"from_sequence": str(prev_step.sequence_hint), "to_sequence": str(next_step.sequence_hint)}, iteration=iteration,
                )

            for participation in wf.actors:
                actor_target = self.resolver.resolve_or_defer(source_node_id=wf_node_id, edge_type="participates_in", hint=participation.actor_id, node_type_hint="actor", reason="workflow actor participation")
                if actor_target is not None:
                    e_id = edge_id("participates_in", actor_target, wf_node_id)
                    seen_ids[_WORKFLOW_REGISTRY].add(e_id)
                    self.memory.upsert_edge(
                        e_id, edge_type="participates_in", source_node_id=actor_target, target_node_id=wf_node_id, status="observed",
                        confidence=participation.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[wf.workflow_id],
                        qualifiers={"role": participation.role_in_workflow}, iteration=iteration,
                    )

            for entity_participation in wf.entities:
                entity_target = self.resolver.resolve(entity_participation.entity_id, "entity")
                if entity_target is not None:
                    e_id = edge_id("acts_on", wf_node_id, entity_target)
                    seen_ids[_WORKFLOW_REGISTRY].add(e_id)
                    self.memory.upsert_edge(
                        e_id, edge_type="acts_on", source_node_id=wf_node_id, target_node_id=entity_target, status="observed",
                        confidence=entity_participation.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[wf.workflow_id], iteration=iteration,
                    )

            for transition in wf.transitions:
                t_id = transition_node_id(transition.transition_id)
                seen_ids[_WORKFLOW_REGISTRY].add(t_id)
                self.memory.upsert_node(
                    t_id, node_type="transition", canonical_name=f"{transition.source_state} -> {transition.target_state}",
                    status="observed" if transition.is_explicit else "partially_observed", confidence=transition.confidence,
                    source_registry=_WORKFLOW_REGISTRY, source_record_id=transition.transition_id,
                    evidence_references=to_evidence_references(transition.evidence, _WORKFLOW_REGISTRY), iteration=iteration,
                )
                e_id = edge_id("transitions", wf_node_id, t_id)
                seen_ids[_WORKFLOW_REGISTRY].add(e_id)
                self.memory.upsert_edge(e_id, edge_type="transitions", source_node_id=wf_node_id, target_node_id=t_id, status="observed", confidence=transition.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[wf.workflow_id], iteration=iteration)

                if transition.entity_id:
                    from_state_id = entity_state_node_id(transition.entity_id, transition.source_state)
                    to_state_id = entity_state_node_id(transition.entity_id, transition.target_state)
                    if from_state_id in self.memory.nodes:
                        e_id2 = edge_id("from_state", t_id, from_state_id)
                        seen_ids[_WORKFLOW_REGISTRY].add(e_id2)
                        self.memory.upsert_edge(e_id2, edge_type="from_state", source_node_id=t_id, target_node_id=from_state_id, status="observed", confidence=transition.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[transition.transition_id], iteration=iteration)
                    if to_state_id in self.memory.nodes:
                        e_id3 = edge_id("to_state", t_id, to_state_id)
                        seen_ids[_WORKFLOW_REGISTRY].add(e_id3)
                        self.memory.upsert_edge(e_id3, edge_type="to_state", source_node_id=t_id, target_node_id=to_state_id, status="observed", confidence=transition.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[transition.transition_id], iteration=iteration)

            for outcome in wf.outcomes:
                o_id = outcome_node_id(outcome.outcome_id)
                seen_ids[_WORKFLOW_REGISTRY].add(o_id)
                self.memory.upsert_node(o_id, node_type="outcome", canonical_name=outcome.description or outcome.outcome_type, status="observed", confidence=outcome.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_id=outcome.outcome_id, evidence_references=to_evidence_references(outcome.evidence, _WORKFLOW_REGISTRY), iteration=iteration)
                e_id = edge_id("produces", wf_node_id, o_id)
                seen_ids[_WORKFLOW_REGISTRY].add(e_id)
                self.memory.upsert_edge(e_id, edge_type="produces", source_node_id=wf_node_id, target_node_id=o_id, status="observed", confidence=outcome.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[wf.workflow_id], iteration=iteration)

            for prereq in wf.prerequisites:
                pr_id = prerequisite_node_id(prereq.prerequisite_id)
                seen_ids[_WORKFLOW_REGISTRY].add(pr_id)
                self.memory.upsert_node(pr_id, node_type="prerequisite", canonical_name=prereq.type, status="blocked" if prereq.satisfied is False else "observed", confidence=prereq.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_id=prereq.prerequisite_id, evidence_references=to_evidence_references(prereq.evidence, _WORKFLOW_REGISTRY), attributes={"target": prereq.target, "satisfied": prereq.satisfied}, iteration=iteration)
                e_id = edge_id("requires", wf_node_id, pr_id)
                seen_ids[_WORKFLOW_REGISTRY].add(e_id)
                self.memory.upsert_edge(e_id, edge_type="requires", source_node_id=wf_node_id, target_node_id=pr_id, status="blocked" if prereq.satisfied is False else "observed", confidence=prereq.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[wf.workflow_id], iteration=iteration)

            for branch in wf.branches:
                b_id = workflow_branch_node_id(branch.branch_id)
                seen_ids[_WORKFLOW_REGISTRY].add(b_id)
                self.memory.upsert_node(b_id, node_type="workflow_branch", canonical_name=branch.branch_type, status="observed", confidence=branch.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_id=branch.branch_id, evidence_references=to_evidence_references(branch.evidence, _WORKFLOW_REGISTRY), attributes={"option_labels": list(branch.option_labels)}, iteration=iteration)
                e_id = edge_id("branches_to", wf_node_id, b_id)
                seen_ids[_WORKFLOW_REGISTRY].add(e_id)
                self.memory.upsert_edge(e_id, edge_type="branches_to", source_node_id=wf_node_id, target_node_id=b_id, status="observed", confidence=branch.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[wf.workflow_id], iteration=iteration)

            for trigger in wf.triggers:
                tr_id = trigger_node_id(trigger.trigger_id)
                seen_ids[_WORKFLOW_REGISTRY].add(tr_id)
                self.memory.upsert_node(tr_id, node_type="trigger", canonical_name=trigger.description or trigger.trigger_type, status="observed", confidence=trigger.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_id=trigger.trigger_id, evidence_references=to_evidence_references(trigger.evidence, _WORKFLOW_REGISTRY), iteration=iteration)
                e_id = edge_id("triggered_by", wf_node_id, tr_id)
                seen_ids[_WORKFLOW_REGISTRY].add(e_id)
                self.memory.upsert_edge(e_id, edge_type="triggered_by", source_node_id=wf_node_id, target_node_id=tr_id, status="observed", confidence=trigger.confidence, source_registry=_WORKFLOW_REGISTRY, source_record_ids=[wf.workflow_id], iteration=iteration)

    # -- dependency projection ------------------------------------------------------

    def _sync_dependencies(self, dependency_registry: Any, iteration: int, seen_ids: dict[str, set[str]]) -> None:
        for output in dependency_registry.all_outputs():
            out_id = derived_output_node_id(output.output_id)
            seen_ids[_DEPENDENCY_REGISTRY].add(out_id)
            self.memory.upsert_node(
                out_id, node_type=node_type_for_output(output.output_type), canonical_name=output.canonical_label, aliases=output.aliases,
                status="observed", confidence=graph_confidence.node_confidence_from_source(output.confidence),
                source_registry=_DEPENDENCY_REGISTRY, source_record_id=output.output_id, source_record_version=output.observation_count,
                evidence_references=to_evidence_references(output.evidence, _DEPENDENCY_REGISTRY),
                attributes={"raw_value": output.raw_value, "output_type": output.output_type}, iteration=iteration,
            )
            if output.page_url:
                p_id = page_node_id(output.page_url)
                seen_ids[_DEPENDENCY_REGISTRY].add(p_id)
                self.memory.upsert_node(p_id, node_type="page", canonical_name=output.page_url, status="observed", confidence=0.5, source_registry=_DEPENDENCY_REGISTRY, source_record_id=output.page_url, iteration=iteration)
                e_id0 = edge_id("appears_on", out_id, p_id)
                seen_ids[_DEPENDENCY_REGISTRY].add(e_id0)
                self.memory.upsert_edge(e_id0, edge_type="appears_on", source_node_id=out_id, target_node_id=p_id, status="observed", confidence=0.5, source_registry=_DEPENDENCY_REGISTRY, source_record_ids=[output.output_id], iteration=iteration)

        for dep in dependency_registry.all_dependencies():
            if not dep.target_output_ids:
                continue
            source_node_id, source_type = _dependency_source_node(dep, self.resolver)
            if source_node_id is None:
                continue
            for output_id in dep.target_output_ids:
                target_node_id = derived_output_node_id(output_id)
                if target_node_id not in self.memory.nodes:
                    continue
                e_id = edge_id(dep.relationship_type, source_node_id, target_node_id, discriminator=dep.dependency_id[:8])
                seen_ids[_DEPENDENCY_REGISTRY].add(e_id)
                attrs = graph_confidence.dependency_confidence_attributes(dep)
                attrs["effect_direction"] = dep.effect_direction
                self.memory.upsert_edge(
                    e_id, edge_type=dep.relationship_type, source_node_id=source_node_id, target_node_id=target_node_id,
                    status=dep.status, confidence=dep.confidence, source_registry=_DEPENDENCY_REGISTRY, source_record_ids=[dep.dependency_id],
                    evidence_references=to_evidence_references(dep.supporting_evidence, _DEPENDENCY_REGISTRY), attributes=attrs,
                    temporal_scope=dep.temporal_scope.label if dep.temporal_scope else None,
                    actor_scope=dep.actor_scope.actor_term if dep.actor_scope else None,
                    tenant_scope=dep.tenant_scope.tenant_term if dep.tenant_scope else None,
                    filter_scope=f"{dep.filter_scope.filter_label}={dep.filter_scope.filter_value}" if dep.filter_scope else None,
                    iteration=iteration,
                )

                for rule in dep.inclusion_rules:
                    state_id = None
                    if source_type == "entity" and source_node_id in self.memory.nodes:
                        entity_term = self.memory.nodes[source_node_id].canonical_name
                        state_id = entity_state_node_id(entity_term, rule.state_label)
                    if state_id and state_id in self.memory.nodes:
                        e_id2 = edge_id("includes_state", state_id, target_node_id, discriminator=dep.dependency_id[:8])
                        seen_ids[_DEPENDENCY_REGISTRY].add(e_id2)
                        self.memory.upsert_edge(e_id2, edge_type="includes_state", source_node_id=state_id, target_node_id=target_node_id, status=dep.status, confidence=rule.confidence, source_registry=_DEPENDENCY_REGISTRY, source_record_ids=[dep.dependency_id], iteration=iteration)

                for producer in dep.known_producers:
                    actor_target = self.resolver.resolve(producer, "actor")
                    if actor_target is not None:
                        e_id3 = edge_id("generated_by", target_node_id, actor_target, discriminator=dep.dependency_id[:8])
                        seen_ids[_DEPENDENCY_REGISTRY].add(e_id3)
                        self.memory.upsert_edge(e_id3, edge_type="generated_by", source_node_id=target_node_id, target_node_id=actor_target, status="observed", confidence=dep.confidence, source_registry=_DEPENDENCY_REGISTRY, source_record_ids=[dep.dependency_id], iteration=iteration)

                for consumer in dep.known_consumers:
                    actor_target = self.resolver.resolve(consumer, "actor")
                    if actor_target is not None:
                        e_id4 = edge_id("visible_to", target_node_id, actor_target, discriminator=dep.dependency_id[:8])
                        seen_ids[_DEPENDENCY_REGISTRY].add(e_id4)
                        self.memory.upsert_edge(e_id4, edge_type="visible_to", source_node_id=target_node_id, target_node_id=actor_target, status="observed", confidence=dep.confidence, source_registry=_DEPENDENCY_REGISTRY, source_record_ids=[dep.dependency_id], iteration=iteration)

    # -- staleness ------------------------------------------------------------------

    def _mark_stale_projections(self, seen_ids: dict[str, set[str]]) -> None:
        for source_registry, seen in seen_ids.items():
            for node_id, node in list(self.memory.nodes.items()):
                if node.source_registry == source_registry and not node.stale and node_id not in seen:
                    self.memory.mark_node_stale(node_id)
            for e_id, edge in list(self.memory.edges.items()):
                if edge.source_registry == source_registry and not edge.stale and e_id not in seen:
                    self.memory.mark_edge_stale(e_id)


def _parse_permission_id(permission_id: str) -> tuple[str, str] | None:
    """`can_create_customer` -> `("create", "customer")`; `can_export` ->
    `("export", "")`. Best-effort, never raises."""
    if not permission_id.startswith("can_"):
        return None
    rest = permission_id[len("can_") :]
    parts = rest.split("_")
    if not parts:
        return None
    verb = parts[0]
    subject = " ".join(parts[1:])
    return verb, subject


def _dependency_source_node(dep: Any, resolver: RelationshipResolver) -> tuple[str | None, str | None]:
    if dep.source_entity_ids:
        target = resolver.resolve(dep.source_entity_ids[0], "entity")
        if target is not None:
            return target, "entity"
    if dep.source_workflow_ids:
        target = resolver.resolve(dep.source_workflow_ids[0], "workflow")
        if target is not None:
            return target, "workflow"
    if dep.source_actor_ids:
        target = resolver.resolve(dep.source_actor_ids[0], "actor")
        if target is not None:
            return target, "actor"
    return None, None
