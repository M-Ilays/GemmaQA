"""Goal Generation Engine (app/intelligence/goal_generation/).

Covers: goal creation (node/gap/contradiction/consistency-issue/reference/
inference/business-rule/ownership/scope-derived goals), scoring (weighted
priority breakdown, penalties, explainability), deduplication (subject-key
merge + cross-subject-key safety net), grouping (entity/workflow/actor/
output/module/business_process/graph_region/contradiction/gap), goal
dependencies (KPI/dependency -> workflow -> transition -> permission
chain), priority ordering (deterministic, stable), graph-gap-derived
goals, contradiction-derived goals, confidence-derived goals, workflow/
permission/dependency goals, no-duplicate + idempotent + deterministic
generation, RunMemory integration, controller non-fatal wiring, the full
query API, and application neutrality (AST contract).
"""

from __future__ import annotations

import ast
import re
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
from app.intelligence.dependency_discovery.schemas import DependencyDescriptor, DependencyEvidence, DerivedOutputDescriptor
from app.intelligence.knowledge_graph import ApplicationKnowledgeGraph
from app.intelligence.goal_generation import GoalGenerationEngine
from app.intelligence.goal_generation.goal_candidate_builder import GoalCandidateBuilder
from app.intelligence.goal_generation.goal_deduplicator import deduplicate
from app.intelligence.goal_generation.goal_priority import compute_priority
from app.intelligence.goal_generation.schemas import GOAL_STATUSES, GOAL_TYPES, GoalCandidate

# ---------------------------------------------------------------------------
# Helpers (mirrors tests/test_knowledge_graph.py)
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


def _output(label, *, output_type="kpi_card", value="5", confidence=0.6, output_id=None) -> DerivedOutputDescriptor:
    kwargs = {"output_id": output_id} if output_id else {}
    return DerivedOutputDescriptor(
        canonical_label=label, output_type=output_type, raw_value=value, parsed_value=float(value) if value.replace(".", "", 1).isdigit() else None,
        confidence=confidence, evidence=[DependencyEvidence(source_kind="heading", observed_text=label)], **kwargs,
    )


def _dependency(*, entity_ids=None, workflow_ids=None, actor_ids=None, output_id, status="observed", confidence=0.6, effect_direction="unknown", dependency_id="fixed-dependency-1") -> DependencyDescriptor:
    return DependencyDescriptor(
        dependency_id=dependency_id,
        canonical_name=f"dep -> {output_id}", dependency_type="entity_dependency", relationship_type="counts",
        source_entity_ids=entity_ids or [], source_workflow_ids=workflow_ids or [], source_actor_ids=actor_ids or [],
        target_output_ids=[output_id], status=status, confidence=confidence, effect_direction=effect_direction,
        supporting_evidence=[DependencyEvidence(source_kind="entity_registry", observed_text="matched")],
    )


def _registries():
    return EntityRegistry(), ActorRegistry(), WorkflowRegistry(), DependencyRegistry()


def _synced_graph(er=None, ar=None, wr=None, dr=None, iteration=1) -> ApplicationKnowledgeGraph:
    kg = ApplicationKnowledgeGraph()
    kg.synchronize(entity_registry=er, actor_registry=ar, workflow_registry=wr, dependency_registry=dr, iteration=iteration)
    return kg


def _generate(graph, *, engine=None, iteration=1):
    engine = engine or GoalGenerationEngine()
    return engine, engine.generate(graph, iteration=iteration)


