"""QA Strategy Engine (app/intelligence/qa_strategy/).

Covers: priority calculation (A), queue generation (B), batch generation
(C), dependency ordering (D), blocked/deferred scenarios (E), batch
optimization (F), coverage/confidence/risk forecasting (G), recommendation
explanations (H), memory/controller integration (I), query API (J),
deterministic ordering (K), idempotency (L), neutrality (M) -- built on
top of the real Scenario Planning -> Goal Generation -> Knowledge Graph
pipeline, exactly like `test_scenario_planning.py`.
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
from app.intelligence.dependency_discovery import DependencyRegistry
from app.intelligence.dependency_discovery.schemas import DependencyDescriptor, DependencyEvidence, DerivedOutputDescriptor
from app.intelligence.entity_discovery import EntityRegistry
from app.intelligence.entity_discovery.schemas import EntityEvidence, EntityRecord
from app.intelligence.goal_generation import GoalGenerationEngine
from app.intelligence.knowledge_graph import ApplicationKnowledgeGraph
from app.intelligence.qa_strategy import QAStrategyEngine
from app.intelligence.qa_strategy.schemas import BATCH_TYPES, POLICY_TYPES, QUEUE_TYPES, RECOMMENDED_ACTIONS
from app.intelligence.qa_strategy.strategy_scoring import POLICY_WEIGHTS
from app.intelligence.scenario_planning import ScenarioPlanningEngine
from app.intelligence.workflow_discovery import WorkflowRegistry
from app.intelligence.workflow_discovery.schemas import WorkflowActorParticipation, WorkflowEntityParticipation, WorkflowStep

# ---------------------------------------------------------------------------
# Helpers (mirrors test_scenario_planning.py's fixture-building helpers)
# ---------------------------------------------------------------------------


def _entity(term, *, states=None, confidence=0.8) -> EntityRecord:
    return EntityRecord(
        canonical_name=term, status="confirmed", confidence=confidence, known_states=states or [],
        evidence=[EntityEvidence(source_kind="heading", observed_text=term)],
    )


def _actor(term, *, permissions=None, confidence=0.7) -> ActorRecord:
    return ActorRecord(
        canonical_name=term, status="confirmed", confidence=confidence, known_permissions=permissions or [],
        supporting_evidence=[ActorEvidence(source_kind="session_info", observed_text=term)],
    )


def _permission(permission_id, *, confidence=0.6) -> PermissionCandidate:
    ev = ActorEvidence(source_kind="visible_control", observed_text=permission_id)
    return PermissionCandidate(permission_id=permission_id, confidence=confidence, positive_evidence=[ev])


def _step(verb, *, entity_id=None, actor_id="current session", sequence_hint=0, source_state=None, target_state=None) -> WorkflowStep:
    return WorkflowStep(
        semantic_action=verb, entity_id=entity_id, actor_id=actor_id, status="observed", sequence_hint=sequence_hint,
        source_state=source_state, target_state=target_state, page_id="p", page_state_fingerprint="f", confidence=0.6,
    )


def _output(label, *, output_type="kpi_card", confidence=0.6, output_id=None) -> DerivedOutputDescriptor:
    kwargs = {"output_id": output_id} if output_id else {}
    return DerivedOutputDescriptor(canonical_label=label, output_type=output_type, raw_value="5", confidence=confidence, evidence=[DependencyEvidence(source_kind="heading", observed_text=label)], **kwargs)


def _dependency(*, workflow_ids=None, output_id, confidence=0.6, effect_direction="unknown", dependency_id=None) -> DependencyDescriptor:
    kwargs = {"dependency_id": dependency_id} if dependency_id else {}
    return DependencyDescriptor(
        canonical_name=f"dep -> {output_id}", dependency_type="entity_dependency", relationship_type="counts",
        source_workflow_ids=workflow_ids or [], target_output_ids=[output_id], status="observed", confidence=confidence,
        effect_direction=effect_direction, supporting_evidence=[DependencyEvidence(source_kind="entity_registry", observed_text="matched")],
        **kwargs,
    )


def _registries():
    return EntityRegistry(), ActorRegistry(), WorkflowRegistry(), DependencyRegistry()


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


def _multi_actor_fixture():
    """Adds a second actor/workflow so batching has more than one
    actor_workflow group to separate, and a cross-actor scenario can occur."""
    er, ar, wr, dr = _single_transition_fixture()
    er.memory.records["report"] = _entity("report", states=["draft", "published"])
    ar.memory.records["reviewer"] = _actor("reviewer", permissions=[_permission("can_publish_report", confidence=0.5)])
    wf2 = wr.memory.get_or_create("report", canonical_name="report workflow")
    wf2.steps.append(_step("publish", entity_id="report", actor_id="reviewer", sequence_hint=0, source_state="draft", target_state="published"))
    wf2.actors.append(WorkflowActorParticipation(actor_id="reviewer", role_in_workflow="approver", step_ids=[]))
    wf2.entities.append(WorkflowEntityParticipation(entity_id="report", role_in_workflow="subject", step_ids=[]))
    return er, ar, wr, dr


def _plan(*, er=None, ar=None, wr=None, dr=None, iteration=1):
    graph = ApplicationKnowledgeGraph()
    graph.synchronize(entity_registry=er, actor_registry=ar, workflow_registry=wr, dependency_registry=dr, iteration=iteration)
    goal_engine = GoalGenerationEngine()
    goal_engine.generate(graph, iteration=iteration)
    scenario_engine = ScenarioPlanningEngine()
    scenario_engine.generate(goal_engine, graph, iteration=iteration)
    return graph, goal_engine, scenario_engine


def _strategize(graph, goal_engine, scenario_engine, *, policy_id="balanced", weight_overrides=None, engine=None):
    engine = engine or QAStrategyEngine()
    result = engine.generate(scenario_engine, goal_engine, graph, policy_id=policy_id, weight_overrides=weight_overrides)
    return engine, result


# ---------------------------------------------------------------------------
# A. Priority calculation
# ---------------------------------------------------------------------------


class TestPriorityCalculation:
    def test_every_candidate_has_bounded_explainable_priority(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        assert result.candidates
        for c in result.candidates:
            assert 0.0 <= c.priority_score <= 1.0
            assert c.priority_breakdown
            assert c.explanation

    def test_all_policy_weight_profiles_sum_to_one(self):
        for policy_id, weights in POLICY_WEIGHTS.items():
            assert abs(sum(weights.values()) - 1.0) < 1e-9, policy_id

    def test_high_risk_scenario_scores_lower_than_low_risk_under_balanced_policy(self):
        er, ar, wr, dr = _multi_actor_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine, policy_id="balanced")
        by_risk = {}
        for c in result.candidates:
            by_risk.setdefault(c.risk_class, []).append(c.priority_score)
        if "high" in by_risk and "read_only" in by_risk:
            assert max(by_risk["high"]) <= max(by_risk["read_only"]) + 1e-9

    def test_blocking_impact_reflects_real_dependents(self):
        # Regression: blocking_impact was hard-coded to 0.0 in the candidate
        # builder despite being a weighted priority signal -- a candidate
        # that other scenarios genuinely depend on (via Scenario Planning's
        # own ScenarioDependency records) must score above zero.
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        assert scenario_engine.memory.dependencies  # fixture must actually produce dependencies
        required_scenario_ids = {d.required_scenario_id for d in scenario_engine.memory.dependencies.values() if d.blocking}
        _, result = _strategize(graph, goal_engine, scenario_engine)
        candidates_by_scenario = {c.scenario_id: c for c in result.candidates}
        for scenario_id in required_scenario_ids:
            assert candidates_by_scenario[scenario_id].blocking_impact > 0.0
            assert candidates_by_scenario[scenario_id].priority_breakdown["blocking_impact"] > 0.0

    def test_policy_changes_relative_ordering(self):
        er, ar, wr, dr = _multi_actor_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, balanced = _strategize(graph, goal_engine, scenario_engine, policy_id="balanced")
        _, read_only_first = _strategize(graph, goal_engine, scenario_engine, policy_id="read_only_first")
        balanced_scores = {c.candidate_id: c.priority_score for c in balanced.candidates}
        read_only_scores = {c.candidate_id: c.priority_score for c in read_only_first.candidates}
        assert balanced_scores != read_only_scores


# ---------------------------------------------------------------------------
# B. Queue generation
# ---------------------------------------------------------------------------


class TestQueueGeneration:
    def test_exactly_ten_queues_always_produced(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        assert {q.queue_type for q in result.queues} == QUEUE_TYPES
        assert len(result.queues) == 10

    def test_every_candidate_assigned_to_exactly_one_queue(self):
        er, ar, wr, dr = _multi_actor_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        seen = []
        for q in result.queues:
            seen.extend(q.candidate_ids)
        assert sorted(seen) == sorted(c.candidate_id for c in result.candidates)
        assert len(seen) == len(set(seen))  # no duplicates across queues

    def test_queue_membership_matches_recommended_action(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        for c in result.candidates:
            if c.recommended_action == "block":
                assert c.queue_type == "blocked"
            if c.recommended_action in {"defer", "skip"}:
                assert c.queue_type in {"deferred", "unknown"} or c.feasibility_status == "unknown"


# ---------------------------------------------------------------------------
# C. Batch generation / F. Batch optimization
# ---------------------------------------------------------------------------


class TestBatchGeneration:
    def test_batches_use_only_closed_batch_types(self):
        er, ar, wr, dr = _multi_actor_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        for b in result.batches:
            assert b.batch_type in BATCH_TYPES

    def test_executing_candidates_in_same_batch_share_actor_when_possible(self):
        er, ar, wr, dr = _multi_actor_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        candidates_by_id = {c.candidate_id: c for c in result.candidates}
        for b in result.batches:
            if b.batch_type in {"actor_workflow", "actor_entity", "actor_output", "actor"}:
                actors = {candidates_by_id[cid].primary_actor for cid in b.candidate_ids if cid in candidates_by_id}
                assert len(actors) <= 1

    def test_no_candidate_in_two_batches(self):
        er, ar, wr, dr = _multi_actor_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        seen = []
        for b in result.batches:
            seen.extend(b.candidate_ids)
        assert len(seen) == len(set(seen))

    def test_only_executing_candidates_are_batched(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        batched_ids = {cid for b in result.batches for cid in b.candidate_ids}
        candidates_by_id = {c.candidate_id: c for c in result.candidates}
        for cid in batched_ids:
            assert candidates_by_id[cid].recommended_action == "execute"


# ---------------------------------------------------------------------------
# D. Dependency ordering / E. Blocked & deferred
# ---------------------------------------------------------------------------


class TestDependencyOrdering:
    def test_dependencies_only_reference_known_candidates(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        known = {c.candidate_id for c in result.candidates}
        for d in result.dependencies:
            assert d.candidate_id in known
            assert d.required_candidate_id in known

    def test_blocked_candidate_never_recommended_execute(self):
        from app.intelligence.qa_strategy.strategy_candidate_builder import build_candidate

        class Obj:
            def __init__(self, **kw): self.__dict__.update(kw)

        scenario = Obj(
            scenario_id="scn:prohibited:primary", goal_id="goal:x", scenario_type="workflow_verification",
            information_gain_score=0.5, confidence_gain_estimate=0.5, complexity_score=0.2, risk_score=0.9,
            risk_assessment=Obj(risk_class="prohibited"), feasibility_status="feasible",
            feasibility_assessment=Obj(blocking_reasons=[], conditional_reasons=[]),
            cleanup_plan=Obj(cleanup_required=False, cleanup_feasibility="feasible"),
            actor_requirements=[], workflow_requirements=[], entity_requirements=[], output_requirements=[], data_requirements=[],
            source_graph_nodes=[], graph_version=1, goal_version=1, estimated_runtime_class="simple",
        )
        candidate = build_candidate(scenario, None, None, max_degree=1)
        assert candidate.recommended_action == "block"
        assert candidate.risk_class == "prohibited"

    def test_deferred_candidate_waits_on_higher_priority_prerequisite(self):
        from app.intelligence.qa_strategy.schemas import ExecutionDependency
        from app.intelligence.qa_strategy.strategy_candidate_builder import build_candidate, refine_actions

        class Obj:
            def __init__(self, **kw): self.__dict__.update(kw)

        def scenario(sid, goal_id, risk="low"):
            return Obj(
                scenario_id=sid, goal_id=goal_id, scenario_type="workflow_verification",
                information_gain_score=0.3, confidence_gain_estimate=0.3, complexity_score=0.3, risk_score=0.2,
                risk_assessment=Obj(risk_class=risk), feasibility_status="feasible",
                feasibility_assessment=Obj(blocking_reasons=[], conditional_reasons=[]),
                cleanup_plan=Obj(cleanup_required=False, cleanup_feasibility="feasible"),
                actor_requirements=[], workflow_requirements=[], entity_requirements=[], output_requirements=[], data_requirements=[],
                source_graph_nodes=[], graph_version=1, goal_version=1, estimated_runtime_class="simple",
            )

        goal_low = Obj(business_value=0.2, priority_score=0.2, risk_score=0.1, coverage_value=0.2, goal_status="active")
        goal_high = Obj(business_value=0.9, priority_score=0.9, risk_score=0.5, coverage_value=0.8, goal_status="active")

        # The DEPENDENT has the higher-priority goal (so it would naturally
        # sort ahead of its own prerequisite) -- that is exactly the case
        # `refine_actions` must catch and defer, since running it first
        # would violate the dependency.
        dependent = build_candidate(scenario("scn:a:primary", "goal:a"), goal_high, None, max_degree=1)
        prerequisite = build_candidate(scenario("scn:b:primary", "goal:b"), goal_low, None, max_degree=1)
        assert dependent.priority_score > prerequisite.priority_score

        dep = ExecutionDependency(
            dependency_id="exec-dependency:1", candidate_id=dependent.candidate_id, required_candidate_id=prerequisite.candidate_id,
            dependency_type="scenario_prerequisite", reason="needs prerequisite first", blocking=True, confidence=0.8,
        )
        refine_actions([dependent, prerequisite], [dep])
        assert dependent.recommended_action == "defer"
        assert prerequisite.recommended_action == "execute"


# ---------------------------------------------------------------------------
# G. Forecasting
# ---------------------------------------------------------------------------


class TestForecasting:
    def test_three_forecast_types_always_produced(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        assert {f.forecast_type for f in result.forecasts} == {"coverage", "confidence", "risk"}

    def test_forecasts_are_bounded_and_labelled_approximate(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        for f in result.forecasts:
            assert 0.0 <= f.baseline_value <= 1.0
            assert 0.0 <= f.projected_value <= 1.0
            assert f.confidence_in_forecast <= 0.6  # never overclaims certainty
            assert f.basis


# ---------------------------------------------------------------------------
# H. Recommendation explanations
# ---------------------------------------------------------------------------


class TestRecommendations:
    def test_every_candidate_has_one_recommendation_matching_its_decision(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        recs_by_candidate = {r.candidate_id: r for r in result.recommendations}
        for c in result.candidates:
            rec = recs_by_candidate[c.candidate_id]
            assert rec.decision == c.recommended_action
            assert rec.reasons
            assert rec.decision in RECOMMENDED_ACTIONS


# ---------------------------------------------------------------------------
# I. Memory / controller integration
# ---------------------------------------------------------------------------


class TestMemoryAndController:
    def _run_memory(self):
        from app.agent.memory import RunMemory
        return RunMemory(run_id="r1", start_url="https://example.test")

    def test_degrades_gracefully_with_no_strategy_engine(self):
        mem = self._run_memory()
        assert mem.generate_strategy() is None
        assert mem.execution_statistics() is None
        assert mem.strategy_summary() is None
        assert mem.strategy_result() == []
        assert mem.execution_batches() == []
        assert mem.blocked_candidates() == []
        assert mem.ready_candidates() == []
        assert mem.next_execution() is None
        assert mem.coverage_forecast() is None

    def test_generate_strategy_populates_memory_snapshot(self):
        mem = self._run_memory()
        er, ar, wr, dr = _single_transition_fixture()
        mem.entity_registry, mem.actor_registry, mem.workflow_registry, mem.dependency_registry = er, ar, wr, dr
        mem.knowledge_graph = ApplicationKnowledgeGraph()
        mem.knowledge_graph.synchronize(entity_registry=er, actor_registry=ar, workflow_registry=wr, dependency_registry=dr, iteration=1)
        mem.goal_engine = GoalGenerationEngine()
        mem.goal_engine.generate(mem.knowledge_graph, iteration=1)
        mem.scenario_engine = ScenarioPlanningEngine()
        mem.scenario_engine.generate(mem.goal_engine, mem.knowledge_graph, iteration=1)
        mem.strategy_engine = QAStrategyEngine()

        result = mem.generate_strategy()
        assert result is not None
        assert result.statistics.total_candidates > 0

        snap = mem.memory_snapshot()
        assert "qa_strategy" in snap
        assert snap["qa_strategy"]["total_candidates"] > 0
        assert mem.execution_statistics().total_candidates > 0

    def test_controller_never_invokes_browser_execution(self):
        import types
        from app.agent.controller import AgentController

        fake_self = types.SimpleNamespace(memory=types.SimpleNamespace(strategy_engine=object(), scenario_engine=object(), goal_engine=object(), knowledge_graph=object(), actions=[]))
        AgentController._run_qa_strategy(fake_self)  # must swallow AttributeError, never raise

    def test_controller_wires_qa_strategy_after_scenario_planning(self):
        import inspect
        from app.agent import controller as controller_module

        source = inspect.getsource(controller_module)
        assert "self._run_scenario_planning()" in source
        assert "self._run_qa_strategy()" in source
        assert source.index("self._run_scenario_planning()") < source.index("self._run_qa_strategy()")

    def test_no_action_executor_or_browser_adapter_reference_in_package(self):
        package_dir = BACKEND / "app" / "intelligence" / "qa_strategy"
        for path in package_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "ActionExecutor(" not in text
            assert "import ActionExecutor" not in text
            assert "BrowserAdapter(" not in text
            assert "import BrowserAdapter" not in text
            assert "import playwright" not in text.lower()
            assert "playwright.sync_api" not in text.lower()
            assert "page.locator" not in text.lower()


# ---------------------------------------------------------------------------
# J. Query API
# ---------------------------------------------------------------------------


class TestQueryAPI:
    def test_ready_returns_only_executing_candidates_in_priority_order(self):
        er, ar, wr, dr = _multi_actor_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        engine, _ = _strategize(graph, goal_engine, scenario_engine)
        ready = engine.query_engine.ready()
        assert all(c.recommended_action == "execute" for c in ready)
        scores = [c.priority_score for c in ready]
        assert scores == sorted(scores, reverse=True)

    def test_next_execution_is_top_of_ready(self):
        er, ar, wr, dr = _multi_actor_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        engine, _ = _strategize(graph, goal_engine, scenario_engine)
        ready = engine.query_engine.ready()
        nxt = engine.query_engine.next_execution()
        if ready:
            assert nxt.candidate_id == ready[0].candidate_id
        else:
            assert nxt is None

    def test_blocked_and_deferred_disjoint_from_ready(self):
        er, ar, wr, dr = _multi_actor_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        engine, _ = _strategize(graph, goal_engine, scenario_engine)
        ready_ids = {c.candidate_id for c in engine.query_engine.ready()}
        blocked_ids = {c.candidate_id for c in engine.query_engine.blocked()}
        deferred_ids = {c.candidate_id for c in engine.query_engine.deferred()}
        assert not (ready_ids & blocked_ids)
        assert not (ready_ids & deferred_ids)

    def test_recommended_sequence_matches_ordering(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        engine, result = _strategize(graph, goal_engine, scenario_engine)
        assert engine.query_engine.recommended_sequence() == result.ordering.ordered_candidate_ids

    def test_highest_value_and_lowest_risk_bounded_by_limit(self):
        er, ar, wr, dr = _multi_actor_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        engine, _ = _strategize(graph, goal_engine, scenario_engine)
        assert len(engine.query_engine.highest_value(limit=1)) <= 1
        assert len(engine.query_engine.lowest_risk(limit=1)) <= 1


# ---------------------------------------------------------------------------
# K. Deterministic ordering / L. Idempotency
# ---------------------------------------------------------------------------


class TestDeterminismAndIdempotency:
    def test_repeated_generation_is_idempotent(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        engine = QAStrategyEngine()
        _, first = _strategize(graph, goal_engine, scenario_engine, engine=engine)
        _, second = _strategize(graph, goal_engine, scenario_engine, engine=engine)
        assert first.strategy_version == second.strategy_version
        first_by_id = {c.candidate_id: c for c in first.candidates}
        for c in second.candidates:
            assert c.created_at == first_by_id[c.candidate_id].created_at

    def test_two_independent_engines_produce_identical_ordering(self):
        er, ar, wr, dr = _multi_actor_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result_a = _strategize(graph, goal_engine, scenario_engine, engine=QAStrategyEngine())
        _, result_b = _strategize(graph, goal_engine, scenario_engine, engine=QAStrategyEngine())
        assert result_a.ordering.ordered_candidate_ids == result_b.ordering.ordered_candidate_ids

    def test_candidate_ids_are_deterministic_from_scenario_id(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        for c in result.candidates:
            assert c.candidate_id == f"candidate:{c.scenario_id}"


# ---------------------------------------------------------------------------
# M. Neutrality
# ---------------------------------------------------------------------------

FORBIDDEN_WORDS = {
    "invoice", "customer", "job", "ticket", "order", "product", "cart", "checkout",
    "employee", "vehicle", "haulvana", "serviceflow", "saucedemo", "insightboard",
}


class TestNeutrality:
    def test_no_hardcoded_business_vocabulary(self):
        package_dir = BACKEND / "app" / "intelligence" / "qa_strategy"
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

    def test_policy_types_have_no_application_specific_members(self):
        for policy_id in POLICY_TYPES:
            for word in FORBIDDEN_WORDS:
                assert word not in policy_id

    def test_candidates_only_reference_real_scenario_and_goal_ids(self):
        er, ar, wr, dr = _single_transition_fixture()
        graph, goal_engine, scenario_engine = _plan(er=er, ar=ar, wr=wr, dr=dr)
        _, result = _strategize(graph, goal_engine, scenario_engine)
        real_scenario_ids = {s.scenario_id for s in scenario_engine.query_engine.all_scenarios()}
        real_goal_ids = {g.goal_id for g in goal_engine.query_engine.all_goals()}
        for c in result.candidates:
            assert c.scenario_id in real_scenario_ids
            assert c.goal_id in real_goal_ids
