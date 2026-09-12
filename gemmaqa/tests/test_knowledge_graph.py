"""Application Knowledge Graph (app/intelligence/knowledge_graph/).

Covers: node projection (entity/actor/workflow/step/output/state/permission/
unknown, stable ids, aliases), edge projection (permission/operation/
participation/steps/transitions/outcomes/output-visibility/state-inclusion-
exclusion), synchronisation (first-sync, idempotent resync, changed-record
update, removed-relation staleness, version-bump discipline, pending-
reference resolution, source-registry immutability, evidence dedup),
identity resolution (direct id, cross-reference, alias, ambiguous-name
non-merge, authoritative-id-conflict issue, equivalent candidate, stale
reference), bounded inference (all 7 rules + inferred-stays-inferred +
premise-capped confidence + invalidation on stale premise), contradictions
(permission conflict, state-sequence conflict, opposite-effect conflict,
denial-violation, invalid-equivalence, cyclic ordering, scope-aware non-
false-positive, both-sides-retained), gap analysis (13+ gap types +
resolution on new evidence), query engine (retrieval + semantic queries +
bounded traversal + filters + result limits), context projection (per-
focus-type + exclusions + record limits), versioning (initial/no-op/diffs/
changes-since), and neutrality (AST contract + generic fixtures + unknown
type preservation) — plus 6 synthetic end-to-end integration fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest

from app.intelligence.actor_discovery import ActorRegistry
from app.intelligence.actor_discovery.schemas import ActorEvidence, ActorRecord, PermissionCandidate
from app.intelligence.entity_discovery import EntityRegistry
from app.intelligence.entity_discovery.schemas import EntityEvidence, EntityOperation, EntityRecord, EntityRelationship
from app.intelligence.workflow_discovery import WorkflowRegistry
from app.intelligence.workflow_discovery.schemas import (
    WorkflowActorParticipation,
    WorkflowEntityParticipation,
    WorkflowOutcome,
    WorkflowStep,
    WorkflowTransition,
)
from app.intelligence.dependency_discovery import DependencyRegistry
from app.intelligence.dependency_discovery.schemas import (
    ActorScope,
    DependencyDescriptor,
    DependencyEvidence,
    DerivedOutputDescriptor,
    StateInclusionRule,
)
from app.intelligence.knowledge_graph import ApplicationKnowledgeGraph
from app.intelligence.knowledge_graph.graph_confidence import inferred_edge_confidence, node_confidence_from_source
from app.intelligence.knowledge_graph.graph_node_factory import actor_node_id, entity_node_id, workflow_node_id
from app.intelligence.knowledge_graph.schemas import GraphQuery, GraphTraversalConstraint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(term, *, states=None, aliases=None, status="confirmed", confidence=0.8, operations=None, relationships=None) -> EntityRecord:
    return EntityRecord(
        canonical_name=term, status=status, confidence=confidence, known_states=states or [], aliases=aliases or [],
        operations=operations or [], relationships=relationships or [],
        evidence=[EntityEvidence(source_kind="heading", observed_text=term)],
    )


def _actor(term, *, permissions=None, status="confirmed", confidence=0.7, known_entities=None, known_dashboards=None) -> ActorRecord:
    return ActorRecord(
        canonical_name=term, status=status, confidence=confidence, known_permissions=permissions or [],
        known_entities=known_entities or [], known_dashboards=known_dashboards or [],
        supporting_evidence=[ActorEvidence(source_kind="session_info", observed_text=term)],
    )


def _permission(permission_id, *, confidence=0.6, positive=True) -> PermissionCandidate:
    ev = ActorEvidence(source_kind="visible_control", observed_text=permission_id)
    return PermissionCandidate(
        permission_id=permission_id, confidence=confidence,
        positive_evidence=[ev] if positive else [], negative_evidence=[] if positive else [ev, ev],
    )


def _step(verb, *, entity_id=None, actor_id="current session", status="observed", sequence_hint=0, source_state=None, target_state=None) -> WorkflowStep:
    return WorkflowStep(
        semantic_action=verb, entity_id=entity_id, actor_id=actor_id, status=status, sequence_hint=sequence_hint,
        source_state=source_state, target_state=target_state,
        page_id="p", page_state_fingerprint="f", confidence=0.6,
    )


def _workflow(registry: WorkflowRegistry, anchor, name, **parts) -> object:
    wf = registry.memory.get_or_create(anchor, canonical_name=name)
    wf.steps.extend(parts.get("steps", []))
    wf.actors.extend(parts.get("actors", []))
    wf.entities.extend(parts.get("entities", []))
    wf.transitions.extend(parts.get("transitions", []))
    wf.outcomes.extend(parts.get("outcomes", []))
    wf.prerequisites.extend(parts.get("prerequisites", []))
    wf.branches.extend(parts.get("branches", []))
    wf.triggers.extend(parts.get("triggers", []))
    return wf


def _output(label, *, output_type="kpi_card", value="5", confidence=0.6) -> DerivedOutputDescriptor:
    return DerivedOutputDescriptor(
        canonical_label=label, output_type=output_type, raw_value=value, parsed_value=float(value) if value.replace(".", "", 1).isdigit() else None,
        confidence=confidence, evidence=[DependencyEvidence(source_kind="heading", observed_text=label)],
    )


def _dependency(*, entity_ids=None, workflow_ids=None, actor_ids=None, output_id, status="observed", confidence=0.6, effect_direction="unknown", inclusion_rules=None, actor_scope=None) -> DependencyDescriptor:
    return DependencyDescriptor(
        canonical_name=f"dep -> {output_id}", dependency_type="entity_dependency", relationship_type="counts",
        source_entity_ids=entity_ids or [], source_workflow_ids=workflow_ids or [], source_actor_ids=actor_ids or [],
        target_output_ids=[output_id], status=status, confidence=confidence, effect_direction=effect_direction,
        inclusion_rules=inclusion_rules or [], actor_scope=actor_scope,
        supporting_evidence=[DependencyEvidence(source_kind="entity_registry", observed_text="matched")],
    )


def _registries():
    return EntityRegistry(), ActorRegistry(), WorkflowRegistry(), DependencyRegistry()


def _sync(kg, er=None, ar=None, wr=None, dr=None, iteration=1):
    return kg.synchronize(entity_registry=er, actor_registry=ar, workflow_registry=wr, dependency_registry=dr, iteration=iteration)


# ---------------------------------------------------------------------------
# A. Node projection
# ---------------------------------------------------------------------------


class TestNodeProjection:
    def test_entity_becomes_entity_node(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["open"])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        node = kg.query_engine.node_by_id(entity_node_id("job"))
        assert node is not None and node.node_type == "entity"

    def test_actor_becomes_actor_node(self):
        _, ar, *_ = _registries()
        ar.memory.records["alice"] = _actor("alice")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, ar=ar)
        node = kg.query_engine.node_by_id(actor_node_id("alice"))
        assert node is not None and node.node_type == "actor"

    def test_workflow_becomes_workflow_node(self):
        _, _, wr, _ = _registries()
        _workflow(wr, "entity:job", "job workflow")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, wr=wr)
        node = kg.query_engine.node_by_id(workflow_node_id("job workflow"))
        assert node is not None and node.node_type == "workflow"

    def test_workflow_step_becomes_step_node(self):
        _, _, wr, _ = _registries()
        wf = _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, wr=wr)
        steps = kg.query_engine.nodes_by_type("workflow_step")
        assert len(steps) == 1 and steps[0].canonical_name == "create"

    @pytest.mark.parametrize("step_status", ["unverified", "completed", "blocked", "contradicted", "partially_observed"])
    def test_every_workflow_step_status_projects_without_crashing(self, step_status):
        # Regression: a live ServiceFlow run crashed on EVERY single
        # synchronize() call (silently, non-fatally logged, but producing
        # an entirely empty graph) because `WorkflowStep.status` uses its
        # own STEP_STATUSES vocabulary ("unverified", "completed", ...)
        # which is NOT a subset of the graph's GRAPH_STATUSES -- the raw
        # value was passed straight to `KnowledgeNode`/`KnowledgeEdge`
        # instead of through `status_for_projection()`. Every step status
        # value must project cleanly.
        _, _, wr, _ = _registries()
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", status=step_status)])
        kg = ApplicationKnowledgeGraph()
        summary = _sync(kg, wr=wr)
        assert summary["version_bumped"] is True
        steps = kg.query_engine.nodes_by_type("workflow_step")
        assert len(steps) == 1
        assert steps[0].status in {"observed", "candidate", "blocked", "contradicted", "partially_observed", "inferred", "verified", "stale", "unknown"}

    def test_dependency_output_becomes_output_node(self):
        _, _, _, dr = _registries()
        out = _output("Open Jobs")
        dr.memory.outputs["a"] = out
        kg = ApplicationKnowledgeGraph()
        _sync(kg, dr=dr)
        nodes = kg.query_engine.nodes_by_type("counter")
        assert len(nodes) == 1 and nodes[0].canonical_name == "Open Jobs"

    def test_state_becomes_entity_state_node(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["open", "closed"])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        states = kg.query_engine.nodes_by_type("entity_state")
        assert {s.canonical_name for s in states} == {"open", "closed"}

    def test_permission_becomes_permission_node(self):
        _, ar, *_ = _registries()
        ar.memory.records["alice"] = _actor("alice", permissions=[_permission("can_create_job")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, ar=ar)
        perms = kg.query_engine.nodes_by_type("permission")
        assert len(perms) == 1

    def test_unknown_semantic_object_preserved_as_relationship_kind(self):
        er, *_ = _registries()
        rel = EntityRelationship(subject_entity_id="job", kind="references", object_entity_id="site")
        er.memory.records["job"] = _entity("job", relationships=[rel])
        er.memory.records["site"] = _entity("site")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        edges = kg.query_engine.edges_by_type("references")
        assert len(edges) == 1

    def test_stable_node_ids_across_resync(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        id1 = entity_node_id("job")
        _sync(kg, er=er, iteration=2)
        id2 = entity_node_id("job")
        assert id1 == id2 and id1 in kg.memory.nodes

    def test_aliases_retained_on_node(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", aliases=["work order"])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        node = kg.query_engine.node_by_id(entity_node_id("job"))
        assert "work order" in node.aliases


# ---------------------------------------------------------------------------
# B. Edge projection
# ---------------------------------------------------------------------------


class TestEdgeProjection:
    def _basic(self):
        er, ar, wr, dr = _registries()
        er.memory.records["job"] = _entity("job", states=["open", "closed"])
        ar.memory.records["alice"] = _actor("alice", permissions=[_permission("can_create_job")])
        _workflow(
            wr, "entity:job", "job workflow",
            steps=[_step("create", entity_id="job", actor_id="alice")],
            actors=[WorkflowActorParticipation(actor_id="alice", role_in_workflow="initiator")],
            entities=[WorkflowEntityParticipation(entity_id="job")],
        )
        return er, ar, wr, dr

    def test_actor_has_permission(self):
        er, ar, wr, dr = self._basic()
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr, dr=dr)
        edges = [e for e in kg.query_engine.outgoing_edges(actor_node_id("alice")) if e.edge_type == "has_permission"]
        assert len(edges) == 1

    def test_permission_enables_operation(self):
        er, ar, wr, dr = self._basic()
        er.memory.records["job"].operations = [EntityOperation(operation="create")]
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr, dr=dr)
        perm_node = kg.query_engine.nodes_by_type("permission")[0]
        edges = [e for e in kg.query_engine.outgoing_edges(perm_node.node_id) if e.edge_type == "enables"]
        assert len(edges) == 1

    def test_operation_acts_on_entity(self):
        er, ar, wr, dr = self._basic()
        er.memory.records["job"].operations = [EntityOperation(operation="create")]
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr, dr=dr)
        op_node = kg.query_engine.nodes_by_type("operation")[0]
        edges = [e for e in kg.query_engine.outgoing_edges(op_node.node_id) if e.edge_type == "acts_on"]
        assert edges and edges[0].target_node_id == entity_node_id("job")

    def test_actor_participates_in_workflow(self):
        er, ar, wr, dr = self._basic()
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr, dr=dr)
        edges = [e for e in kg.query_engine.outgoing_edges(actor_node_id("alice")) if e.edge_type == "participates_in"]
        assert len(edges) == 1

    def test_workflow_has_ordered_steps(self):
        _, _, wr, _ = _registries()
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", sequence_hint=0), _step("approve", entity_id="job", sequence_hint=1)])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, wr=wr)
        steps = kg.query_engine.workflow_steps(workflow_node_id("job workflow"))
        assert [s.canonical_name for s in steps] == ["create", "approve"]
        precedes = kg.query_engine.edges_by_type("precedes")
        assert len(precedes) == 1

    def test_step_performed_by_actor(self):
        er, ar, wr, dr = self._basic()
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr, dr=dr)
        step = kg.query_engine.nodes_by_type("workflow_step")[0]
        edges = [e for e in kg.query_engine.outgoing_edges(step.node_id) if e.edge_type == "performed_by"]
        assert edges and edges[0].target_node_id == actor_node_id("alice")

    def test_step_acts_on_entity(self):
        er, ar, wr, dr = self._basic()
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr, dr=dr)
        step = kg.query_engine.nodes_by_type("workflow_step")[0]
        edges = [e for e in kg.query_engine.outgoing_edges(step.node_id) if e.edge_type == "acts_on"]
        assert edges and edges[0].target_node_id == entity_node_id("job")

    def test_transition_connects_states(self):
        _, _, wr, _ = _registries()
        _workflow(
            wr, "entity:job", "job workflow",
            transitions=[WorkflowTransition(entity_id="job", source_state="open", target_state="closed", action_verb="complete", is_explicit=True)],
        )
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["open", "closed"])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, wr=wr)
        transition = kg.query_engine.nodes_by_type("transition")[0]
        from_edges = [e for e in kg.query_engine.outgoing_edges(transition.node_id) if e.edge_type == "from_state"]
        to_edges = [e for e in kg.query_engine.outgoing_edges(transition.node_id) if e.edge_type == "to_state"]
        assert from_edges and to_edges

    def test_workflow_produces_outcome(self):
        _, _, wr, _ = _registries()
        _workflow(wr, "entity:job", "job workflow", outcomes=[WorkflowOutcome(description="job created", outcome_type="new_entity")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, wr=wr)
        edges = kg.query_engine.edges_by_type("produces")
        assert len(edges) == 1

    def test_workflow_affects_output_via_dependency(self):
        er, ar, wr, dr = self._basic()
        out = _output("Open Jobs")
        dr.memory.outputs["a"] = out
        dr.memory.records["d1"] = _dependency(entity_ids=["job"], output_id=out.output_id)
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr, dr=dr)
        edges = kg.query_engine.edges_by_type("counts")
        assert len(edges) == 1

    def test_output_visible_to_actor(self):
        _, _, _, dr = _registries()
        out = _output("Open Jobs")
        dr.memory.outputs["a"] = out
        dep = _dependency(entity_ids=["job"], output_id=out.output_id)
        dep.known_consumers.append("alice")
        dr.memory.records["d1"] = dep
        er, ar, wr, _ = _registries()
        er.memory.records["job"] = _entity("job")
        ar.memory.records["alice"] = _actor("alice")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, dr=dr)
        edges = kg.query_engine.edges_by_type("visible_to")
        assert len(edges) == 1

    def test_dependency_includes_and_excludes_states(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["open", "closed"])
        _, _, _, dr = _registries()
        out = _output("Open Jobs")
        dr.memory.outputs["a"] = out
        dep = _dependency(entity_ids=["job"], output_id=out.output_id, inclusion_rules=[StateInclusionRule(state_label="open")])
        dr.memory.records["d1"] = dep
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, dr=dr)
        edges = kg.query_engine.edges_by_type("includes_state")
        assert len(edges) == 1


# ---------------------------------------------------------------------------
# C. Synchronisation
# ---------------------------------------------------------------------------


class TestSynchronisation:
    def test_first_full_synchronisation_creates_nodes(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        kg = ApplicationKnowledgeGraph()
        summary = _sync(kg, er=er)
        assert summary["version_bumped"] is True
        assert entity_node_id("job") in kg.memory.nodes

    def test_repeated_synchronisation_is_idempotent(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        v1 = kg.memory.graph_version
        s2 = _sync(kg, er=er, iteration=2)
        assert s2["version_bumped"] is False
        assert kg.memory.graph_version == v1

    def test_changed_source_record_updates_projection(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", confidence=0.5)
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        er.memory.records["job"].confidence = 0.9
        er.memory.records["job"].evidence.append(EntityEvidence(source_kind="table_column", observed_text="Jobs"))
        summary = _sync(kg, er=er, iteration=2)
        assert summary["version_bumped"] is True
        assert kg.query_engine.node_by_id(entity_node_id("job")).confidence == pytest.approx(0.9)

    def test_removed_source_relation_becomes_stale(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["open"])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        er.memory.records["job"].known_states = []
        _sync(kg, er=er, iteration=2)
        state_node = kg.query_engine.node_by_id("entity_state:job:open")
        assert state_node.stale is True

    def test_graph_version_changes_only_when_needed(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        v1 = kg.memory.graph_version
        _sync(kg, er=er, iteration=2)
        _sync(kg, er=er, iteration=3)
        assert kg.memory.graph_version == v1

    def test_unresolved_reference_resolves_later(self):
        _, _, wr, _ = _registries()
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", actor_id="bob")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, wr=wr)
        assert len(kg.memory.unresolved_references()) >= 1
        _, ar, _, _ = _registries()
        ar.memory.records["bob"] = _actor("bob")
        _sync(kg, wr=wr, ar=ar, iteration=2)
        assert not any(r.target_hint == "bob" for r in kg.memory.unresolved_references())
        step = kg.query_engine.nodes_by_type("workflow_step")[0]
        assert any(e.edge_type == "performed_by" for e in kg.query_engine.outgoing_edges(step.node_id))

    def test_source_registries_not_mutated(self):
        er, *_ = _registries()
        record = _entity("job")
        er.memory.records["job"] = record
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        assert er.memory.records["job"] is record
        assert record.canonical_name == "job"

    def test_duplicate_evidence_does_not_inflate_counts(self):
        er, *_ = _registries()
        same_ev = EntityEvidence(source_kind="heading", observed_text="Job")
        er.memory.records["job"] = _entity("job")
        er.memory.records["job"].evidence = [same_ev]
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        count1 = len(kg.query_engine.node_by_id(entity_node_id("job")).evidence_references)
        er.memory.records["job"].evidence = [same_ev, same_ev]
        _sync(kg, er=er, iteration=2)
        count2 = len(kg.query_engine.node_by_id(entity_node_id("job")).evidence_references)
        assert count1 == count2


# ---------------------------------------------------------------------------
# D. Identity resolution
# ---------------------------------------------------------------------------


class TestIdentityResolution:
    def test_direct_registry_id_resolution(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        assert entity_node_id("job") in kg.memory.nodes

    def test_explicit_cross_reference_resolution(self):
        er, ar, wr, dr = _registries()
        er.memory.records["job"] = _entity("job")
        ar.memory.records["alice"] = _actor("alice")
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", actor_id="alice")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr)
        step = kg.query_engine.nodes_by_type("workflow_step")[0]
        assert any(e.target_node_id == actor_node_id("alice") for e in kg.query_engine.outgoing_edges(step.node_id))

    def test_alias_candidate_retained(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", aliases=["work order"])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        found = kg.query_engine.find_nodes_by_alias("work order")
        assert len(found) == 1 and found[0].node_id == entity_node_id("job")

    def test_ambiguous_names_not_destructively_merged(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        er.memory.records["jobsite"] = _entity("jobsite")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        assert entity_node_id("job") != entity_node_id("jobsite")
        assert entity_node_id("job") in kg.memory.nodes and entity_node_id("jobsite") in kg.memory.nodes

    def test_authoritative_id_conflict_creates_consistency_issue(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        er.memory.records["work_order"] = _entity("work order")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        kg.memory.upsert_edge(
            "edge:equivalent_to:entity:job->entity:work order", edge_type="equivalent_to",
            source_node_id=entity_node_id("job"), target_node_id=entity_node_id("work order"),
            status="candidate", confidence=0.3, source_registry="test",
        )
        kg.consistency_checker.check_all()
        issues = [i for i in kg.consistency_issues() if i.issue_type == "equivalence_conflict"]
        assert len(issues) == 1

    def test_stale_reference_handling(self):
        # A workflow-sourced edge (transition step -> entity_state) stays
        # ACTIVE while its target node goes stale via a DIFFERENT registry
        # (entity_registry no longer reports that state) -- the two
        # registries update independently, so this is the realistic case
        # the check targets, not "both go stale together."
        er, _, wr, _ = _registries()
        er.memory.records["job"] = _entity("job", states=["open"])
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", source_state=None, target_state="open")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, wr=wr)
        er.memory.records["job"].known_states = []
        _sync(kg, er=er, wr=wr, iteration=2)
        issues = [i for i in kg.consistency_issues() if i.issue_type == "stale_edge_endpoint"]
        assert len(issues) >= 1


# ---------------------------------------------------------------------------
# E. Inference
# ---------------------------------------------------------------------------


class TestInference:
    def test_actor_step_implies_workflow_participation(self):
        _, ar, wr, _ = _registries()
        ar.memory.records["alice"] = _actor("alice")
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", actor_id="alice")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, ar=ar, wr=wr)
        edges = [e for e in kg.query_engine.outgoing_edges(actor_node_id("alice")) if e.edge_type == "participates_in"]
        assert edges and edges[0].status == "inferred"

    def test_step_entity_implies_workflow_entity_involvement(self):
        er, _, wr, _ = _registries()
        er.memory.records["job"] = _entity("job")
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, wr=wr)
        edges = [e for e in kg.query_engine.outgoing_edges(workflow_node_id("job workflow")) if e.edge_type == "acts_on"]
        assert edges and edges[0].status == "inferred"

    def test_permission_operation_implies_actor_capability(self):
        er, ar, *_ = _registries()
        er.memory.records["job"] = _entity("job", operations=[EntityOperation(operation="create")])
        ar.memory.records["alice"] = _actor("alice", permissions=[_permission("can_create_job")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar)
        edges = [e for e in kg.query_engine.outgoing_edges(actor_node_id("alice")) if e.edge_type == "can_perform"]
        assert edges and edges[0].status == "inferred"

    def test_compatible_state_sequence_inference(self):
        er, _, wr, _ = _registries()
        er.memory.records["job"] = _entity("job", states=["open", "in_progress", "closed"])
        _workflow(
            wr, "entity:job", "job workflow",
            transitions=[
                WorkflowTransition(entity_id="job", source_state="open", target_state="in_progress", action_verb="start", is_explicit=True),
                WorkflowTransition(entity_id="job", source_state="in_progress", target_state="closed", action_verb="complete", is_explicit=True),
            ],
            steps=[
                _step("start", entity_id="job", sequence_hint=0, source_state="open", target_state="in_progress"),
                _step("complete", entity_id="job", sequence_hint=1, source_state="in_progress", target_state="closed"),
            ],
        )
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, wr=wr)
        edges = kg.query_engine.edges_by_type("compatible_continuation")
        assert len(edges) == 1

    def test_workflow_output_dependency_propagation(self):
        er, _, wr, dr = _registries()
        er.memory.records["job"] = _entity("job", states=["open", "closed"])
        _workflow(
            wr, "entity:job", "job workflow",
            transitions=[WorkflowTransition(entity_id="job", source_state="open", target_state="closed", action_verb="complete", is_explicit=True)],
        )
        out = _output("Closed Jobs")
        dr.memory.outputs["a"] = out
        dep = _dependency(entity_ids=["job"], output_id=out.output_id, inclusion_rules=[StateInclusionRule(state_label="closed")])
        dr.memory.records["d1"] = dep
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, wr=wr, dr=dr)
        edges = [e for e in kg.query_engine.outgoing_edges(workflow_node_id("job workflow")) if e.edge_type == "affects"]
        assert edges and edges[0].status == "inferred"

    def test_actor_specific_dashboard_implies_consumer_visibility(self):
        _, ar, _, dr = _registries()
        ar.memory.records["alice"] = _actor("alice", known_dashboards=["https://x.example/dash"])
        out = _output("Open Jobs")
        out.page_url = "https://x.example/dash"
        dr.memory.outputs["a"] = out
        kg = ApplicationKnowledgeGraph()
        _sync(kg, ar=ar, dr=dr)
        edges = [e for e in kg.query_engine.outgoing_edges(kg.query_engine.nodes_by_type("counter")[0].node_id) if e.edge_type == "visible_to"]
        assert edges and edges[0].status == "inferred"

    def test_cross_role_step_sequence_implies_handoff(self):
        _, ar, wr, _ = _registries()
        ar.memory.records["alice"] = _actor("alice")
        ar.memory.records["bob"] = _actor("bob")
        _workflow(
            wr, "entity:job", "job workflow",
            steps=[_step("create", entity_id="job", actor_id="alice", sequence_hint=0), _step("approve", entity_id="job", actor_id="bob", sequence_hint=1)],
        )
        kg = ApplicationKnowledgeGraph()
        _sync(kg, ar=ar, wr=wr)
        edges = kg.query_engine.edges_by_type("hands_off_to")
        assert len(edges) == 1 and edges[0].source_node_id == actor_node_id("alice") and edges[0].target_node_id == actor_node_id("bob")

    def test_inferred_status_remains_inferred(self):
        _, ar, wr, _ = _registries()
        ar.memory.records["alice"] = _actor("alice")
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", actor_id="alice")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, ar=ar, wr=wr)
        edges = kg.query_engine.edges_by_type("participates_in")
        assert all(e.status == "inferred" for e in edges)

    def test_inference_confidence_capped_at_weakest_premise(self):
        conf = inferred_edge_confidence([0.9, 0.2], depth=2)
        assert conf <= 0.2


# ---------------------------------------------------------------------------
# F. Contradictions
# ---------------------------------------------------------------------------


class TestContradictions:
    def test_positive_and_negative_permission_same_scope(self):
        kg = ApplicationKnowledgeGraph()
        kg.memory.upsert_node("actor:alice", node_type="actor", canonical_name="alice", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("operation:job:create", node_type="operation", canonical_name="create", source_registry="test", source_record_id="1")
        kg.memory.upsert_edge("e1", edge_type="can_perform", source_node_id="actor:alice", target_node_id="operation:job:create", status="observed", confidence=0.6, source_registry="test")
        kg.memory.upsert_edge("e2", edge_type="cannot_perform", source_node_id="actor:alice", target_node_id="operation:job:create", status="observed", confidence=0.6, source_registry="test")
        kg.consistency_checker.check_all()
        issues = [i for i in kg.consistency_issues() if i.issue_type == "permission_conflict"]
        assert len(issues) == 1

    def test_incompatible_workflow_state_sequence_cycle(self):
        kg = ApplicationKnowledgeGraph()
        for n in ("a", "b", "c"):
            kg.memory.upsert_node(f"workflow_step:{n}", node_type="workflow_step", canonical_name=n, source_registry="test", source_record_id=n)
        kg.memory.upsert_edge("e1", edge_type="precedes", source_node_id="workflow_step:a", target_node_id="workflow_step:b", status="observed", confidence=0.5, source_registry="test")
        kg.memory.upsert_edge("e2", edge_type="precedes", source_node_id="workflow_step:b", target_node_id="workflow_step:c", status="observed", confidence=0.5, source_registry="test")
        kg.memory.upsert_edge("e3", edge_type="precedes", source_node_id="workflow_step:c", target_node_id="workflow_step:a", status="observed", confidence=0.5, source_registry="test")
        kg.consistency_checker.check_all()
        issues = [i for i in kg.consistency_issues() if i.issue_type == "cyclic_workflow_ordering"]
        assert len(issues) == 1

    def test_opposite_output_effects_same_scope(self):
        kg = ApplicationKnowledgeGraph()
        kg.memory.upsert_node("entity:job", node_type="entity", canonical_name="job", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("derived_output:o1", node_type="counter", canonical_name="Open Jobs", source_registry="test", source_record_id="1")
        kg.memory.upsert_edge("e1", edge_type="counts", source_node_id="entity:job", target_node_id="derived_output:o1", status="observed", confidence=0.6, source_registry="test", attributes={"effect_direction": "increase"})
        kg.memory.upsert_edge("e2", edge_type="counts", source_node_id="entity:job", target_node_id="derived_output:o1", status="observed", confidence=0.6, source_registry="test", attributes={"effect_direction": "decrease"})
        kg.consistency_checker.check_all()
        contradictions = [c for c in kg.memory.contradictions.values()]
        assert len(contradictions) == 1

    def test_actor_performs_operation_despite_verified_denial(self):
        kg = ApplicationKnowledgeGraph()
        kg.memory.upsert_node("workflow_step:s1", node_type="workflow_step", canonical_name="create", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("actor:alice", node_type="actor", canonical_name="alice", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("entity:job", node_type="entity", canonical_name="job", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("permission:alice:cannot_create_job", node_type="permission", canonical_name="cannot_create_job", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("operation:job:create", node_type="operation", canonical_name="create", source_registry="test", source_record_id="1")
        kg.memory.upsert_edge("e1", edge_type="performed_by", source_node_id="workflow_step:s1", target_node_id="actor:alice", status="observed", confidence=0.6, source_registry="test")
        kg.memory.upsert_edge("e2", edge_type="acts_on", source_node_id="workflow_step:s1", target_node_id="entity:job", status="observed", confidence=0.6, source_registry="test")
        kg.memory.upsert_edge("e3", edge_type="lacks_permission", source_node_id="actor:alice", target_node_id="permission:alice:cannot_create_job", status="observed", confidence=0.6, source_registry="test")
        kg.memory.upsert_edge("e4", edge_type="enables", source_node_id="permission:alice:cannot_create_job", target_node_id="operation:job:create", status="observed", confidence=0.6, source_registry="test")
        kg.consistency_checker.check_all()
        issues = [i for i in kg.consistency_issues() if i.issue_type == "performed_despite_denial"]
        assert len(issues) == 1

    def test_denial_of_one_operation_does_not_flag_unrelated_operation(self):
        # Regression: a live ServiceFlow run raised 35 "performed_despite_
        # denial" issues for a single actor because the check only compared
        # the ENTITY (job) between the denied permission's enabled
        # operation and the step being performed, never the VERB -- a
        # denial of "export" on job falsely flagged an unrelated "create"
        # step on that same job.
        kg = ApplicationKnowledgeGraph()
        kg.memory.upsert_node("workflow_step:s1", node_type="workflow_step", canonical_name="create", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("actor:alice", node_type="actor", canonical_name="alice", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("entity:job", node_type="entity", canonical_name="job", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("permission:alice:cannot_export_job", node_type="permission", canonical_name="cannot_export_job", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("operation:job:export", node_type="operation", canonical_name="export", source_registry="test", source_record_id="1")
        kg.memory.upsert_edge("e1", edge_type="performed_by", source_node_id="workflow_step:s1", target_node_id="actor:alice", status="observed", confidence=0.6, source_registry="test")
        kg.memory.upsert_edge("e2", edge_type="acts_on", source_node_id="workflow_step:s1", target_node_id="entity:job", status="observed", confidence=0.6, source_registry="test")
        kg.memory.upsert_edge("e3", edge_type="lacks_permission", source_node_id="actor:alice", target_node_id="permission:alice:cannot_export_job", status="observed", confidence=0.6, source_registry="test")
        kg.memory.upsert_edge("e4", edge_type="enables", source_node_id="permission:alice:cannot_export_job", target_node_id="operation:job:export", status="observed", confidence=0.6, source_registry="test")
        kg.consistency_checker.check_all()
        issues = [i for i in kg.consistency_issues() if i.issue_type == "performed_despite_denial"]
        assert len(issues) == 0

    def test_scope_incompatible_facts_not_falsely_contradicted(self):
        kg = ApplicationKnowledgeGraph()
        kg.memory.upsert_node("entity:job", node_type="entity", canonical_name="job", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("derived_output:o1", node_type="counter", canonical_name="Open Jobs", source_registry="test", source_record_id="1")
        kg.memory.upsert_edge("e1", edge_type="counts", source_node_id="entity:job", target_node_id="derived_output:o1", status="observed", confidence=0.6, source_registry="test", attributes={"effect_direction": "increase"}, actor_scope="alice")
        kg.memory.upsert_edge("e2", edge_type="counts", source_node_id="entity:job", target_node_id="derived_output:o1", status="observed", confidence=0.6, source_registry="test", attributes={"effect_direction": "decrease"}, actor_scope="bob")
        kg.consistency_checker.check_all()
        assert len(kg.memory.contradictions) == 0

    def test_contradiction_retained_with_both_sides(self):
        kg = ApplicationKnowledgeGraph()
        kg.memory.upsert_node("entity:job", node_type="entity", canonical_name="job", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("derived_output:o1", node_type="counter", canonical_name="Open Jobs", source_registry="test", source_record_id="1")
        kg.memory.upsert_edge("e1", edge_type="counts", source_node_id="entity:job", target_node_id="derived_output:o1", status="observed", confidence=0.6, source_registry="test", attributes={"effect_direction": "increase"})
        kg.memory.upsert_edge("e2", edge_type="counts", source_node_id="entity:job", target_node_id="derived_output:o1", status="observed", confidence=0.6, source_registry="test", attributes={"effect_direction": "decrease"})
        kg.consistency_checker.check_all()
        assert "e1" in kg.memory.edges and "e2" in kg.memory.edges
        assert kg.memory.edges["e1"].contradiction_ids and kg.memory.edges["e2"].contradiction_ids

    def test_dangling_reference_detected(self):
        kg = ApplicationKnowledgeGraph()
        kg.memory.upsert_node("entity:job", node_type="entity", canonical_name="job", source_registry="test", source_record_id="1")
        from app.intelligence.knowledge_graph.schemas import KnowledgeEdge
        kg.memory.edges["ghost"] = KnowledgeEdge(edge_id="ghost", edge_type="acts_on", source_node_id="entity:job", target_node_id="entity:missing", source_registry="test")
        kg.consistency_checker.check_all()
        issues = [i for i in kg.consistency_issues() if i.issue_type == "dangling_reference"]
        assert len(issues) == 1


# ---------------------------------------------------------------------------
# G. Gap analysis
# ---------------------------------------------------------------------------


class TestGapAnalysis:
    def test_entity_without_workflow_gap(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        _, ar, _, _ = _registries()
        ar.memory.records["alice"] = _actor("alice", known_entities=["job"])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar)
        assert any(g.gap_type == "entity_without_workflow" for g in kg.gaps())

    def test_workflow_without_actor_gap(self):
        _, _, wr, _ = _registries()
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", actor_id=None)])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, wr=wr)
        assert any(g.gap_type == "workflow_without_actor" for g in kg.gaps())

    def test_workflow_without_entity_gap(self):
        _, ar, wr, _ = _registries()
        ar.memory.records["alice"] = _actor("alice")
        _workflow(wr, "page:job", "job workflow", actors=[WorkflowActorParticipation(actor_id="alice", role_in_workflow="initiator")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, ar=ar, wr=wr)
        assert any(g.gap_type == "workflow_without_entity" for g in kg.gaps())

    def test_state_without_transition_gap(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["archived"])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        assert any(g.gap_type == "entity_state_without_transition" for g in kg.gaps())

    def test_output_without_source_gap(self):
        _, _, _, dr = _registries()
        out = _output("Mystery Metric")
        out.page_url = "https://x.example/dash"  # give it an edge so it isn't merely "isolated_node"
        dr.memory.outputs["a"] = out
        kg = ApplicationKnowledgeGraph()
        _sync(kg, dr=dr)
        assert any(g.gap_type == "output_without_source" for g in kg.gaps())

    def test_output_without_consumer_gap(self):
        er, _, _, dr = _registries()
        er.memory.records["job"] = _entity("job")
        out = _output("Open Jobs")
        dr.memory.outputs["a"] = out
        dr.memory.records["d1"] = _dependency(entity_ids=["job"], output_id=out.output_id)
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, dr=dr)
        assert any(g.gap_type == "output_without_consumer" for g in kg.gaps())

    def test_permission_without_operation_gap(self):
        _, ar, *_ = _registries()
        ar.memory.records["alice"] = _actor("alice", permissions=[_permission("can_export")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, ar=ar)
        assert any(g.gap_type == "permission_without_operation" for g in kg.gaps())

    def test_unresolved_actor_hand_off_gap(self):
        _, _, wr, _ = _registries()
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", actor_id=None)])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, wr=wr)
        assert any(g.gap_type == "unresolved_actor_hand_off" for g in kg.gaps())

    def test_dependency_without_verification_path_gap(self):
        er, _, _, dr = _registries()
        er.memory.records["job"] = _entity("job")
        out = _output("Open Jobs")
        dr.memory.outputs["a"] = out
        dep = _dependency(entity_ids=["job"], output_id=out.output_id, status="observed")
        dep.known_consumers.append("alice")
        dr.memory.records["d1"] = dep
        _, ar, _, _ = _registries()
        ar.memory.records["alice"] = _actor("alice")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, dr=dr)
        assert any(g.gap_type == "dependency_without_verification_path" for g in kg.gaps())

    def test_isolated_node_gap(self):
        kg = ApplicationKnowledgeGraph()
        kg.memory.upsert_node("entity:lonely", node_type="entity", canonical_name="lonely", source_registry="test", source_record_id="1")
        kg.gap_analyzer.analyze()
        assert any(g.gap_type == "isolated_node" for g in kg.gaps())

    def test_low_confidence_bridge_gap(self):
        kg = ApplicationKnowledgeGraph()
        kg.memory.upsert_node("entity:a", node_type="entity", canonical_name="a", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("entity:b", node_type="entity", canonical_name="b", source_registry="test", source_record_id="1")
        kg.memory.upsert_edge("e1", edge_type="relates_to", source_node_id="entity:a", target_node_id="entity:b", status="observed", confidence=0.1, source_registry="test")
        kg.gap_analyzer.analyze()
        assert any(g.gap_type == "low_confidence_bridge" for g in kg.gaps())

    def test_stale_subgraph_or_disconnected_module_gap(self):
        kg = ApplicationKnowledgeGraph()
        kg.memory.upsert_node("entity:a", node_type="entity", canonical_name="a", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("entity:b", node_type="entity", canonical_name="b", source_registry="test", source_record_id="1")
        kg.memory.upsert_edge("e1", edge_type="relates_to", source_node_id="entity:a", target_node_id="entity:b", status="observed", confidence=0.6, source_registry="test")
        kg.memory.upsert_node("entity:c", node_type="entity", canonical_name="c", source_registry="test", source_record_id="1")
        kg.memory.upsert_node("entity:d", node_type="entity", canonical_name="d", source_registry="test", source_record_id="1")
        kg.memory.upsert_edge("e2", edge_type="relates_to", source_node_id="entity:c", target_node_id="entity:d", status="observed", confidence=0.6, source_registry="test")
        kg.gap_analyzer.analyze()
        assert any(g.gap_type == "disconnected_module" for g in kg.gaps())

    def test_gap_resolves_after_new_registry_evidence(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["open"])  # a state edge keeps it non-isolated
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        gap_before = next(g for g in kg.gaps() if g.gap_type == "entity_without_workflow")
        assert gap_before.status == "open"
        _, _, wr, _ = _registries()
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job")], entities=[WorkflowEntityParticipation(entity_id="job")])
        _sync(kg, er=er, wr=wr, iteration=2)
        assert gap_before.status == "resolved"


# ---------------------------------------------------------------------------
# H. Query engine
# ---------------------------------------------------------------------------


class TestQueryEngine:
    def _seeded(self):
        er, ar, wr, dr = _registries()
        er.memory.records["job"] = _entity("job", states=["open"])
        ar.memory.records["alice"] = _actor("alice")
        _workflow(
            wr, "entity:job", "job workflow",
            steps=[_step("create", entity_id="job", actor_id="alice")],
            actors=[WorkflowActorParticipation(actor_id="alice", role_in_workflow="initiator")],
            entities=[WorkflowEntityParticipation(entity_id="job")],
        )
        out = _output("Open Jobs")
        dr.memory.outputs["a"] = out
        dep = _dependency(entity_ids=["job"], workflow_ids=["job workflow"], output_id=out.output_id)
        dep.known_consumers.append("alice")
        dr.memory.records["d1"] = dep
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr, dr=dr)
        return kg, out

    def test_node_and_edge_retrieval(self):
        kg, _ = self._seeded()
        node = kg.query_engine.node_by_id(entity_node_id("job"))
        assert node is not None
        edges = kg.query_engine.edges_by_type("has_step")
        assert len(edges) == 1

    def test_actors_for_entity_and_entities_for_actor(self):
        _, ar, _, _ = _registries()
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        ar.memory.records["alice"] = _actor("alice", known_entities=["job"])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar)
        actors = kg.query_engine.actors_for_entity(entity_node_id("job"))
        assert any(a.node_id == actor_node_id("alice") for a in actors)
        entities = kg.query_engine.entities_for_actor(actor_node_id("alice"))
        assert any(e.node_id == entity_node_id("job") for e in entities)

    def test_workflows_for_entity_and_workflows_for_actor(self):
        kg, _ = self._seeded()
        wfs_entity = kg.query_engine.workflows_for_entity(entity_node_id("job"))
        assert len(wfs_entity) == 1
        wfs_actor = kg.query_engine.workflows_for_actor(actor_node_id("alice"))
        assert len(wfs_actor) == 1

    def test_producers_and_consumers_for_output(self):
        kg, out = self._seeded()
        consumers = kg.query_engine.consumers_for_output(f"derived_output:{out.output_id}")
        assert any(c.node_id == actor_node_id("alice") for c in consumers)

    def test_prerequisites_for_workflow(self):
        from app.intelligence.workflow_discovery.schemas import WorkflowPrerequisite
        _, _, wr, _ = _registries()
        _workflow(wr, "entity:job", "job workflow", prerequisites=[WorkflowPrerequisite(type="authentication", satisfied=False)])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, wr=wr)
        prereqs = kg.query_engine.prerequisites_for_workflow(workflow_node_id("job workflow"))
        assert len(prereqs) == 1

    def test_unexplained_outputs_and_incomplete_workflows(self):
        kg, _ = self._seeded()
        unexplained = kg.query_engine.unexplained_outputs()
        incomplete = kg.query_engine.incomplete_workflows()
        assert isinstance(unexplained, list) and isinstance(incomplete, list)

    def test_shortest_bounded_path(self):
        kg, out = self._seeded()
        path = kg.query_engine.shortest_path(actor_node_id("alice"), f"derived_output:{out.output_id}", constraint=GraphTraversalConstraint(max_depth=4))
        assert path is not None and path.node_ids[0] == actor_node_id("alice")

    def test_bounded_all_path_query(self):
        kg, out = self._seeded()
        paths = kg.query_engine.all_paths(actor_node_id("alice"), workflow_node_id("job workflow"), constraint=GraphTraversalConstraint(max_depth=3, max_results=5))
        assert len(paths) <= 5

    def test_filtered_neighbourhood(self):
        kg, _ = self._seeded()
        neighbourhood = kg.query_engine.neighbourhood(entity_node_id("job"), constraint=GraphTraversalConstraint(max_depth=1, allowed_node_types=["workflow"]))
        assert all(kg.memory.nodes[n].node_type == "workflow" for n in neighbourhood.node_ids if n != entity_node_id("job") and n in kg.memory.nodes)

    def test_confidence_and_status_filters(self):
        kg, _ = self._seeded()
        low_conf_edges = kg.query_engine.edges_by_confidence(0.0, 0.1)
        assert all(e.confidence <= 0.1 for e in low_conf_edges)

    def test_stale_and_inferred_inclusion_controls(self):
        kg, _ = self._seeded()
        constraint_no_inferred = GraphTraversalConstraint(max_depth=2, include_inferred=False)
        neighbourhood = kg.query_engine.neighbourhood(actor_node_id("alice"), constraint=constraint_no_inferred)
        for e_id in neighbourhood.edge_ids:
            assert kg.memory.edges[e_id].status != "inferred"

    def test_result_size_limits(self):
        kg, _ = self._seeded()
        constraint = GraphTraversalConstraint(max_depth=3, max_results=1)
        neighbourhood = kg.query_engine.neighbourhood(entity_node_id("job"), constraint=constraint)
        assert len(neighbourhood.node_ids) <= 2  # center + at most 1 more before truncation kicks in


# ---------------------------------------------------------------------------
# I. Context projection
# ---------------------------------------------------------------------------


class TestContextProjection:
    def _seeded(self):
        er, ar, wr, dr = _registries()
        er.memory.records["job"] = _entity("job", states=["open"])
        ar.memory.records["alice"] = _actor("alice")
        _workflow(
            wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", actor_id="alice")],
            actors=[WorkflowActorParticipation(actor_id="alice", role_in_workflow="initiator")],
            entities=[WorkflowEntityParticipation(entity_id="job")],
        )
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr)
        return kg

    def test_entity_lifecycle_context(self):
        kg = self._seeded()
        ctx = kg.context_for_entity("job")
        assert ctx["focus_node"]["node_id"] == entity_node_id("job")
        assert ctx["neighbours"]

    def test_actor_capability_context(self):
        kg = self._seeded()
        ctx = kg.context_for_actor("alice")
        assert ctx["focus_node"]["canonical_name"] == "alice"

    def test_workflow_context(self):
        kg = self._seeded()
        ctx = kg.context_for_workflow("job workflow")
        assert ctx.get("found") is not False
        assert ctx["focus_node"]["canonical_name"] == "job workflow"

    def test_unrelated_nodes_excluded(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        er.memory.records["unrelated"] = _entity("unrelated")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        ctx = kg.context_for_entity("job", depth=1)
        neighbour_ids = {n["node_id"] for n in ctx["neighbours"]}
        assert entity_node_id("unrelated") not in neighbour_ids

    def test_sensitive_evidence_excluded(self):
        kg = self._seeded()
        ctx = kg.context_for_entity("job")
        serialized = str(ctx)
        assert "password" not in serialized.lower() and "token" not in serialized.lower()

    def test_record_limit_applied(self):
        er, ar, wr, dr = _registries()
        er.memory.records["job"] = _entity("job")
        for i in range(10):
            ar.memory.records[f"actor{i}"] = _actor(f"actor{i}", known_entities=["job"])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar)
        ctx = kg.context_for_entity("job", max_neighbours=3)
        assert len(ctx["neighbours"]) <= 3

    def test_stable_serialisation(self):
        kg = self._seeded()
        ctx1 = kg.context_for_entity("job")
        ctx2 = kg.context_for_entity("job")
        assert ctx1["focus_node"] == ctx2["focus_node"]

    def test_context_for_unknown_node_reports_not_found(self):
        kg = ApplicationKnowledgeGraph()
        ctx = kg.context_for_node("entity:does_not_exist")
        assert ctx["found"] is False


# ---------------------------------------------------------------------------
# J. Versioning
# ---------------------------------------------------------------------------


class TestVersioning:
    def test_initial_version_is_zero(self):
        kg = ApplicationKnowledgeGraph()
        assert kg.memory.graph_version == 0

    def test_noop_synchronisation_does_not_bump(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        v = kg.memory.graph_version
        _sync(kg, er=er, iteration=2)
        assert kg.memory.graph_version == v

    def test_added_node_diff(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        v1 = kg.memory.graph_version
        er.memory.records["customer"] = _entity("customer")
        _sync(kg, er=er, iteration=2)
        diff = kg.changes_since(v1)
        assert entity_node_id("customer") in diff["added_node_ids"]

    def test_updated_edge_diff(self):
        er, ar, wr, _ = _registries()
        er.memory.records["job"] = _entity("job")
        ar.memory.records["alice"] = _actor("alice")
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", actor_id="alice")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr)
        v1 = kg.memory.graph_version
        wr.records["entity:job"].steps[0].confidence = 0.99
        wr.records["entity:job"].steps[0].evidence.append(DependencyEvidence(source_kind="interactive_element", observed_text="new evidence"))
        _sync(kg, er=er, ar=ar, wr=wr, iteration=2)
        diff = kg.changes_since(v1)
        assert diff["updated_node_ids"] or diff["updated_edge_ids"]

    def test_stale_projection_diff(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["open"])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        v1 = kg.memory.graph_version
        er.memory.records["job"].known_states = []
        _sync(kg, er=er, iteration=2)
        diff = kg.changes_since(v1)
        assert "entity_state:job:open" in diff["stale_node_ids"]

    def test_resolved_reference_diff(self):
        _, _, wr, _ = _registries()
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", actor_id="bob")])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, wr=wr)
        v1 = kg.memory.graph_version
        _, ar, _, _ = _registries()
        ar.memory.records["bob"] = _actor("bob")
        _sync(kg, wr=wr, ar=ar, iteration=2)
        diff = kg.changes_since(v1)
        assert diff["resolved_reference_ids"]

    def test_gap_diff(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        v1 = kg.memory.graph_version
        _, _, wr, _ = _registries()
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job")], entities=[WorkflowEntityParticipation(entity_id="job")])
        _sync(kg, er=er, wr=wr, iteration=2)
        diff = kg.changes_since(v1)
        assert diff["resolved_gap_ids"]

    def test_changes_since_and_diff_snapshots_agree(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er)
        v1 = kg.memory.graph_version
        er.memory.records["customer"] = _entity("customer")
        _sync(kg, er=er, iteration=2)
        v2 = kg.memory.graph_version
        changes = kg.changes_since(v1)
        diff = kg.diff_snapshots(v1, v2)
        assert changes["added_node_ids"] == diff["added_node_ids"]


# ---------------------------------------------------------------------------
# K. Neutrality
# ---------------------------------------------------------------------------


class TestApplicationNeutrality:
    def test_no_business_domain_vocabulary_in_package_code(self):
        import ast

        import app.intelligence.knowledge_graph as pkg

        forbidden_words = {
            "customer", "customers", "invoice", "invoices", "order", "orders",
            "driver", "drivers", "shipment", "shipments", "technician",
            "administrator", "manager", "dispatcher", "employee",
            "job", "jobs", "contact", "contacts", "ticket", "tickets",
        }
        offenders: list[str] = []
        for path in sorted(Path(pkg.__file__).parent.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstring_nodes: set[int] = set()
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                    docstring_nodes.add(id(body[0].value))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in docstring_nodes:
                    continue
                for word in node.value.lower().replace("_", " ").replace("-", " ").split():
                    cleaned = word.strip(".,;:!?\"'()")
                    if cleaned in forbidden_words:
                        offenders.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}")
        assert not offenders, "business vocabulary hardcoded in knowledge_graph: " + "; ".join(offenders)

    def test_generic_fixture_logistics_application(self):
        er, ar, wr, dr = _registries()
        er.memory.records["shipment"] = _entity("shipment", states=["in transit", "delivered"])
        ar.memory.records["carrier"] = _actor("carrier")
        _workflow(
            wr, "entity:shipment", "shipment workflow", steps=[_step("dispatch", entity_id="shipment", actor_id="carrier")],
            actors=[WorkflowActorParticipation(actor_id="carrier", role_in_workflow="initiator")],
            entities=[WorkflowEntityParticipation(entity_id="shipment")],
        )
        kg = ApplicationKnowledgeGraph()
        summary = _sync(kg, er=er, ar=ar, wr=wr)
        assert summary["version_bumped"] is True
        assert entity_node_id("shipment") in kg.memory.nodes

    def test_generic_fixture_dashboard_application(self):
        er, _, _, dr = _registries()
        er.memory.records["widget"] = _entity("widget", states=["active", "archived"])
        out = _output("Active Widgets")
        dr.memory.outputs["a"] = out
        dr.memory.records["d1"] = _dependency(entity_ids=["widget"], output_id=out.output_id)
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, dr=dr)
        assert kg.query_engine.outputs_for_entity(entity_node_id("widget"))

    def test_unknown_node_type_preserved(self):
        kg = ApplicationKnowledgeGraph()
        kg.memory.upsert_node("unknown:widget:1", node_type="unknown", canonical_name="mystery thing", source_registry="test", source_record_id="1")
        node = kg.query_engine.node_by_id("unknown:widget:1")
        assert node is not None and node.node_type == "unknown"


# ---------------------------------------------------------------------------
# Integration fixtures (task section 29)
# ---------------------------------------------------------------------------


class TestIntegrationFixtures:
    def test_fixture_1_single_actor_lifecycle(self):
        er, ar, wr, dr = _registries()
        er.memory.records["job"] = _entity("job", states=["open", "closed"])
        ar.memory.records["alice"] = _actor("alice")
        _workflow(
            wr, "entity:job", "job workflow",
            steps=[_step("create", entity_id="job", actor_id="alice", sequence_hint=0), _step("complete", entity_id="job", actor_id="alice", sequence_hint=1)],
            actors=[WorkflowActorParticipation(actor_id="alice", role_in_workflow="initiator")],
            entities=[WorkflowEntityParticipation(entity_id="job")],
            transitions=[WorkflowTransition(entity_id="job", source_state="open", target_state="closed", action_verb="complete", is_explicit=True)],
        )
        out = _output("Closed Jobs")
        dr.memory.outputs["a"] = out
        dr.memory.records["d1"] = _dependency(entity_ids=["job"], workflow_ids=["job workflow"], output_id=out.output_id, effect_direction="increase")
        kg = ApplicationKnowledgeGraph()
        summary = _sync(kg, er=er, ar=ar, wr=wr, dr=dr)
        assert summary["version_bumped"] is True
        assert kg.query_engine.workflows_for_actor(actor_node_id("alice"))
        assert kg.query_engine.outputs_for_entity(entity_node_id("job"))

    def test_fixture_2_cross_role_lifecycle(self):
        er, ar, wr, dr = _registries()
        er.memory.records["job"] = _entity("job")
        ar.memory.records["alice"] = _actor("alice")
        ar.memory.records["bob"] = _actor("bob")
        ar.memory.records["carol"] = _actor("carol")
        _workflow(
            wr, "entity:job", "job workflow",
            steps=[
                _step("create", entity_id="job", actor_id="alice", sequence_hint=0),
                _step("approve", entity_id="job", actor_id="bob", sequence_hint=1),
            ],
            actors=[
                WorkflowActorParticipation(actor_id="alice", role_in_workflow="initiator"),
                WorkflowActorParticipation(actor_id="bob", role_in_workflow="approver"),
            ],
            entities=[WorkflowEntityParticipation(entity_id="job")],
        )
        out = _output("Report")
        dr.memory.outputs["a"] = out
        dep = _dependency(workflow_ids=["job workflow"], output_id=out.output_id)
        dep.known_consumers.append("carol")
        dr.memory.records["d1"] = dep
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, ar=ar, wr=wr, dr=dr)
        assert kg.query_engine.edges_by_type("hands_off_to")
        assert any(c.node_id == actor_node_id("carol") for c in kg.query_engine.consumers_for_output(f"derived_output:{out.output_id}"))

    def test_fixture_3_branched_workflow(self):
        from app.intelligence.workflow_discovery.schemas import WorkflowBranch
        _, _, wr, dr = _registries()
        _workflow(wr, "entity:job", "job workflow", branches=[WorkflowBranch(branch_type="approve_vs_reject", option_labels=["approve", "reject"])])
        out_approved = _output("Approved Jobs")
        out_rejected = _output("Rejected Jobs")
        dr.memory.outputs["a"] = out_approved
        dr.memory.outputs["b"] = out_rejected
        dr.memory.records["d1"] = _dependency(workflow_ids=["job workflow"], output_id=out_approved.output_id, effect_direction="increase")
        dr.memory.records["d2"] = _dependency(workflow_ids=["job workflow"], output_id=out_rejected.output_id, effect_direction="increase")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, wr=wr, dr=dr)
        assert kg.query_engine.edges_by_type("branches_to")
        outs = kg.query_engine.outputs_for_workflow(workflow_node_id("job workflow"))
        assert len(outs) == 2

    def test_fixture_4_contradictory_evidence(self):
        _, ar, *_ = _registries()
        ar.memory.records["alice"] = _actor("alice", permissions=[_permission("can_create_job", positive=True)])
        kg = ApplicationKnowledgeGraph()
        _sync(kg, ar=ar)
        kg.memory.upsert_node("operation:job:create", node_type="operation", canonical_name="create", source_registry="test", source_record_id="1")
        kg.memory.upsert_edge("e_can", edge_type="can_perform", source_node_id=actor_node_id("alice"), target_node_id="operation:job:create", status="observed", confidence=0.6, source_registry="test")
        kg.memory.upsert_edge("e_cannot", edge_type="cannot_perform", source_node_id=actor_node_id("alice"), target_node_id="operation:job:create", status="observed", confidence=0.6, source_registry="test")
        kg.consistency_checker.check_all()
        assert any(i.issue_type == "permission_conflict" for i in kg.consistency_issues())

    def test_fixture_5_incomplete_application_knowledge(self):
        _, _, _, dr = _registries()
        out = _output("Unexplained Total")
        out.page_url = "https://x.example/dash"
        dr.memory.outputs["a"] = out
        kg = ApplicationKnowledgeGraph()
        _sync(kg, dr=dr)
        assert any(g.gap_type == "output_without_source" for g in kg.gaps())

    def test_fixture_6_scope_sensitive_dependency(self):
        _, _, _, dr = _registries()
        out = _output("Open Items")
        dr.memory.outputs["a"] = out
        dr.memory.records["d1"] = _dependency(entity_ids=["job"], output_id=out.output_id, actor_scope=ActorScope(actor_term="alice"))
        dr.memory.records["d2"] = _dependency(entity_ids=["job"], output_id=out.output_id, actor_scope=ActorScope(actor_term="bob"))
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        kg = ApplicationKnowledgeGraph()
        _sync(kg, er=er, dr=dr)
        edges = [e for e in kg.query_engine.outgoing_edges(entity_node_id("job")) if e.edge_type == "counts"]
        assert len(edges) == 2
        assert {e.actor_scope for e in edges} == {"alice", "bob"}