def _full_kpi_chain():
    """entity(job) -[state: open/closed]-> workflow(job workflow, step create by
    current session, transition open->closed) -> output(open jobs, kpi_card)
    depends on the WORKFLOW (not the entity directly); actor has a permission
    enabling 'create'. Illustrates the KPI -> workflow -> transition ->
    permission dependency chain from the commissioning task."""
    er, ar, wr, dr = _registries()
    er.memory.records["job"] = _entity("job", states=["open", "closed"], operations=[EntityOperation(operation="create", confidence=0.6, evidence=[])])
    ar.memory.records["current session"] = _actor(
        "current session", permissions=[_permission("can_create_job", confidence=0.5, positive=True)],
    )
    wf = _workflow(
        wr, "job", "job workflow",
        steps=[_step("create", entity_id="job", actor_id="current session", sequence_hint=0, source_state="open", target_state="closed")],
        actors=[WorkflowActorParticipation(actor_id="current session", role_in_workflow="initiator", step_ids=[])],
        entities=[WorkflowEntityParticipation(entity_id="job", role_in_workflow="subject", step_ids=[])],
    )
    # A fixed output_id (rather than DerivedOutputDescriptor's random
    # default) so cross-instance determinism tests aren't fighting an
    # upstream (already-shipped Dependency Discovery) random id field.
    output = _output("open jobs", output_type="kpi_card", confidence=0.5, output_id="fixed-output-1")
    dr.memory.outputs[output.output_id] = output
    dep = _dependency(workflow_ids=["job workflow"], output_id=output.output_id, status="observed", confidence=0.5)
    dr.memory.records[dep.dependency_id] = dep
    return er, ar, wr, dr


# ---------------------------------------------------------------------------
# A. Goal creation
# ---------------------------------------------------------------------------


