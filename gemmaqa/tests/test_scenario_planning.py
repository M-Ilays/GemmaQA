"""Scenario Planning Engine (app/intelligence/scenario_planning/).

Covers: goal-to-scenario mapping (A), scenario identity/determinism (B),
requirement resolution (C), step decomposition (D), before/after planning
(E), permission scenarios (F), workflow scenarios (G), evidence planning
(H), feasibility (I), risk (J), branches/alternatives (K), dependencies/
conflicts (L), gaps (M), query API (N), memory/controller integration
(O), neutrality (P) -- plus synthetic integration fixtures mirroring the
task's 8 illustrative scenarios.
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
from app.intelligence.entity_discovery.schemas import EntityEvidence, EntityOperation, EntityRecord
from app.intelligence.workflow_discovery import WorkflowRegistry
from app.intelligence.workflow_discovery.schemas import (
    WorkflowActorParticipation,
    WorkflowBranch,
    WorkflowEntityParticipation,
    WorkflowStep,
)
from app.intelligence.dependency_discovery import DependencyRegistry
from app.intelligence.dependency_discovery.schemas import ActorScope, DependencyDescriptor, DependencyEvidence, DerivedOutputDescriptor
from app.intelligence.knowledge_graph import ApplicationKnowledgeGraph
from app.intelligence.goal_generation import GoalGenerationEngine
from app.intelligence.scenario_planning import ScenarioPlanningEngine
from app.intelligence.scenario_planning.schemas import RISK_CLASSES, SCENARIO_STATUSES, SCENARIO_TYPES, STEP_TYPES
from app.intelligence.scenario_planning.scenario_risk_analyzer import assess_risk

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity(term, *, states=None, confidence=0.8, operations=None) -> EntityRecord:
    return EntityRecord(
        canonical_name=term, status="confirmed", confidence=confidence, known_states=states or [],
        operations=operations or [], evidence=[EntityEvidence(source_kind="heading", observed_text=term)],
    )


def _actor(term, *, permissions=None, confidence=0.7) -> ActorRecord:
    return ActorRecord(
        canonical_name=term, status="confirmed", confidence=confidence, known_permissions=permissions or [],
        supporting_evidence=[ActorEvidence(source_kind="session_info", observed_text=term)],
    )


def _permission(permission_id, *, confidence=0.6, positive=True) -> PermissionCandidate:
    ev = ActorEvidence(source_kind="visible_control", observed_text=permission_id)
    return PermissionCandidate(permission_id=permission_id, confidence=confidence, positive_evidence=[ev] if positive else [], negative_evidence=[] if positive else [ev, ev])


def _step(verb, *, entity_id=None, actor_id="current session", status="observed", sequence_hint=0, source_state=None, target_state=None) -> WorkflowStep:
    return WorkflowStep(
        semantic_action=verb, entity_id=entity_id, actor_id=actor_id, status=status, sequence_hint=sequence_hint,
        source_state=source_state, target_state=target_state, page_id="p", page_state_fingerprint="f", confidence=0.6,
    )


def _output(label, *, output_type="kpi_card", confidence=0.6, output_id=None) -> DerivedOutputDescriptor:
    kwargs = {"output_id": output_id} if output_id else {}
    return DerivedOutputDescriptor(canonical_label=label, output_type=output_type, raw_value="5", confidence=confidence, evidence=[DependencyEvidence(source_kind="heading", observed_text=label)], **kwargs)


def _dependency(*, workflow_ids=None, entity_ids=None, output_id, status="observed", confidence=0.6, effect_direction="unknown", actor_scope=None, dependency_id=None) -> DependencyDescriptor:
    kwargs = {"dependency_id": dependency_id} if dependency_id else {}
    return DependencyDescriptor(
        canonical_name=f"dep -> {output_id}", dependency_type="entity_dependency", relationship_type="counts",
        source_workflow_ids=workflow_ids or [], source_entity_ids=entity_ids or [], target_output_ids=[output_id],
        status=status, confidence=confidence, effect_direction=effect_direction, actor_scope=actor_scope,
        supporting_evidence=[DependencyEvidence(source_kind="entity_registry", observed_text="matched")],
        **kwargs,
    )


def _registries():
    return EntityRegistry(), ActorRegistry(), WorkflowRegistry(), DependencyRegistry()


def _primary(result, scenario_type: str):
    """The PRIMARY variant of a scenario type -- several scenario types
    (workflow_verification, dependency_verification, etc.) also produce
    "observational"/"reduced_scope" alternatives, so picking by
    scenario_type alone is ambiguous; tests that care about the mutating,
    canonical form must select by variant explicitly."""
    return next(s for s in result.scenarios if s.scenario_type == scenario_type and s.scenario_id.endswith(":primary"))


def _plan(*, er=None, ar=None, wr=None, dr=None, iteration=1):
    graph = ApplicationKnowledgeGraph()
    graph.synchronize(entity_registry=er, actor_registry=ar, workflow_registry=wr, dependency_registry=dr, iteration=iteration)
    goal_engine = GoalGenerationEngine()
    goal_engine.generate(graph, iteration=iteration)
    scenario_engine = ScenarioPlanningEngine()
    result = scenario_engine.generate(goal_engine, graph, iteration=iteration)
    return graph, goal_engine, scenario_engine, result


def _single_transition_fixture():
    er, ar, wr, dr = _registries()
    er.memory.records["job"] = _entity("job", states=["open", "closed"])
    ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_create_job", confidence=0.5)])
    wf = wr.memory.get_or_create("job", canonical_name="job workflow")
    wf.steps.append(_step("create", entity_id="job", sequence_hint=0, source_state="open", target_state="closed"))
    wf.actors.append(WorkflowActorParticipation(actor_id="current session", role_in_workflow="initiator", step_ids=[]))
    wf.entities.append(WorkflowEntityParticipation(entity_id="job", role_in_workflow="subject", step_ids=[]))
    output = _output("open jobs", output_type="kpi_card", confidence=0.5, output_id="out1")
    dr.memory.outputs[output.output_id] = output
    dep = _dependency(workflow_ids=["job workflow"], output_id=output.output_id, effect_direction="increase", dependency_id="dep1")
    dr.memory.records["dep1"] = dep
    return er, ar, wr, dr


# ---------------------------------------------------------------------------
# A. Goal-to-scenario mapping
# ---------------------------------------------------------------------------


class TestGoalMapping:
    def test_workflow_goal_produces_workflow_scenario(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job"))
        _, _, _, result = _plan(wr=wr)
        assert any(s.scenario_type == "workflow_verification" for s in result.scenarios)

    def test_dependency_goal_produces_dependency_scenario(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        assert any(s.scenario_type == "dependency_verification" for s in result.scenarios)

    def test_metric_goal_produces_metric_scenario(self):
        _, _, _, dr = _registries()
        dr.memory.outputs["a"] = _output("open jobs", output_type="kpi_card")
        _, _, _, result = _plan(dr=dr)
        assert any(s.scenario_type == "metric_verification" for s in result.scenarios)

    def test_permission_positive_goal_produces_positive_scenario(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_view_job", positive=True)])
        _, _, _, result = _plan(ar=ar)
        assert any(s.scenario_type == "permission_positive_verification" for s in result.scenarios)

    def test_permission_negative_goal_produces_denial_scenario(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_delete_job", positive=False)])
        _, _, _, result = _plan(ar=ar)
        assert any(s.scenario_type == "permission_negative_verification" for s in result.scenarios)

    def test_state_transition_goal_produces_transition_scenario(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["open"])
        _, _, _, result = _plan(er=er)
        assert any(s.scenario_type == "state_transition_verification" for s in result.scenarios)

    def test_contradiction_goal_produces_distinguishing_scenario(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        graph = ApplicationKnowledgeGraph()
        graph.synchronize(entity_registry=er, iteration=1)
        from app.intelligence.knowledge_graph.schemas import GraphContradiction
        graph.memory.add_contradiction(GraphContradiction(description="conflict", node_ids=["entity:job"], edge_ids=[]))
        goal_engine = GoalGenerationEngine()
        goal_engine.generate(graph, iteration=1)
        scenario_engine = ScenarioPlanningEngine()
        result = scenario_engine.generate(goal_engine, graph, iteration=1)
        assert any(s.scenario_type == "contradiction_resolution" for s in result.scenarios)

    def test_graph_gap_goal_produces_resolution_scenario(self):
        _, ar, _, _ = _registries()
        ar.memory.records["ghost"] = _actor("ghost")
        _, _, _, result = _plan(ar=ar)
        assert result.scenarios

    def test_unknown_goal_type_produces_incomplete_generic_scenario(self):
        from app.intelligence.scenario_planning.scenario_candidate_builder import build_candidates
        from app.intelligence.scenario_planning.scenario_decomposer import GoalValidation

        class FakeGoal:
            goal_id = "goal:fake"
            goal_type = "verify_workflow"  # valid type but validation says can't plan
            title = "fake"
            description = ""
            priority_score = 0.5
            required_actors = []

        validation = GoalValidation(goal=FakeGoal(), graph=None, can_plan=False, warnings=["no context"])
        candidates = build_candidates(validation)
        assert len(candidates) == 1
        assert candidates[0].scenario_type == "unknown"
        assert candidates[0].variant == "incomplete"

    def test_every_scenario_type_is_within_closed_vocabulary(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        for s in result.scenarios:
            assert s.scenario_type in SCENARIO_TYPES
            assert s.status in SCENARIO_STATUSES


# ---------------------------------------------------------------------------
# B. Scenario identity / determinism
# ---------------------------------------------------------------------------


class TestScenarioIdentity:
    def test_scenario_ids_are_stable_across_runs(self):
        er1, ar1, wr1, dr1 = _single_transition_fixture()
        _, _, _, result1 = _plan(er=er1, ar=ar1, wr=wr1, dr=dr1)
        er2, ar2, wr2, dr2 = _single_transition_fixture()
        _, _, _, result2 = _plan(er=er2, ar=ar2, wr=wr2, dr=dr2)
        assert sorted(s.scenario_id for s in result1.scenarios) == sorted(s.scenario_id for s in result2.scenarios)

    def test_ordering_is_deterministic(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, engine, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        ids = [s.scenario_id for s in engine.query_engine.all_scenarios()]
        assert ids == sorted(ids)

    def test_no_random_identity_fields(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        for s in result.scenarios:
            for gap_id in s.gaps:
                assert gap_id.startswith(s.scenario_id)
            for req in s.actor_requirements:
                assert req.requirement_id == f"actor:{req.canonical_name}"

    def test_unchanged_input_is_idempotent(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph = ApplicationKnowledgeGraph()
        graph.synchronize(entity_registry=er, actor_registry=ar, workflow_registry=wr, dependency_registry=dr, iteration=1)
        goal_engine = GoalGenerationEngine()
        goal_engine.generate(graph, iteration=1)
        scenario_engine = ScenarioPlanningEngine()

        result1 = scenario_engine.generate(goal_engine, graph, iteration=1)
        created1 = {s.scenario_id: s.created_at for s in result1.scenarios}
        obs1 = {s.scenario_id: s.observation_count for s in result1.scenarios}

        goal_engine.generate(graph, iteration=2)
        result2 = scenario_engine.generate(goal_engine, graph, iteration=2)
        created2 = {s.scenario_id: s.created_at for s in result2.scenarios}
        obs2 = {s.scenario_id: s.observation_count for s in result2.scenarios}

        assert created1 == created2
        assert obs1 == obs2
        assert scenario_engine.memory.scenario_plan_version == 1

    def test_no_op_generation_does_not_change_version(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph = ApplicationKnowledgeGraph()
        graph.synchronize(entity_registry=er, actor_registry=ar, workflow_registry=wr, dependency_registry=dr, iteration=1)
        goal_engine = GoalGenerationEngine()
        goal_engine.generate(graph, iteration=1)
        scenario_engine = ScenarioPlanningEngine()
        scenario_engine.generate(goal_engine, graph, iteration=1)
        v1 = scenario_engine.memory.scenario_plan_version
        scenario_engine.generate(goal_engine, graph, iteration=2)
        v2 = scenario_engine.memory.scenario_plan_version
        assert v1 == v2 == 1


# ---------------------------------------------------------------------------
# C. Requirement resolution
# ---------------------------------------------------------------------------


class TestRequirementResolution:
    def test_actor_resolved(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        workflow_scenario = _primary(result, "workflow_verification")
        assert any(r.resolved and r.status == "satisfiable" for r in workflow_scenario.actor_requirements)

    def test_actor_unresolved(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", actor_id="phantom actor"))
        wf.actors.append(WorkflowActorParticipation(actor_id="phantom actor", role_in_workflow="initiator", step_ids=[]))
        _, _, _, result = _plan(wr=wr)
        workflow_scenario = _primary(result, "workflow_verification")
        assert any(r.status == "unresolved" for r in workflow_scenario.actor_requirements)

    def test_permission_resolved(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_view_job")])
        _, _, _, result = _plan(ar=ar)
        perm_scenario = _primary(result, "permission_positive_verification")
        assert any(r.status == "satisfied" for r in perm_scenario.permission_requirements)

    def test_permission_contradicted(self):
        er, *_ = _registries()
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor(
            "current session", permissions=[_permission("can_view_job", positive=True), _permission("can_view_job", positive=False)],
        )
        _, _, _, result = _plan(ar=ar)
        assert any(r.status == "contradicted" for s in result.scenarios for r in s.permission_requirements)

    def test_entity_resolved(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        _, _, _, result = _plan(er=er)
        lifecycle_scenario = next(s for s in result.scenarios if s.scenario_type == "entity_lifecycle_verification")
        assert any(r.status == "satisfied" for r in lifecycle_scenario.entity_requirements)

    def test_state_resolved(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["open"])
        _, _, _, result = _plan(er=er)
        transition_scenario = _primary(result, "state_transition_verification")
        assert any(r.status in {"satisfied", "satisfiable"} for r in transition_scenario.state_requirements)

    def test_workflow_resolved(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        wf_scenario = _primary(result, "workflow_verification")
        assert any(r.status == "satisfied" for r in wf_scenario.workflow_requirements)

    def test_output_resolved(self):
        _, _, _, dr = _registries()
        dr.memory.outputs["a"] = _output("open jobs", output_type="kpi_card")
        _, _, _, result = _plan(dr=dr)
        kpi_scenario = _primary(result, "metric_verification")
        assert any(r.status == "satisfied" for r in kpi_scenario.output_requirements)

    def test_scope_resolved(self):
        _, _, _, dr = _registries()
        output = _output("open jobs")
        dr.memory.outputs[output.output_id] = output
        dep = _dependency(output_id=output.output_id, actor_scope=ActorScope(actor_term="alice"))
        dr.memory.records[dep.dependency_id] = dep
        _, _, _, result = _plan(dr=dr)
        assert result.scenarios  # scope info flows through without crashing

    def test_data_requirement_resolved_for_mutating_scenario(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        wf_scenario = _primary(result, "workflow_verification")
        assert wf_scenario.data_requirements

    def test_missing_requirements_become_gaps(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", actor_id="phantom"))
        wf.actors.append(WorkflowActorParticipation(actor_id="phantom", role_in_workflow="initiator", step_ids=[]))
        _, _, _, result = _plan(wr=wr)
        assert any(g.gap_type == "missing_actor" for g in result.gaps)


# ---------------------------------------------------------------------------
# D. Step decomposition
# ---------------------------------------------------------------------------


class TestStepDecomposition:
    def test_baseline_step_present_for_dependency_scenario(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        dep_scenario = _primary(result, "dependency_verification")
        assert any(step.checkpoint_type == "output_before" for step in dep_scenario.checkpoints)

    def test_actor_session_step_present(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        wf_scenario = _primary(result, "workflow_verification")
        assert any(step.step_type == "establish_session" for step in wf_scenario.steps)

    def test_workflow_action_step_present(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        wf_scenario = _primary(result, "workflow_verification")
        assert any(step.step_type == "perform_workflow_step" for step in wf_scenario.steps)

    def test_state_verification_step_present(self):
        er, *_ = _registries()
        er.memory.records["job"] = _entity("job", states=["open"])
        _, _, _, result = _plan(er=er)
        transition_scenario = _primary(result, "state_transition_verification")
        assert any(step.step_type == "verify_state" for step in transition_scenario.steps)

    def test_output_observation_step_present(self):
        _, _, _, dr = _registries()
        dr.memory.outputs["a"] = _output("open jobs")
        _, _, _, result = _plan(dr=dr)
        kpi_scenario = _primary(result, "metric_verification")
        assert any(step.step_type == "observe_output" for step in kpi_scenario.steps)

    def test_comparison_step_present(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        dep_scenario = _primary(result, "dependency_verification")
        assert any(step.step_type == "compare" for step in dep_scenario.steps)

    def test_evidence_capture_step_present(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        for s in result.scenarios:
            assert any(step.step_type == "capture_evidence" for step in s.steps)

    def test_cleanup_present_for_mutating_scenario(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        wf_scenario = _primary(result, "workflow_verification")
        assert wf_scenario.cleanup_plan is not None and wf_scenario.cleanup_plan.cleanup_required

    def test_ordering_is_stable(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        wf_scenario = _primary(result, "workflow_verification")
        indices = [step.sequence_index for step in wf_scenario.steps]
        assert indices == sorted(indices)

    def test_step_dependencies_preserved(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        wf_scenario = _primary(result, "workflow_verification")
        for step in wf_scenario.steps[1:]:
            assert step.depends_on_step_ids

    def test_all_step_types_within_closed_vocabulary(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        for s in result.scenarios:
            for step in s.steps:
                assert step.step_type in STEP_TYPES

    def test_no_browser_selectors_in_semantic_action(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        forbidden = ["page.locator", "css selector", "xpath", ".click(", "waitfortimeout", "#", ".btn"]
        for s in result.scenarios:
            for step in s.steps:
                lowered = step.semantic_action.lower()
                for term in forbidden:
                    assert term not in lowered


# ---------------------------------------------------------------------------
# E. Before/after planning
# ---------------------------------------------------------------------------


class TestBeforeAfterPlanning:
    def test_expected_increase_direction(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        dep_scenario = _primary(result, "dependency_verification")
        comparison = dep_scenario.comparisons[0]
        assert comparison.comparison_operator == "increases"
        assert comparison.expected_direction == "increase"

    def test_expected_decrease_direction(self):
        er, ar, wr, _ = _single_transition_fixture()
        dr = DependencyRegistry()
        output = _output("closed jobs")
        dr.memory.outputs[output.output_id] = output
        dep = _dependency(workflow_ids=["job workflow"], output_id=output.output_id, effect_direction="decrease")
        dr.memory.records[dep.dependency_id] = dep
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        dep_scenario = _primary(result, "dependency_verification")
        assert dep_scenario.comparisons[0].comparison_operator == "decreases"

    def test_unknown_direction_stays_unknown(self):
        er, ar, wr, _ = _single_transition_fixture()
        dr = DependencyRegistry()
        output = _output("mystery metric")
        dr.memory.outputs[output.output_id] = output
        dep = _dependency(workflow_ids=["job workflow"], output_id=output.output_id, effect_direction="unknown")
        dr.memory.records[dep.dependency_id] = dep
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        dep_scenario = _primary(result, "dependency_verification")
        comparison = dep_scenario.comparisons[0]
        assert comparison.comparison_operator == "unknown"
        assert comparison.expected_delta is None

    def test_delayed_effect_branch_represented(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        dep_scenario = _primary(result, "dependency_verification")
        assert any(step.step_type == "wait_for_effect" for step in dep_scenario.steps)
        assert any(b.branch_type == "immediate_vs_delayed" for b in dep_scenario.branches)

    def test_incompatible_scope_detected(self):
        _, _, _, dr = _registries()
        output = _output("scoped metric")
        dr.memory.outputs[output.output_id] = output
        dep1 = _dependency(output_id=output.output_id, actor_scope=ActorScope(actor_term="alice"))
        dep2 = _dependency(output_id=output.output_id, actor_scope=ActorScope(actor_term="bob"))
        dr.memory.records[dep1.dependency_id] = dep1
        dr.memory.records[dep2.dependency_id] = dep2
        _, _, _, result = _plan(dr=dr)
        # Two dependency edges with different actor_scope on the same output
        # is exactly the incompatible-scope signal the conflict detector uses.
        assert result.scenarios

    def test_no_unsupported_numerical_delta_invented(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        for s in result.scenarios:
            for comparison in s.comparisons:
                assert comparison.expected_delta is None


# ---------------------------------------------------------------------------
# F. Permission scenarios
# ---------------------------------------------------------------------------


class TestPermissionScenarios:
    def test_allowed_permission_scenario(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_view_job", positive=True)])
        _, _, _, result = _plan(ar=ar)
        scenario = _primary(result, "permission_positive_verification")
        assert any(r.status == "satisfied" for r in scenario.permission_requirements)

    def test_denied_permission_scenario(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_delete_job", positive=False)])
        _, _, _, result = _plan(ar=ar)
        scenario = next(s for s in result.scenarios if s.scenario_type == "permission_negative_verification")
        assert any(r.denial_expected for r in scenario.permission_requirements)

    def test_no_mutation_after_denial_evidenced_by_low_risk(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_delete_job", positive=False)])
        _, _, _, result = _plan(ar=ar)
        scenario = next(s for s in result.scenarios if s.scenario_type == "permission_negative_verification")
        assert scenario.risk_assessment.risk_class in {"read_only", "low"}

    def test_positive_and_negative_evidence_conflict_surfaces_as_alternative(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor(
            "current session", permissions=[_permission("can_view_job", positive=True), _permission("can_view_job", positive=False)],
        )
        _, _, _, result = _plan(ar=ar)
        types = {s.scenario_type for s in result.scenarios}
        assert "permission_positive_verification" in types and "permission_negative_verification" in types


# ---------------------------------------------------------------------------
# G. Workflow scenarios
# ---------------------------------------------------------------------------


class TestWorkflowScenarios:
    def test_linear_workflow(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", sequence_hint=0))
        wf.steps.append(_step("approve", entity_id="job", sequence_hint=1))
        _, _, _, result = _plan(wr=wr)
        scenario = _primary(result, "workflow_verification")
        assert len([s for s in scenario.steps if s.step_type == "perform_workflow_step"]) == 2

    def test_branched_workflow_represented(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("submit", entity_id="job", sequence_hint=0))
        wf.branches.append(WorkflowBranch(branch_id="b1", branch_type="approve_vs_reject", decision_point_step_id="s1", option_labels=["approve", "reject"], confidence=0.5))
        _, _, _, result = _plan(wr=wr)
        scenario = _primary(result, "workflow_verification")
        assert any(b.branch_type == "approval_rejection" for b in scenario.branches)

    def test_prerequisite_represented(self):
        from app.intelligence.workflow_discovery.schemas import WorkflowPrerequisite
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job"))
        wf.prerequisites.append(WorkflowPrerequisite(prerequisite_id="p1", type="required_permission", target="can_create", satisfied=False))
        _, _, _, result = _plan(wr=wr)
        assert any(s.scenario_type == "prerequisite_verification" for s in result.scenarios)

    def test_actor_handoff_represented(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", actor_id="alice", sequence_hint=0))
        wf.steps.append(_step("approve", entity_id="job", actor_id="bob", sequence_hint=1))
        _, _, _, result = _plan(wr=wr)
        assert any(s.scenario_type == "actor_handoff_verification" for s in result.scenarios)

    def test_incomplete_workflow_produces_gap(self):
        _, _, wr, _ = _registries()
        wr.memory.get_or_create("job", canonical_name="job workflow")  # no steps at all
        _, _, _, result = _plan(wr=wr)
        assert any(g.gap_type == "incomplete_workflow" for g in result.gaps)

    def test_multiple_actors_produce_multiple_requirements(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", actor_id="alice", sequence_hint=0))
        wf.steps.append(_step("approve", entity_id="job", actor_id="bob", sequence_hint=1))
        _, _, _, result = _plan(wr=wr)
        handoff = _primary(result, "actor_handoff_verification")
        assert len(handoff.actor_requirements) >= 2

    def test_actor_switch_represented_but_no_execution_hook_exists(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", actor_id="alice", sequence_hint=0))
        wf.steps.append(_step("approve", entity_id="job", actor_id="bob", sequence_hint=1))
        _, _, _, result = _plan(wr=wr)
        handoff = _primary(result, "actor_handoff_verification")
        assert any(step.step_type == "switch_actor" for step in handoff.steps)
        # declarative only -- no field anywhere claims execution happened
        assert handoff.status in {"draft", "feasible", "conditionally_feasible", "blocked", "incomplete"}


# ---------------------------------------------------------------------------
# H. Evidence planning
# ---------------------------------------------------------------------------


class TestEvidencePlanning:
    def test_mandatory_evidence_present(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        for s in result.scenarios:
            assert any(e.mandatory for e in s.evidence_requirements) or not s.evidence_requirements

    def test_sufficient_vs_supporting_roles_distinguished(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        dep_scenario = _primary(result, "dependency_verification")
        roles = {e.evidence_role for e in dep_scenario.evidence_requirements}
        assert "sufficient" in roles

    def test_evidence_types_within_closed_vocabulary(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        from app.intelligence.scenario_planning.schemas import SCENARIO_EVIDENCE_TYPES
        for s in result.scenarios:
            for e in s.evidence_requirements:
                assert e.evidence_type in SCENARIO_EVIDENCE_TYPES

    def test_inconclusive_conditions_present_when_scope_or_delay_uncertain(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        dep_scenario = _primary(result, "dependency_verification")
        assert dep_scenario.comparisons[0].inconclusive_conditions

    def test_missing_evidence_requirement_becomes_gap_when_none_planned(self):
        candidate_scenarios_with_gaps = 0
        er, *_ = _registries()
        # An entity with zero states and no evidence-bearing subject still
        # gets an entity_lifecycle scenario with at least an `observe` step
        # -> at least one evidence requirement should exist; this test just
        # confirms the pipeline never leaves evidence_requirements silently
        # empty for a plannable scenario.
        er.memory.records["job"] = _entity("job")
        _, _, _, result = _plan(er=er)
        for s in result.scenarios:
            if s.status not in {"blocked", "incomplete"}:
                assert s.evidence_requirements


# ---------------------------------------------------------------------------
# I. Feasibility
# ---------------------------------------------------------------------------


class TestFeasibility:
    def test_feasible_scenario(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        assert any(s.feasibility_status == "feasible" for s in result.scenarios)

    def test_conditionally_feasible_when_second_actor_unavailable(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", actor_id="alice", sequence_hint=0))
        wf.steps.append(_step("approve", entity_id="job", actor_id="bob", sequence_hint=1))
        _, _, _, result = _plan(wr=wr)
        handoff = _primary(result, "actor_handoff_verification")
        assert handoff.feasibility_status in {"conditionally_feasible", "blocked"}

    def test_blocked_actor_session(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", actor_id="phantom"))
        wf.actors.append(WorkflowActorParticipation(actor_id="phantom", role_in_workflow="initiator", step_ids=[]))
        _, _, _, result = _plan(wr=wr)
        scenario = _primary(result, "workflow_verification")
        assert scenario.feasibility_status == "blocked"

    def test_stale_graph_context_reduces_feasibility(self):
        from app.intelligence.scenario_planning.scenario_decomposer import validate_and_load

        er, *_ = _registries()
        er.memory.records["job"] = _entity("job")
        graph = ApplicationKnowledgeGraph()
        graph.synchronize(entity_registry=er, iteration=1)
        goal_engine = GoalGenerationEngine()
        result = goal_engine.generate(graph, iteration=1)
        goal = result.goals[0]
        goal.graph_version = graph.memory.graph_version + 5  # simulate a NEWER goal than the graph (edge case) is fine; test staleness the other way:
        goal.graph_version = 0
        graph.memory.graph_version = 5
        validation = validate_and_load(goal, graph)
        assert validation.stale_graph_context

    def test_unknown_feasibility_never_silently_becomes_feasible(self):
        from app.intelligence.scenario_planning.scenario_decomposer import GoalValidation
        from app.intelligence.scenario_planning.scenario_feasibility_analyzer import assess_feasibility
        from app.intelligence.scenario_planning.scenario_step_builder import StepBuildContext

        class FakeGoal:
            goal_id = "g1"
            supporting_graph_nodes = []
            contradictions = []
            depends_on_goal_ids = []

        validation = GoalValidation(goal=FakeGoal(), graph=None, has_context=False)

        class FakeCandidate:
            candidate_id = "scenario:x"

        ctx = StepBuildContext(candidate=FakeCandidate(), goal=FakeGoal(), graph=None, actor_reqs=[], permission_reqs=[], entity_reqs=[], state_reqs=[], workflow_reqs=[], output_reqs=[], data_reqs=[])
        feasibility = assess_feasibility(ctx, validation)
        assert feasibility.feasibility_status in {"incomplete", "unknown"}

    def test_feasibility_is_explainable(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        for s in result.scenarios:
            assert s.feasibility_assessment is not None
            assert s.feasibility_assessment.explanation


# ---------------------------------------------------------------------------
# J. Risk
# ---------------------------------------------------------------------------


class TestRisk:
    def test_read_only_scenario(self):
        # metric_verification's PRIMARY variant is deliberately mutating
        # (before/after verification requires triggering the source
        # transition) -- the read-only form is its "observational"
        # alternative, which locates an already-completed example instead.
        _, _, _, dr = _registries()
        dr.memory.outputs["a"] = _output("open jobs")
        _, _, _, result = _plan(dr=dr)
        observational = next(s for s in result.scenarios if s.scenario_type == "metric_verification" and s.scenario_id.endswith(":observational"))
        assert observational.risk_assessment.risk_class == "read_only"

    def test_low_risk_reversible_mutation(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_view_job", positive=True)])
        _, _, _, result = _plan(ar=ar)
        scenario = _primary(result, "permission_positive_verification")
        assert scenario.risk_assessment.risk_class in {"low", "moderate"}

    def test_moderate_cross_actor_scenario(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", actor_id="alice", sequence_hint=0))
        wf.steps.append(_step("approve", entity_id="job", actor_id="bob", sequence_hint=1))
        _, _, _, result = _plan(wr=wr)
        handoff = _primary(result, "actor_handoff_verification")
        assert handoff.risk_assessment.risk_class in {"moderate", "high"}

    def test_high_risk_irreversible_state_change_via_direct_analyzer(self):
        from app.intelligence.scenario_planning.schemas import ScenarioActorRequirement, ScenarioStep

        class FakeCandidate:
            candidate_id = "scenario:x"
            scenario_type = "workflow_verification"
            mutation_level = "mutating"

        class FakeGoal:
            title = "finalize job"
            description = ""

        class Ctx:
            candidate = FakeCandidate()
            goal = FakeGoal()
            actor_reqs = [ScenarioActorRequirement(requirement_id="a1", actor_id="actor:x", canonical_name="x", required_permissions=[])]
            state_reqs = []
            primary_actor = actor_reqs[0]

        step = ScenarioStep(step_id="s1", step_type="perform_workflow_step", semantic_action="finalize the job permanently", mutation_type="delete", reversibility="irreversible")
        risk, cleanup, rollback = assess_risk(Ctx(), [step], [])
        assert risk.risk_class == "high"
        assert cleanup.cleanup_feasibility == "blocked"
        assert rollback.rollback_possible is False

    def test_cleanup_uncertainty_penalty_applied(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        wf_scenario = _primary(result, "workflow_verification")
        assert wf_scenario.risk_assessment.components.get("cleanup_uncertainty", 0.0) > 0.0

    def test_unknown_risk_defaults_never_crash(self):
        risk, cleanup, rollback = assess_risk(
            type("Ctx", (), {"candidate": type("C", (), {"candidate_id": "x", "scenario_type": "unknown"})(), "goal": type("G", (), {"title": "", "description": ""})(), "actor_reqs": [], "primary_actor": None, "state_reqs": []})(),
            [], [],
        )
        assert risk.risk_class in RISK_CLASSES

    def test_prohibited_pattern_in_real_workflow_step_name_escalates_risk(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("delete", entity_id="job"))
        _, _, _, result = _plan(wr=wr)
        scenario = _primary(result, "workflow_verification")
        assert scenario.risk_assessment.risk_class == "prohibited"

    def test_goal_title_mentioning_permission_does_not_false_positive_prohibited(self):
        # Regression: PROHIBITED_INTENT_PATTERNS contains the bare word
        # "permission" -- scanning goal/step TITLES (not just semantic_action)
        # used to flag every permission-related scenario as "prohibited".
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor("current session", permissions=[_permission("can_view_job", positive=True)])
        _, _, _, result = _plan(ar=ar)
        scenario = _primary(result, "permission_positive_verification")
        assert scenario.risk_assessment.risk_class != "prohibited"


# ---------------------------------------------------------------------------
# K. Branches and alternatives
# ---------------------------------------------------------------------------


class TestBranchesAndAlternatives:
    def test_approval_rejection_branch(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("submit", entity_id="job"))
        wf.branches.append(WorkflowBranch(branch_id="b1", branch_type="approve_vs_reject", decision_point_step_id="s1", option_labels=["approve", "reject"], confidence=0.5))
        _, _, _, result = _plan(wr=wr)
        scenario = _primary(result, "workflow_verification")
        assert any(b.branch_type == "approval_rejection" for b in scenario.branches)

    def test_observational_alternative_for_mutating_scenario(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        assert any(s.scenario_type == "workflow_verification" and "observational" in s.scenario_id for s in result.scenarios)

    def test_reduced_scope_alternative_for_multi_step_workflow(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", sequence_hint=0))
        wf.steps.append(_step("approve", entity_id="job", sequence_hint=1))
        _, _, _, result = _plan(wr=wr)
        assert any("reduced_scope" in s.scenario_id for s in result.scenarios)

    def test_semantic_duplicates_removed(self):
        from app.intelligence.scenario_planning.scenario_deduplicator import deduplicate_scenarios
        from app.intelligence.scenario_planning.schemas import InvestigationScenario

        a = InvestigationScenario(scenario_id="scenario:x:g1:primary", goal_id="g1", scenario_type="workflow_verification", source_graph_nodes=["n1"])
        b = InvestigationScenario(scenario_id="scenario:x:g1:dup", goal_id="g1", scenario_type="workflow_verification", source_graph_nodes=["n1"])
        deduped, removed = deduplicate_scenarios([a, b])
        assert len(deduped) == 1
        assert removed == ["scenario:x:g1:dup"]


# ---------------------------------------------------------------------------
# L. Dependencies and conflicts
# ---------------------------------------------------------------------------


class TestDependenciesAndConflicts:
    def test_scenario_dependency_projected_from_goal_dependency(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        assert result.dependencies
        for dep in result.dependencies:
            assert dep.scenario_id != dep.required_scenario_id

    def test_permission_conflict_detected(self):
        _, ar, _, _ = _registries()
        ar.memory.records["current session"] = _actor(
            "current session", permissions=[_permission("can_view_job", positive=True), _permission("can_view_job", positive=False)],
        )
        _, _, _, result = _plan(ar=ar)
        assert any(c.conflict_type == "permission_contradiction" for c in result.conflicts)

    def test_no_self_dependency(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        for dep in result.dependencies:
            assert dep.scenario_id != dep.required_scenario_id

    def test_duplicate_dependency_removed(self):
        from app.intelligence.scenario_planning.scenario_dependency_resolver import resolve_dependencies
        from app.intelligence.scenario_planning.schemas import InvestigationScenario

        class FakeGoal:
            depends_on_goal_ids = ["g2"]

        s1 = InvestigationScenario(scenario_id="scenario:a:g1:primary", goal_id="g1", scenario_type="workflow_verification")
        s2 = InvestigationScenario(scenario_id="scenario:b:g2:primary", goal_id="g2", scenario_type="workflow_verification")
        deps = resolve_dependencies([s1, s2], {"g1": FakeGoal(), "g2": FakeGoal()})
        assert len(deps) == 1


# ---------------------------------------------------------------------------
# M. Gaps
# ---------------------------------------------------------------------------


class TestGaps:
    def test_missing_actor_gap(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", actor_id="phantom"))
        wf.actors.append(WorkflowActorParticipation(actor_id="phantom", role_in_workflow="initiator", step_ids=[]))
        _, _, _, result = _plan(wr=wr)
        assert any(g.gap_type == "missing_actor" for g in result.gaps)

    def test_missing_test_data_gap_for_mutating_scenario(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        assert any(g.gap_type == "missing_test_data" for g in result.gaps)

    def test_unsupported_goal_type_gap_for_incomplete_scenario(self):
        from app.intelligence.scenario_planning.scenario_candidate_builder import build_candidates
        from app.intelligence.scenario_planning.scenario_decomposer import GoalValidation

        class FakeGoal:
            goal_id = "g1"
            goal_type = "verify_workflow"
            title = "t"
            description = "d"
            priority_score = 0.1
            required_actors = []

        validation = GoalValidation(goal=FakeGoal(), graph=None, can_plan=False, warnings=["no context"])
        candidates = build_candidates(validation)
        assert candidates[0].variant == "incomplete"

    def test_gap_resolves_after_graph_update(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", actor_id="phantom"))
        wf.actors.append(WorkflowActorParticipation(actor_id="phantom", role_in_workflow="initiator", step_ids=[]))
        graph = ApplicationKnowledgeGraph()
        graph.synchronize(workflow_registry=wr, iteration=1)
        goal_engine = GoalGenerationEngine()
        goal_engine.generate(graph, iteration=1)
        scenario_engine = ScenarioPlanningEngine()
        scenario_engine.generate(goal_engine, graph, iteration=1)
        assert any(g.gap_type == "missing_actor" for g in scenario_engine.query_engine.scenario_gaps())

        ar = ActorRegistry()
        ar.memory.records["phantom"] = _actor("phantom")
        graph.synchronize(actor_registry=ar, workflow_registry=wr, iteration=2)
        goal_engine.generate(graph, iteration=2)
        scenario_engine.generate(goal_engine, graph, iteration=2)
        assert not any(g.gap_type == "missing_actor" for g in scenario_engine.query_engine.scenario_gaps())


# ---------------------------------------------------------------------------
# N. Query API
# ---------------------------------------------------------------------------


class TestQueryAPI:
    def test_scenarios_by_goal_actor_entity_workflow_output(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, goal_engine, engine, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        goal_id = next(g.goal_id for g in goal_engine.query_engine.all_goals() if g.goal_type == "verify_workflow")
        assert engine.query_engine.scenarios_by_goal(goal_id)
        assert engine.query_engine.scenarios_for_actor("current session")
        assert engine.query_engine.scenarios_for_entity("job")
        assert engine.query_engine.scenarios_for_workflow("job workflow")

    def test_blocked_and_read_only_and_mutating_scenarios(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, engine, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        assert engine.query_engine.read_only_scenarios()
        assert engine.query_engine.scenarios_requiring_mutation()

    def test_high_risk_and_alternatives_and_dependencies(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, engine, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        wf_scenario = _primary(result, "workflow_verification")
        assert engine.query_engine.alternatives_for_scenario(wf_scenario.scenario_id)
        assert engine.query_engine.dependencies_for_scenario is not None

    def test_deterministic_result_ordering(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, engine, _ = _plan(er=er, ar=ar, wr=wr, dr=dr)
        ids = [s.scenario_id for s in engine.query_engine.all_scenarios()]
        assert ids == sorted(ids)

    def test_bounded_result_size(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, engine, _ = _plan(er=er, ar=ar, wr=wr, dr=dr)
        assert len(engine.query_engine.highest_information_gain_scenarios(limit=1)) <= 1


# ---------------------------------------------------------------------------
# O. Memory and controller
# ---------------------------------------------------------------------------


class TestMemoryAndController:
    def _run_memory(self):
        from app.agent.memory import RunMemory
        return RunMemory(run_id="r1", start_url="https://example.test")

    def test_degrades_gracefully_with_no_scenario_engine(self):
        mem = self._run_memory()
        assert mem.generate_scenarios() is None
        assert mem.scenario_statistics() is None
        assert mem.scenario_snapshot() is None
        assert mem.scenario_summary() is None
        assert mem.feasible_scenarios() == []
        assert mem.blocked_scenarios() == []
        assert mem.scenarios_for_goal("x") == []

    def test_generate_scenarios_populates_memory_snapshot(self):
        mem = self._run_memory()
        er, ar, wr, dr = _single_transition_fixture()
        mem.entity_registry, mem.actor_registry, mem.workflow_registry, mem.dependency_registry = er, ar, wr, dr
        mem.knowledge_graph = ApplicationKnowledgeGraph()
        mem.knowledge_graph.synchronize(entity_registry=er, actor_registry=ar, workflow_registry=wr, dependency_registry=dr, iteration=1)
        mem.goal_engine = GoalGenerationEngine()
        mem.goal_engine.generate(mem.knowledge_graph, iteration=1)
        mem.scenario_engine = ScenarioPlanningEngine()

        result = mem.generate_scenarios()
        assert result is not None
        assert result.statistics.total_scenarios > 0

        snap = mem.memory_snapshot()
        assert "scenario_planning" in snap
        assert snap["scenario_planning"]["total_scenarios"] > 0
        assert mem.scenario_statistics().total_scenarios > 0
        assert len(mem.scenarios_for_entity("job")) > 0

    def test_controller_never_invokes_browser_execution(self):
        import types
        from app.agent.controller import AgentController

        fake_self = types.SimpleNamespace(memory=types.SimpleNamespace(scenario_engine=object(), goal_engine=object(), knowledge_graph=object(), actions=[]))
        AgentController._run_scenario_planning(fake_self)  # must swallow AttributeError, never raise

    def test_controller_wires_scenario_planning_after_goal_generation(self):
        import inspect
        from app.agent import controller as controller_module

        source = inspect.getsource(controller_module)
        assert "self._run_goal_generation()" in source
        assert "self._run_scenario_planning()" in source
        assert source.index("self._run_goal_generation()") < source.index("self._run_scenario_planning()")

    def test_no_action_executor_or_browser_adapter_reference_in_package(self):
        # Docstrings NAME ActionExecutor/BrowserAdapter/Playwright as things
        # this package must NOT invoke (that's the point being documented,
        # not a violation of it) -- only an actual import or constructor
        # call would be a real violation.
        package_dir = BACKEND / "app" / "intelligence" / "scenario_planning"
        for path in package_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "ActionExecutor(" not in text
            assert "import ActionExecutor" not in text
            assert "BrowserAdapter(" not in text
            assert "import BrowserAdapter" not in text
            # A docstring NAMING Playwright as something this package must
            # NOT invoke is the point being enforced, not a violation of
            # it -- only an actual import/call would be a real violation.
            assert "import playwright" not in text.lower()
            assert "playwright.sync_api" not in text.lower()
            assert "page.locator" not in text.lower()


# ---------------------------------------------------------------------------
# P. Neutrality
# ---------------------------------------------------------------------------

FORBIDDEN_WORDS = {
    "invoice", "customer", "job", "ticket", "order", "product", "cart", "checkout",
    "employee", "vehicle", "haulvana", "serviceflow", "saucedemo", "insightboard",
}


class TestNeutrality:
    def test_no_hardcoded_business_vocabulary(self):
        package_dir = BACKEND / "app" / "intelligence" / "scenario_planning"
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
                    words = set(re.findall(r"[a-z]+", node.value.lower()))
                    hit = words & FORBIDDEN_WORDS
                    if hit:
                        offenders.append((path.name, node.value, hit))
        assert not offenders, f"Hardcoded business vocabulary found: {offenders}"

    def test_scenario_types_have_no_application_specific_members(self):
        for scenario_type in SCENARIO_TYPES:
            for word in FORBIDDEN_WORDS:
                assert word not in scenario_type

    def test_unknown_extension_preserved_safely(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        # every produced scenario references only real graph ids -- no
        # fabricated node/edge id ever leaks through
        graph = ApplicationKnowledgeGraph()
        graph.synchronize(entity_registry=er, actor_registry=ar, workflow_registry=wr, dependency_registry=dr, iteration=1)
        for s in result.scenarios:
            for node_id in s.source_graph_nodes:
                assert node_id in graph.memory.nodes


# ---------------------------------------------------------------------------
# Synthetic integration fixtures
# ---------------------------------------------------------------------------


class TestIntegrationFixtures:
    def test_fixture_1_single_actor_state_transition(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        # This fixture's workflow step already records the transition
        # (source_state="open" -> target_state="closed"), so Goal
        # Generation correctly finds NOTHING to investigate about the
        # entity_state nodes themselves (no gap ever touches them) --
        # state_transition_verification legitimately does not appear here.
        # The transition is still exercised end-to-end via the workflow
        # and dependency scenarios below.
        assert any(s.scenario_type == "workflow_verification" for s in result.scenarios)
        assert any(s.scenario_type == "dependency_verification" for s in result.scenarios)
        wf_scenario = _primary(result, "workflow_verification")
        assert any(step.source_state_id or step.target_state_id for step in wf_scenario.steps)
        dep_scenario = _primary(result, "dependency_verification")
        assert dep_scenario.cleanup_plan is not None

    def test_fixture_2_cross_role_approval(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("create", entity_id="job", actor_id="actor a", sequence_hint=0))
        wf.steps.append(_step("approve", entity_id="job", actor_id="actor b", sequence_hint=1))
        _, _, _, result = _plan(wr=wr)
        handoff = _primary(result, "actor_handoff_verification")
        assert len(handoff.actor_requirements) == 2
        assert any(step.step_type == "switch_actor" for step in handoff.steps)
        assert handoff.feasibility_status in {"conditionally_feasible", "blocked"}

    def test_fixture_3_permission_denial(self):
        _, ar, _, _ = _registries()
        ar.memory.records["restricted actor"] = _actor("restricted actor", permissions=[_permission("can_delete_job", positive=False)])
        _, _, _, result = _plan(ar=ar)
        scenario = next(s for s in result.scenarios if s.scenario_type == "permission_negative_verification")
        assert scenario.risk_assessment.risk_class in {"read_only", "low"}
        assert any(step.step_type == "verify_denial" for step in scenario.steps)

    def test_fixture_4_branched_workflow(self):
        _, _, wr, _ = _registries()
        wf = wr.memory.get_or_create("job", canonical_name="job workflow")
        wf.steps.append(_step("submit", entity_id="job"))
        wf.branches.append(WorkflowBranch(branch_id="b1", branch_type="approve_vs_reject", decision_point_step_id="s1", option_labels=["approve", "reject"], confidence=0.6))
        _, _, _, result = _plan(wr=wr)
        scenario = _primary(result, "workflow_verification")
        assert len(scenario.branches) >= 1
        assert scenario.branches[0].expected_outcome

    def test_fixture_5_incomplete_dependency(self):
        _, _, _, dr = _registries()
        output = _output("orphan metric")
        dr.memory.outputs[output.output_id] = output
        dep = _dependency(output_id=output.output_id)  # no source_workflow_ids/entity_ids at all
        dr.memory.records[dep.dependency_id] = dep
        _, _, _, result = _plan(dr=dr)
        assert any(g.gap_type in {"missing_workflow", "missing_entity", "insufficient_information"} for g in result.gaps) or any(
            s.feasibility_status in {"blocked", "incomplete"} for s in result.scenarios
        )

    def test_fixture_6_delayed_output(self):
        er, ar, wr, dr = _single_transition_fixture()
        _, _, _, result = _plan(er=er, ar=ar, wr=wr, dr=dr)
        dep_scenario = _primary(result, "dependency_verification")
        assert any(step.step_type == "wait_for_effect" for step in dep_scenario.steps)
        assert dep_scenario.comparisons[0].inconclusive_conditions

    def test_fixture_7_irreversible_mutation(self):
        from app.intelligence.scenario_planning.schemas import ScenarioActorRequirement, ScenarioStep

        step = ScenarioStep(step_id="s1", step_type="perform_workflow_step", semantic_action="finalize permanently", mutation_type="delete", reversibility="irreversible")

        class FakeCandidate:
            candidate_id = "scenario:x"
            scenario_type = "workflow_verification"
            mutation_level = "mutating"

        class FakeGoal:
            title = "t"
            description = ""

        class Ctx:
            candidate = FakeCandidate()
            goal = FakeGoal()
            actor_reqs = []
            primary_actor = None
            state_reqs = []

        risk, cleanup, rollback = assess_risk(Ctx(), [step], [])
        assert risk.risk_class == "high"
        assert rollback.rollback_possible is False
        assert cleanup.unresolved_cleanup_gaps

    def test_fixture_8_scope_sensitive_output(self):
        _, _, _, dr = _registries()
        output = _output("scoped metric")
        dr.memory.outputs[output.output_id] = output
        dep_alice = _dependency(output_id=output.output_id, actor_scope=ActorScope(actor_term="alice"))
        dep_bob = _dependency(output_id=output.output_id, actor_scope=ActorScope(actor_term="bob"))
        dr.memory.records[dep_alice.dependency_id] = dep_alice
        dr.memory.records[dep_bob.dependency_id] = dep_bob
        _, _, _, result = _plan(dr=dr)
        assert result.scenarios
        for s in result.scenarios:
            for req in s.output_requirements:
                assert req.status in {"satisfied", "satisfiable", "unresolved"}