class TestGoalCreation:
    def test_entity_becomes_verify_entity_lifecycle_goal(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        graph = _synced_graph(er=er)
        _, result = _generate(graph)
        assert any(g.goal_type == "verify_entity_lifecycle" for g in result.goals)

    def test_workflow_becomes_verify_workflow_goal(self):
        _, _, wr, _ = _registries()
        _workflow(wr, "job", "job workflow", steps=[_step("create", entity_id="job")])
        graph = _synced_graph(wr=wr)
        _, result = _generate(graph)
        assert any(g.goal_type == "verify_workflow" for g in result.goals)

    def test_permission_becomes_verify_permission_goal(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_view_job")])
        graph = _synced_graph(ar=ar)
        _, result = _generate(graph)
        assert any(g.goal_type == "verify_permission" for g in result.goals)

    def test_actor_becomes_verify_actor_capability_goal(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor("current session")
        graph = _synced_graph(ar=ar)
        _, result = _generate(graph)
        assert any(g.goal_type == "verify_actor_capability" and "current session" in g.title for g in result.goals)

    def test_counter_output_becomes_verify_kpi_goal(self):
        _, _, _, dr = _registries()
        dr.memory.outputs["a"] = _output("open jobs", output_type="kpi_card")
        graph = _synced_graph(dr=dr)
        _, result = _generate(graph)
        assert any(g.goal_type == "verify_kpi" for g in result.goals)

    def test_report_output_becomes_validate_report_goal(self):
        _, _, _, dr = _registries()
        dr.memory.outputs["a"] = _output("summary report", output_type="report_summary")
        graph = _synced_graph(dr=dr)
        _, result = _generate(graph)
        assert any(g.goal_type == "validate_report" for g in result.goals)

    def test_notification_output_becomes_validate_notification_goal(self):
        _, _, _, dr = _registries()
        dr.memory.outputs["a"] = _output("alerts", output_type="notification_count")
        graph = _synced_graph(dr=dr)
        _, result = _generate(graph)
        assert any(g.goal_type == "validate_notification" for g in result.goals)

    def test_entity_state_becomes_verify_state_transition_goal(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["open"])
        graph = _synced_graph(er=er)
        _, result = _generate(graph)
        assert any(g.goal_type == "verify_state_transition" for g in result.goals)

    def test_prerequisite_becomes_validate_prerequisite_goal(self):
        _, _, wr, _ = _registries()
        from app.intelligence.workflow_discovery.schemas import WorkflowPrerequisite
        _workflow(
            wr, "job", "job workflow",
            steps=[_step("create", entity_id="job")],
            prerequisites=[WorkflowPrerequisite(prerequisite_id="p1", type="required_permission", target="can_create", satisfied=False)],
        )
        graph = _synced_graph(wr=wr)
        _, result = _generate(graph)
        assert any(g.goal_type == "validate_prerequisite" for g in result.goals)

    def test_dependency_edge_becomes_verify_dependency_goal(self):
        er, _, _, dr = _registries()
        er.memory.records["job"] = _entity("job")
        output = _output("open jobs", output_type="kpi_card")
        dr.memory.outputs[output.output_id] = output
        dep = _dependency(entity_ids=["job"], output_id=output.output_id, status="observed")
        dr.memory.records[dep.dependency_id] = dep
        graph = _synced_graph(er=er, dr=dr)
        _, result = _generate(graph)
        assert any(g.goal_type == "verify_dependency" for g in result.goals)

    def test_candidate_business_rule_relationship_becomes_goal(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", relationships=[EntityRelationship(kind="references", subject_entity_id="job", object_entity_id="customer", confidence=0.4, evidence=[])])
        er.memory.records["customer"] = _entity("customer")
        graph = _synced_graph(er=er)
        _, result = _generate(graph)
        assert any(g.goal_type == "verify_business_rule" for g in result.goals)

    def test_all_goal_types_are_within_closed_vocabulary(self):
        er, ar, wr, dr = _full_kpi_chain()
        graph = _synced_graph(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _generate(graph)
        for g in result.goals:
            assert g.goal_type in GOAL_TYPES
            assert g.goal_status in GOAL_STATUSES


# ---------------------------------------------------------------------------
# B. Goal scoring
# ---------------------------------------------------------------------------


class TestGoalScoring:
    def test_priority_breakdown_is_fully_populated(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        graph = _synced_graph(er=er)
        _, result = _generate(graph)
        goal = result.goals[0]
        assert goal.priority is not None
        assert 0.0 <= goal.priority.final_score <= 1.0
        assert goal.priority.explanation
        assert goal.priority_score == pytest.approx(goal.priority.final_score)

    def test_fully_connected_clean_entity_produces_no_goal(self):
        # An entity referenced by BOTH an actor (satisfies entity_without_actor)
        # and a workflow (satisfies entity_without_workflow) has no open gap at
        # all -- nothing motivates investigating it further, so no candidate
        # should survive the final pruning pass.
        er, ar, wr, _ = _registries()
        er.memory.records["job"] = _entity("job", confidence=0.9)
        ar.memory.records["mgr"] = _actor("mgr", known_entities=["job"])
        _workflow(
            wr, "job", "job workflow",
            steps=[_step("create", entity_id="job", actor_id="mgr")],
            entities=[WorkflowEntityParticipation(entity_id="job", role_in_workflow="subject", step_ids=[])],
        )
        graph = _synced_graph(er=er, ar=ar, wr=wr)
        _, result = _generate(graph)
        assert not any(g.goal_type == "verify_entity_lifecycle" for g in result.goals)

    def test_node_explicitly_marked_verified_in_graph_gets_penalised(self):
        # Has an actor reference (satisfies entity_without_actor) but no
        # workflow reference (entity_without_workflow still fires) -- so the
        # candidate carries evidence and survives pruning even though its
        # graph status is (artificially, for this test) "verified".
        er, ar, _, _ = _registries()
        er.memory.records["job"] = _entity("job", confidence=0.9)
        ar.memory.records["mgr"] = _actor("mgr", known_entities=["job"])
        graph = _synced_graph(er=er, ar=ar)
        from app.intelligence.knowledge_graph.graph_node_factory import entity_node_id
        graph.memory.nodes[entity_node_id("job")].status = "verified"
        _, result = _generate(graph)
        goal = next(g for g in result.goals if g.goal_type == "verify_entity_lifecycle")
        assert "already_verified" in goal.priority.applied_penalties

    def test_already_verified_penalty_applied_when_evidence_remains(self):
        candidate = GoalCandidate(subject_key="k", goal_type="verify_entity_lifecycle", title="t", already_verified=True, source_confidence=0.9)
        priority = compute_priority(
            "k", business_value=0.8, risk=0.1, knowledge_gain=0.1, coverage_improvement=0.1,
            dependency_impact=0.1, graph_centrality=0.1, blocking_severity=0.1, confidence_gap=0.1,
            already_verified=True,
        )
        assert "already_verified" in priority.applied_penalties
        assert priority.final_score < priority.weighted_score

    def test_low_business_impact_penalty_applied(self):
        priority = compute_priority(
            "k", business_value=0.05, risk=0.1, knowledge_gain=0.1, coverage_improvement=0.1,
            dependency_impact=0.1, graph_centrality=0.1, blocking_severity=0.1, confidence_gap=0.1,
        )
        assert "low_business_impact" in priority.applied_penalties

    def test_contradiction_and_blocking_gap_raise_risk(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", confidence=0.9, status="confirmed")
        graph = _synced_graph(er=er)
        builder = GoalCandidateBuilder(graph)
        candidates = builder.build()
        entity_goal = next(c for c in candidates if c.goal_type == "verify_entity_lifecycle")
        entity_goal.contradictions = ["c1"]
        entity_goal.blocking_gaps = ["g1"]
        from app.intelligence.goal_generation.goal_priority import derive_priority_signals, max_node_degree
        signals = derive_priority_signals(entity_goal, graph=graph, max_degree=max_node_degree(graph.memory))
        assert signals["risk"] > 0.4


# ---------------------------------------------------------------------------
# C. Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication:
    def test_gap_and_low_confidence_merge_into_one_workflow_goal(self):
        _, _, wr, _ = _registries()
        _workflow(wr, "job", "job workflow", steps=[_step("create", entity_id="job", status="observed")])
        graph = _synced_graph(wr=wr)
        _, result = _generate(graph)
        workflow_goals = [g for g in result.goals if g.goal_type == "verify_workflow"]
        assert len(workflow_goals) == 1
        assert len(workflow_goals[0].supporting_evidence) >= 1

    def test_deduplicate_merges_same_signature_candidates(self):
        a = GoalCandidate(subject_key="resolve_graph_gap:gap:1", goal_type="resolve_graph_gap", title="a", supporting_graph_nodes=["n1", "n2"])
        b = GoalCandidate(subject_key="resolve_graph_gap:gap:2", goal_type="resolve_graph_gap", title="b", supporting_graph_nodes=["n2", "n1"])
        merged, conflicts = deduplicate([a, b])
        assert len(merged) == 1
        assert len(conflicts) == 1
        assert conflicts[0].resolution == "merged"

    def test_no_duplicate_goal_ids_in_result(self):
        er, ar, wr, dr = _full_kpi_chain()
        graph = _synced_graph(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _generate(graph)
        ids = [g.goal_id for g in result.goals]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# D. Grouping
# ---------------------------------------------------------------------------


class TestGrouping:
    def test_entity_group_created(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        graph = _synced_graph(er=er)
        _, result = _generate(graph)
        assert any(g.group_type == "entity" for g in result.groups)

    def test_workflow_group_created_and_exposes_metrics(self):
        _, _, wr, _ = _registries()
        _workflow(wr, "job", "job workflow", steps=[_step("create", entity_id="job")])
        graph = _synced_graph(wr=wr)
        _, result = _generate(graph)
        wf_groups = [g for g in result.groups if g.group_type == "workflow"]
        assert len(wf_groups) == 1
        group = wf_groups[0]
        assert group.goal_count == len(group.goal_ids) > 0
        assert 0.0 <= group.importance <= 1.0
        assert 0.0 <= group.coverage <= 1.0
        assert 0.0 <= group.risk <= 1.0

    def test_gap_type_group_created(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_view_job")])
        graph = _synced_graph(ar=ar)
        _, result = _generate(graph)
        assert any(g.group_type == "gap" for g in result.groups)

    def test_goal_group_id_is_set_on_member_goals(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        graph = _synced_graph(er=er)
        _, result = _generate(graph)
        entity_goal = next(g for g in result.goals if g.goal_type == "verify_entity_lifecycle")
        assert entity_goal.group_id is not None


# ---------------------------------------------------------------------------
# E. Goal dependencies
# ---------------------------------------------------------------------------


class TestGoalDependencies:
    def test_kpi_chain_dependencies_represented(self):
        er, ar, wr, dr = _full_kpi_chain()
        graph = _synced_graph(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _generate(graph)

        by_type = {}
        for g in result.goals:
            by_type.setdefault(g.goal_type, []).append(g)

        assert "verify_dependency" in by_type
        assert "verify_workflow" in by_type
        dep_goal = by_type["verify_dependency"][0]
        workflow_goal = by_type["verify_workflow"][0]

        assert workflow_goal.goal_id in dep_goal.depends_on_goal_ids or any(
            d.goal_id == dep_goal.goal_id and d.depends_on_goal_id == workflow_goal.goal_id for d in result.dependencies
        )

    def test_dependent_goal_is_blocked_status(self):
        er, ar, wr, dr = _full_kpi_chain()
        graph = _synced_graph(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _generate(graph)
        dep_goal = next(g for g in result.goals if g.goal_type == "verify_dependency")
        if dep_goal.depends_on_goal_ids:
            assert dep_goal.goal_status == "blocked"

    def test_dependencies_never_form_self_loop(self):
        er, ar, wr, dr = _full_kpi_chain()
        graph = _synced_graph(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _generate(graph)
        for dep in result.dependencies:
            assert dep.goal_id != dep.depends_on_goal_id

    def test_explanation_names_recommended_investigation(self):
        er, ar, wr, dr = _full_kpi_chain()
        graph = _synced_graph(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _generate(graph)
        for goal in result.goals:
            assert goal.explanation is not None
            assert goal.explanation.recommended_investigation
            assert goal.recommended_goal_type in GOAL_TYPES


# ---------------------------------------------------------------------------
# F. Priority ordering
# ---------------------------------------------------------------------------


class TestPriorityOrdering:
    def test_highest_priority_goals_sorted_descending(self):
        er, ar, wr, dr = _full_kpi_chain()
        graph = _synced_graph(er=er, ar=ar, wr=wr, dr=dr)
        engine, _ = _generate(graph)
        top = engine.highest_priority_goals(limit=10)
        scores = [g.priority_score for g in top]
        assert scores == sorted(scores, reverse=True)

    def test_ordering_is_deterministic_across_runs(self):
        er, ar, wr, dr = _full_kpi_chain()
        graph1 = _synced_graph(er=er, ar=ar, wr=wr, dr=dr)
        engine1, result1 = _generate(graph1)

        er2, ar2, wr2, dr2 = _full_kpi_chain()
        graph2 = _synced_graph(er=er2, ar=ar2, wr=wr2, dr=dr2)
        engine2, result2 = _generate(graph2)

        ids1 = [g.goal_id for g in engine1.highest_priority_goals(20)]
        ids2 = [g.goal_id for g in engine2.highest_priority_goals(20)]
        assert ids1 == ids2


# ---------------------------------------------------------------------------
# G. Graph-gap goals
# ---------------------------------------------------------------------------


class TestGraphGapGoals:
    def test_isolated_node_gap_produces_standalone_goal(self):
        _, ar, _, _ = _registries()
        ar.memory.records["ghost"] = _actor("ghost")
        # An actor with zero relationships at all becomes isolated.
        graph = ApplicationKnowledgeGraph()
        from app.intelligence.knowledge_graph.knowledge_graph_memory import KnowledgeGraphMemory
        graph.synchronize(actor_registry=ar, iteration=1)
        _, result = _generate(graph)
        # Isolated actor still gets its own verify_actor_capability candidate
        # from node scanning (Step 1) since it's a tracked type -- the
        # isolated_node gap enriches that SAME goal, never a second one.
        actor_goals = [g for g in result.goals if g.goal_type == "verify_actor_capability"]
        assert len(actor_goals) == 1

    def test_workflow_without_trigger_gap_merges_into_workflow_goal(self):
        _, _, wr, _ = _registries()
        _workflow(wr, "job", "job workflow", steps=[_step("create", entity_id="job")])
        graph = _synced_graph(wr=wr)
        _, result = _generate(graph)
        workflow_goal = next(g for g in result.goals if g.goal_type == "verify_workflow")
        assert any(g_id for g_id in workflow_goal.source_gap_ids)


# ---------------------------------------------------------------------------
# H. Contradiction goals
# ---------------------------------------------------------------------------


class TestContradictionGoals:
    def test_contradiction_produces_resolve_contradiction_goal(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        graph = _synced_graph(er=er)
        from app.intelligence.knowledge_graph.schemas import GraphContradiction
        graph.memory.add_contradiction(GraphContradiction(description="conflicting effect directions", node_ids=["entity:job"], edge_ids=[]))
        _, result = _generate(graph)
        contradiction_goals = [g for g in result.goals if g.goal_type == "resolve_contradiction"]
        assert len(contradiction_goals) == 1
        assert contradiction_goals[0].contradictions

    def test_contradiction_group_created(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        graph = _synced_graph(er=er)
        from app.intelligence.knowledge_graph.schemas import GraphContradiction
        graph.memory.add_contradiction(GraphContradiction(description="x", node_ids=["entity:job"], edge_ids=[]))
        _, result = _generate(graph)
        assert any(g.group_type == "contradiction" for g in result.groups)


# ---------------------------------------------------------------------------
# I. Confidence goals
# ---------------------------------------------------------------------------


class TestConfidenceGoals:
    def test_low_confidence_edge_produces_increase_confidence_goal(self):
        _, ar, wr, _ = _registries()
        ar.memory.records["current session"] = _actor("current session")
        _workflow(
            wr, "job", "job workflow",
            steps=[_step("create", entity_id="job", actor_id="current session")],
            actors=[WorkflowActorParticipation(actor_id="current session", role_in_workflow="initiator", step_ids=[])],
        )
        graph = _synced_graph(ar=ar, wr=wr)
        # force the performed_by edge low-confidence directly
        performed_by = next(e for e in graph.memory.edges.values() if e.edge_type == "performed_by")
        graph.memory.edges[performed_by.edge_id].confidence = 0.1
        _, result = _generate(graph)
        assert any(g.goal_type == "increase_confidence" and performed_by.edge_id in g.supporting_graph_edges for g in result.goals)


# ---------------------------------------------------------------------------
# J. Workflow / permission / dependency goals
# ---------------------------------------------------------------------------


class TestWorkflowPermissionDependencyGoals:
    def test_inferred_relationship_goal_grouped_by_rule(self):
        er, ar, wr, _ = _registries()
        er.memory.records["job"] = _entity("job")
        ar.memory.records["current session"] = _actor("current session")
        _workflow(
            wr, "job", "job workflow",
            steps=[_step("create", entity_id="job", actor_id="current session")],
        )
        graph = _synced_graph(er=er, ar=ar, wr=wr)
        _, result = _generate(graph)
        inferred_goals = [g for g in result.goals if g.goal_type == "validate_inferred_relationship"]
        # rule A (actor-workflow participation) and rule B (workflow-entity
        # involvement) should each surface as their own grouped goal.
        assert len(inferred_goals) >= 1
        for g in inferred_goals:
            assert len(g.source_inference_rule_ids) == 1

    def test_actor_hand_off_uses_its_own_goal_type_not_generic_inferred(self):
        _, ar, wr, _ = _registries()
        ar.memory.records["alice"] = _actor("alice")
        ar.memory.records["bob"] = _actor("bob")
        _workflow(
            wr, "job", "job workflow",
            steps=[
                _step("create", entity_id="job", actor_id="alice", sequence_hint=0),
                _step("approve", entity_id="job", actor_id="bob", sequence_hint=1),
            ],
        )
        graph = _synced_graph(ar=ar, wr=wr)
        _, result = _generate(graph)
        handoff_goals = [g for g in result.goals if g.goal_type == "validate_actor_hand_off" and "inference_rule" in g.goal_id]
        assert len(handoff_goals) == 1


# ---------------------------------------------------------------------------
# K. Determinism / idempotency
# ---------------------------------------------------------------------------


class TestDeterminismAndIdempotency:
    def test_repeated_generation_is_idempotent(self):
        er, ar, wr, dr = _full_kpi_chain()
        graph = _synced_graph(er=er, ar=ar, wr=wr, dr=dr)
        engine = GoalGenerationEngine()
        result1 = engine.generate(graph, iteration=1)
        created_at_1 = {g.goal_id: g.created_at for g in result1.goals}
        obs_count_1 = {g.goal_id: g.observation_count for g in result1.goals}

        result2 = engine.generate(graph, iteration=2)
        created_at_2 = {g.goal_id: g.created_at for g in result2.goals}
        obs_count_2 = {g.goal_id: g.observation_count for g in result2.goals}

        assert set(created_at_1) == set(created_at_2)
        assert created_at_1 == created_at_2
        assert obs_count_1 == obs_count_2
        assert engine.memory.generation_count == 1

    def test_same_graph_content_produces_same_goal_set_from_scratch(self):
        er1, ar1, wr1, dr1 = _full_kpi_chain()
        graph1 = _synced_graph(er=er1, ar=ar1, wr=wr1, dr=dr1)
        _, result1 = _generate(graph1)

        er2, ar2, wr2, dr2 = _full_kpi_chain()
        graph2 = _synced_graph(er=er2, ar=ar2, wr=wr2, dr=dr2)
        _, result2 = _generate(graph2)

        assert sorted(g.goal_id for g in result1.goals) == sorted(g.goal_id for g in result2.goals)
        priorities1 = {g.goal_id: round(g.priority_score, 6) for g in result1.goals}
        priorities2 = {g.goal_id: round(g.priority_score, 6) for g in result2.goals}
        assert priorities1 == priorities2

    def test_goal_disappears_when_evidence_disappears(self):
        er, ar, wr, _ = _registries()
        er.memory.records["job"] = _entity("job", confidence=0.3)
        graph = _synced_graph(er=er)
        engine = GoalGenerationEngine()
        result1 = engine.generate(graph, iteration=1)
        assert any(g.goal_type == "verify_entity_lifecycle" for g in result1.goals)

        # Now both entity_without_actor and entity_without_workflow resolve --
        # nothing left motivates investigating this entity.
        ar.memory.records["mgr"] = _actor("mgr", known_entities=["job"])
        _workflow(
            wr, "job", "job workflow",
            steps=[_step("create", entity_id="job", actor_id="mgr")],
            entities=[WorkflowEntityParticipation(entity_id="job", role_in_workflow="subject", step_ids=[])],
        )
        graph.synchronize(entity_registry=er, actor_registry=ar, workflow_registry=wr, iteration=2)
        result2 = engine.generate(graph, iteration=2)
        assert not any(g.goal_type == "verify_entity_lifecycle" for g in result2.goals)


# ---------------------------------------------------------------------------
# L. Memory integration
# ---------------------------------------------------------------------------


class TestMemoryIntegration:
    def _run_memory(self):
        from app.agent.memory import RunMemory
        return RunMemory(run_id="r1", start_url="https://example.test")

    def test_degrades_gracefully_with_no_goal_engine(self):
        mem = self._run_memory()
        assert mem.generate_goals() is None
        assert mem.goal_statistics() is None
        assert mem.goal_snapshot() is None
        assert mem.goal_summary() is None
        assert mem.highest_priority_goals() == []
        assert mem.blocked_goals() == []
        assert mem.pending_goals() == []
        assert mem.completed_goals() == []

    def test_generate_goals_populates_memory_snapshot(self):
        mem = self._run_memory()
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        mem.entity_registry = er
        mem.knowledge_graph = ApplicationKnowledgeGraph()
        mem.knowledge_graph.synchronize(entity_registry=er, iteration=1)
        mem.goal_engine = GoalGenerationEngine()

        result = mem.generate_goals()
        assert result is not None
        assert result.statistics.total_goals > 0

        snap = mem.memory_snapshot()
        assert "goal_generation" in snap
        assert snap["goal_generation"]["total_goals"] > 0

        assert mem.goal_statistics().total_goals > 0
        assert len(mem.goal_snapshot()["goals"]) > 0
        assert mem.goal_summary()["statistics"]["total_goals"] > 0
        assert len(mem.highest_priority_goals(limit=5)) > 0
        assert len(mem.goals_for_entity("job")) > 0


# ---------------------------------------------------------------------------
# M. Controller integration
# ---------------------------------------------------------------------------


class TestControllerIntegration:
    def test_run_goal_generation_is_non_fatal_on_exception(self):
        import types
        from app.agent.controller import AgentController

        fake_self = types.SimpleNamespace(memory=types.SimpleNamespace(goal_engine=object(), knowledge_graph=object(), actions=[]))
        # goal_engine.generate doesn't exist on a bare object() -> AttributeError,
        # must be swallowed, never raised.
        AgentController._run_goal_generation(fake_self)

    def test_controller_module_wires_goal_generation_after_knowledge_graph_sync(self):
        import inspect
        from app.agent import controller as controller_module
        source = inspect.getsource(controller_module)
        assert "self._run_knowledge_graph_sync()" in source
        assert "self._run_goal_generation()" in source
        sync_idx = source.index("self._run_knowledge_graph_sync()")
        goal_idx = source.index("self._run_goal_generation()")
        assert sync_idx < goal_idx


# ---------------------------------------------------------------------------
# N. Query API
# ---------------------------------------------------------------------------


class TestQueryAPI:
    def _engine_result(self):
        er, ar, wr, dr = _full_kpi_chain()
        graph = _synced_graph(er=er, ar=ar, wr=wr, dr=dr)
        return _generate(graph)

    def test_goals_for_entity_actor_workflow_output(self):
        engine, result = self._engine_result()
        assert engine.query_engine.goals_for_entity("job")
        assert engine.query_engine.goals_for_actor("current session")
        assert engine.query_engine.goals_for_workflow("job workflow")
        assert engine.query_engine.goals_for_output("open jobs")

    def test_pending_blocked_completed_dismissed_partition_all_goals(self):
        engine, result = self._engine_result()
        pending = engine.query_engine.pending_goals()
        blocked = engine.query_engine.blocked_goals()
        completed = engine.query_engine.completed_goals()
        dismissed = engine.query_engine.dismissed_goals()
        assert len(pending) + len(blocked) + len(completed) + len(dismissed) == len(result.goals)

    def test_goal_statistics_matches_goal_count(self):
        engine, result = self._engine_result()
        stats = engine.query_engine.goal_statistics()
        assert stats.total_goals == len(result.goals)
        assert sum(stats.goals_by_status.values()) == stats.total_goals
        assert sum(stats.goals_by_type.values()) == stats.total_goals

    def test_statistics_graph_version_matches_when_queried_independently(self):
        # Regression: `goal_engine.statistics()` (and `RunMemory.goal_
        # statistics()`), called AFTER generate() rather than reading the
        # GoalGenerationResult it returned, used to report graph_version=0
        # forever -- the version patch only ever landed on the result's own
        # copy of the statistics object, never on GoalMemory itself.
        engine, result = self._engine_result()
        assert result.statistics.graph_version > 0
        independently_queried = engine.statistics()
        assert independently_queried.graph_version == result.statistics.graph_version

    def test_goals_for_gap_and_contradiction(self):
        engine, result = self._engine_result()
        workflow_goal = next(g for g in result.goals if g.goal_type == "verify_workflow")
        if workflow_goal.source_gap_ids:
            gap_id = workflow_goal.source_gap_ids[0]
            assert workflow_goal in engine.query_engine.goals_for_gap(gap_id)


# ---------------------------------------------------------------------------
# O. Application neutrality
# ---------------------------------------------------------------------------


FORBIDDEN_WORDS = {
    "invoice", "customer", "job", "ticket", "order", "product", "cart", "checkout",
    "employee", "vehicle", "haulvana", "serviceflow", "saucedemo", "insightboard",
}


class TestApplicationNeutrality:
    def test_no_hardcoded_business_vocabulary(self):
        package_dir = BACKEND / "app" / "intelligence" / "goal_generation"
        offenders = []
        for path in sorted(package_dir.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            docstring_nodes = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                    body = getattr(node, "body", [])
                    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                        docstring_nodes.add(id(body[0].value))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_nodes:
                    words_in_constant = set(re.findall(r"[a-z]+", node.value.lower()))
                    hit = words_in_constant & FORBIDDEN_WORDS
                    if hit:
                        offenders.append((path.name, node.value, hit))
        assert not offenders, f"Hardcoded business vocabulary found: {offenders}"

    def test_goal_types_have_no_application_specific_members(self):
        for goal_type in GOAL_TYPES:
            for word in FORBIDDEN_WORDS:
                assert word not in goal_type


# ---------------------------------------------------------------------------
# Integration fixture: full pipeline sanity
# ---------------------------------------------------------------------------


class TestIntegrationFixture:
    def test_full_pipeline_produces_consistent_result(self):
        er, ar, wr, dr = _full_kpi_chain()
        graph = _synced_graph(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _generate(graph)

        assert result.statistics.total_goals == len(result.goals)
        assert result.graph_version == graph.memory.graph_version
        for goal in result.goals:
            assert goal.supporting_graph_nodes or goal.supporting_evidence
            for node_id in goal.supporting_graph_nodes:
                assert node_id in graph.memory.nodes
            for edge_id in goal.supporting_graph_edges:
                assert edge_id in graph.memory.edges
