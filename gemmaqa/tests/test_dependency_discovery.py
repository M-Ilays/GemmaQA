"""Business Dependency Discovery Engine (app/intelligence/dependency_discovery/).

Covers: derived output discovery (KPI/badge/queue/chart/report/table-total/
notification, with static/date/version/phone/identifier/stepper rejection),
entity dependencies (label match, API resource match, ambiguous candidates,
no match, alias merging), workflow dependencies (create/delete/state-
transition/assignment/approval/cancellation effect-direction inference),
actor dependencies (same actor, cross-role, processing actor, unresolved
verification actor, single-role application), aggregation rule inference
(count/filtered-count/sum/average/percentage/grouped/ambiguous/competing),
scope (date/tenant/actor/tab/status filter, incompatible-not-compared,
compatible-correlated), before/after correlation (increase/decrease/
category-move/no-change/delayed/output-without-source/source-without-
output), registry/merging (duplicate KPI across pages/actors/sessions,
aliases, repeated evidence, contradictions, stable ids), confidence
(correlated > label-only, contradiction lowers, duplicate evidence bounded,
inferred < verified, incompatible scope doesn't raise), and application
neutrality (AST contract + generic fixtures + unknown output types kept).
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest

from app.intelligence.actor_discovery import ActorRegistry
from app.intelligence.entity_discovery import EntityRegistry
from app.intelligence.entity_discovery.schemas import EntityRecord
from app.intelligence.workflow_discovery import WorkflowRegistry
from app.intelligence.workflow_discovery.schemas import (
    WorkflowActorParticipation,
    WorkflowEntityParticipation,
    WorkflowStep,
    WorkflowTransition,
)
from app.intelligence.dependency_discovery import DependencyDiscoveryEngine
from app.intelligence.dependency_discovery import dependency_confidence
from app.intelligence.dependency_discovery.aggregation_rule_inferer import AggregationRuleInferer
from app.intelligence.dependency_discovery.dependency_correlator import CorrelationInput, DependencyCorrelator
from app.intelligence.dependency_discovery.metric_semantic_analyzer import MetricSemanticAnalyzer, classify_metric_semantic_type
from app.intelligence.dependency_discovery.output_candidate_builder import OutputCandidateBuilder, parse_numeric_value
from app.intelligence.dependency_discovery.schemas import (
    DependencyContradiction,
    DependencyDescriptor,
    DependencyEvidence,
    DerivedOutputDescriptor,
    MetricDescriptor,
    ScopeDimension,
)
from app.intelligence.dependency_discovery.scope_dimension_analyzer import ScopeDimensionAnalyzer, scopes_compatible
from app.perception.models import (
    AlertDescriptor,
    CanonicalPageModel,
    HeadingDescriptor,
    ImageDescriptor,
    NavigationItem,
    NavigationRegion,
    NetworkEvidence,
    PageRegion,
    PaginationDescriptor,
    TabDescriptor,
    TabGroup,
    TextBlockDescriptor,
)
from app.schemas import FormDescriptor, FormFieldDescriptor, InteractiveElement, TableDescriptor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _model(url: str, fp: str = "fp1", **kwargs) -> CanonicalPageModel:
    return CanonicalPageModel(url=url, state_fingerprint=fp, **kwargs)


def _kpi_parts(region_id: str, label: str, value: str):
    region = PageRegion(stable_id=region_id, region_type="card_group")
    heading = HeadingDescriptor(stable_id=f"{region_id}_h", parent_region_id=region_id, text=label)
    value_block = TextBlockDescriptor(stable_id=f"{region_id}_v", parent_region_id=region_id, text=value, block_type="label")
    return region, heading, value_block


def _kpi_model(url: str, fp: str, region_id: str, label: str, value: str, **kwargs) -> CanonicalPageModel:
    region, heading, value_block = _kpi_parts(region_id, label, value)
    return _model(
        url, fp,
        regions=[region, *kwargs.pop("regions", [])],
        headings=[heading, *kwargs.pop("headings", [])],
        text_blocks=[value_block, *kwargs.pop("text_blocks", [])],
        **kwargs,
    )


def _entity(term: str, *, states: list[str] | None = None, status: str = "confirmed", confidence: float = 0.8, aliases: list[str] | None = None) -> EntityRecord:
    return EntityRecord(canonical_name=term, status=status, confidence=confidence, known_states=states or [], aliases=aliases or [])


def _workflow(
    registry: WorkflowRegistry, anchor: str, name: str, *,
    steps: list[WorkflowStep] | None = None,
    actor_participations: list[WorkflowActorParticipation] | None = None,
    entity_participations: list[WorkflowEntityParticipation] | None = None,
    transitions: list[WorkflowTransition] | None = None,
):
    wf = registry.memory.get_or_create(anchor, canonical_name=name)
    wf.steps.extend(steps or [])
    wf.actors.extend(actor_participations or [])
    wf.entities.extend(entity_participations or [])
    wf.transitions.extend(transitions or [])
    return wf


def _step(verb: str, *, entity_id: str | None = None, actor_id: str | None = "current session", status: str = "observed") -> WorkflowStep:
    return WorkflowStep(semantic_action=verb, entity_id=entity_id, actor_id=actor_id, status=status, page_id="p", page_state_fingerprint="f")


def _observe(engine: DependencyDiscoveryEngine, before, after, **kwargs):
    return engine.observe(
        before_model=before, after_model=after,
        executed_element_id=kwargs.pop("element_id", None), action_succeeded=kwargs.pop("succeeded", True),
        iteration=kwargs.pop("iteration", 1), current_actor_term=kwargs.pop("actor", "current session"), **kwargs,
    )


# ---------------------------------------------------------------------------
# A. Output discovery
# ---------------------------------------------------------------------------


class TestOutputDiscovery:
    def test_kpi_card_discovered(self):
        model = _kpi_model("https://x.example/dash", "fp1", "r1", "Open Jobs", "5")
        outputs = OutputCandidateBuilder().build(model)
        assert any(o.output_type == "kpi_card" and o.parsed_value == 5.0 for o in outputs)

    def test_badge_discovered(self):
        el = InteractiveElement(element_id="b1", tag="button", accessible_name="Warnings (3)", text="Warnings (3)", is_visible=True, is_enabled=True)
        model = _model("https://x.example/dash", interactive_elements=[el])
        outputs = OutputCandidateBuilder().build(model)
        assert any(o.output_type == "notification_count" for o in outputs)

    def test_queue_count_discovered(self):
        el = InteractiveElement(element_id="q1", tag="button", accessible_name="Unassigned (7)", text="Unassigned (7)", is_visible=True, is_enabled=True)
        model = _model("https://x.example/dash", interactive_elements=[el])
        outputs = OutputCandidateBuilder().build(model)
        assert any(o.output_type == "queue_count" and o.is_aggregate for o in outputs)

    def test_chart_total_discovered(self):
        img = ImageDescriptor(stable_id="i1", element_id="i1", visual_semantic_type="chart", alt_text="Jobs by status")
        model = _model("https://x.example/dash", images=[img])
        outputs = OutputCandidateBuilder().build(model)
        assert any(o.output_type == "chart_total" and o.is_aggregate for o in outputs)

    def test_report_summary_discovered(self):
        tb = TextBlockDescriptor(stable_id="t1", element_id="t1", text="42 jobs completed this month", block_type="summary")
        model = _model("https://x.example/reports", text_blocks=[tb])
        outputs = OutputCandidateBuilder().build(model)
        assert any(o.output_type == "report_summary" for o in outputs)

    def test_whole_page_text_block_rejected_as_summary(self):
        # Regression: a live ServiceFlow run had a text_block mis-tagged
        # block_type="summary" that actually held the ENTIRE page's chrome
        # (nav + header + footer concatenated, containing "5 customers, 4
        # jobs" among hundreds of unrelated words) — a genuine summary
        # figure is a short phrase, not several paragraphs of page dump.
        huge_text = "Dashboard Customers Jobs Settings " * 20 + "5 customers, 4 jobs"
        tb = TextBlockDescriptor(stable_id="t1", element_id="t1", text=huge_text, block_type="summary")
        model = _model("https://x.example/dash", text_blocks=[tb])
        outputs = OutputCandidateBuilder().build(model)
        assert not any(o.output_type == "report_summary" for o in outputs)

    def test_newline_fragmented_chrome_text_rejected_as_summary(self):
        # Regression: a live SauceDemo run had a SHORT (<200 char) but
        # newline-fragmented block_type="summary" text_block holding stacked
        # nav/footer labels ("Open Menu\nYour Cart\nCheckout\nTwitter\n...")
        # -- length alone didn't catch it; a genuine summary is prose, not
        # several stacked one-or-two-word lines.
        chrome_text = "Open Menu\nYour Cart\nCheckout\nTwitter\nFacebook\n3 items"
        tb = TextBlockDescriptor(stable_id="t1", element_id="t1", text=chrome_text, block_type="summary")
        model = _model("https://x.example/cart", text_blocks=[tb])
        outputs = OutputCandidateBuilder().build(model)
        assert not any(o.output_type == "report_summary" for o in outputs)

    def test_table_total_discovered(self):
        table = TableDescriptor(table_id="t1", headers=["Name", "Amount"], row_count=3, sample_rows=[["Total", "150"]])
        model = _model("https://x.example/list", tables=[table])
        outputs = OutputCandidateBuilder().build(model)
        assert any(o.output_type == "table_total" and o.parsed_value == 150.0 for o in outputs)

    def test_notification_count_discovered(self):
        alert = AlertDescriptor(stable_id="a1", element_id="a1", text="3 new alerts", severity="warning")
        model = _model("https://x.example/dash", alerts=[alert])
        outputs = OutputCandidateBuilder().build(model)
        assert any(o.output_type == "alert_count" for o in outputs)

    def test_static_number_rejected_version(self):
        assert parse_numeric_value("v2.4") == (None, None, None)

    def test_date_rejected(self):
        assert parse_numeric_value("2026-07-28") == (None, None, None)

    def test_stepper_number_rejected(self):
        tab_group = TabGroup(stable_id="tg1", tabs=[TabDescriptor(stable_id="t0", element_id="t0", text="1"), TabDescriptor(stable_id="t1", element_id="t1", text="2")])
        el = InteractiveElement(element_id="t0", tag="button", accessible_name="1", text="1", is_visible=True, is_enabled=True)
        model = _model("https://x.example/wizard", tabs=[tab_group], interactive_elements=[el])
        outputs = OutputCandidateBuilder().build(model)
        assert not any(o.canonical_label == "1" for o in outputs)

    def test_identifier_rejected(self):
        assert parse_numeric_value("CUST-001") == (None, None, None)


# ---------------------------------------------------------------------------
# B. Entity dependencies
# ---------------------------------------------------------------------------


class TestEntityDependencies:
    def test_output_label_matches_entity(self):
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job", states=["open"])
        output = DerivedOutputDescriptor(canonical_label="Open Jobs", output_type="kpi_card", raw_value="5", parsed_value=5.0)
        metric = MetricSemanticAnalyzer().analyze(output, entity_registry=er)
        assert "job" in metric.candidate_entity_terms

    def test_api_resource_matches_entity(self):
        engine = DependencyDiscoveryEngine()
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        net = NetworkEvidence(stable_id="n1", method="GET", text="/api/jobs/summary")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Jobs", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Jobs", "5", network_evidence=[net])
        summary = _observe(engine, before, after, entity_registry=er)
        assert any(d["source_entity_ids"] == ["job"] for d in summary["dependencies_touched"])

    def test_table_and_kpi_share_entity(self):
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        o1 = DerivedOutputDescriptor(canonical_label="Job Total", output_type="table_total", raw_value="10", parsed_value=10.0)
        o2 = DerivedOutputDescriptor(canonical_label="Job Count", output_type="kpi_card", raw_value="10", parsed_value=10.0)
        m1 = MetricSemanticAnalyzer().analyze(o1, entity_registry=er)
        m2 = MetricSemanticAnalyzer().analyze(o2, entity_registry=er)
        assert "job" in m1.candidate_entity_terms and "job" in m2.candidate_entity_terms

    def test_ambiguous_entity_candidates(self):
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        er.memory.records["jobsite"] = _entity("jobsite", aliases=["job"])
        output = DerivedOutputDescriptor(canonical_label="Job Total", output_type="kpi_card", raw_value="4", parsed_value=4.0)
        metric = MetricSemanticAnalyzer().analyze(output, entity_registry=er)
        assert len(metric.candidate_entity_terms) >= 2

    def test_no_matching_entity(self):
        er = EntityRegistry()
        er.memory.records["customer"] = _entity("customer")
        output = DerivedOutputDescriptor(canonical_label="Weather Forecast", output_type="kpi_card", raw_value="4", parsed_value=4.0)
        metric = MetricSemanticAnalyzer().analyze(output, entity_registry=er)
        assert metric.candidate_entity_terms == []

    def test_aliases_merge_correctly(self):
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job", aliases=["work order"])
        output = DerivedOutputDescriptor(canonical_label="Work Orders Open", output_type="kpi_card", raw_value="3", parsed_value=3.0)
        metric = MetricSemanticAnalyzer().analyze(output, entity_registry=er)
        assert "job" in metric.candidate_entity_terms


# ---------------------------------------------------------------------------
# C. Workflow dependencies
# ---------------------------------------------------------------------------


class TestWorkflowDependencies:
    def _setup(self, verb: str):
        engine = DependencyDiscoveryEngine()
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job", states=["open", "closed"])
        wr = WorkflowRegistry()
        _workflow(
            wr, "entity:job", "job workflow",
            steps=[_step(verb, entity_id="job")],
            actor_participations=[WorkflowActorParticipation(actor_id="current session", role_in_workflow="initiator")],
            entity_participations=[WorkflowEntityParticipation(entity_id="job")],
        )
        return engine, er, wr

    def test_create_workflow_increases_count_candidate(self):
        engine, er, wr = self._setup("create")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Open Jobs", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Open Jobs", "6")
        summary = _observe(engine, before, after, entity_registry=er, workflow_registry=wr)
        deps = [d for d in summary["dependencies_touched"] if "job" in d["source_entity_ids"]]
        assert any(d["effect_direction"] == "increase" for d in deps)

    def test_delete_workflow_decreases_count_candidate(self):
        engine, er, wr = self._setup("delete")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Open Jobs", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Open Jobs", "4")
        summary = _observe(engine, before, after, entity_registry=er, workflow_registry=wr)
        deps = [d for d in summary["dependencies_touched"] if "job" in d["source_entity_ids"]]
        assert any(d["effect_direction"] == "decrease" for d in deps)

    def test_state_transition_moves_between_categories(self):
        engine = DependencyDiscoveryEngine()
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job", states=["open", "closed"])
        wr = WorkflowRegistry()
        _workflow(
            wr, "entity:job", "job workflow",
            transitions=[WorkflowTransition(entity_id="job", source_state="open", target_state="closed", action_verb="complete", is_explicit=True)],
            entity_participations=[WorkflowEntityParticipation(entity_id="job")],
        )
        open_before = _kpi_model("https://x.example/dash", "fp1", "r1", "Open Jobs", "5")
        open_after = _kpi_model("https://x.example/dash", "fp2", "r1", "Open Jobs", "4")
        _observe(engine, open_before, open_after, entity_registry=er, workflow_registry=wr)
        state_deps = [d for d in engine.registry.all_dependencies() if d.relationship_type == "filters_by_state" and any(s.state_label.lower() == "open" for s in d.inclusion_rules)]
        assert any(d.effect_direction == "decrease" for d in state_deps)

    def test_assignment_moves_queue(self):
        engine, er, wr = self._setup("assign")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Open Jobs", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Open Jobs", "5")
        summary = _observe(engine, before, after, entity_registry=er, workflow_registry=wr)
        deps = [d for d in summary["dependencies_touched"] if "job" in d["source_entity_ids"]]
        assert any(d["effect_direction"] == "move_between_categories" for d in deps)

    def test_approval_changes_pending_and_approved_categories(self):
        engine, er, wr = self._setup("approve")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Open Jobs", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Open Jobs", "5")
        summary = _observe(engine, before, after, entity_registry=er, workflow_registry=wr)
        deps = [d for d in summary["dependencies_touched"] if "job" in d["source_entity_ids"]]
        assert any(d["effect_direction"] == "move_between_categories" for d in deps)

    def test_cancellation_changes_active_total(self):
        engine, er, wr = self._setup("cancel")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Open Jobs", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Open Jobs", "5")
        summary = _observe(engine, before, after, entity_registry=er, workflow_registry=wr)
        deps = [d for d in summary["dependencies_touched"] if "job" in d["source_entity_ids"]]
        assert any(d["effect_direction"] == "move_between_categories" for d in deps)

    def test_workflow_with_unknown_output(self):
        engine = DependencyDiscoveryEngine()
        wr = WorkflowRegistry()
        _workflow(wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job")])
        before = _model("https://x.example/dash", "fp1")
        after = _model("https://x.example/dash", "fp2")
        summary = _observe(engine, before, after, workflow_registry=wr)
        assert summary["dependencies_touched"] == []


# ---------------------------------------------------------------------------
# D. Actor dependencies
# ---------------------------------------------------------------------------


class TestActorDependencies:
    def test_producer_and_consumer_same_actor(self):
        engine = DependencyDiscoveryEngine()
        wr = WorkflowRegistry()
        _workflow(
            wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job")],
            actor_participations=[WorkflowActorParticipation(actor_id="current session", role_in_workflow="initiator")],
            entity_participations=[WorkflowEntityParticipation(entity_id="job")],
        )
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Job Total", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Job Total", "6")
        _observe(engine, before, after, actor="current session", entity_registry=er, workflow_registry=wr)
        deps = [d for d in engine.registry.all_dependencies() if "job" in d.source_entity_ids]
        assert all(not d.is_cross_role() for d in deps)

    def test_cross_role_producer_and_consumer(self):
        engine = DependencyDiscoveryEngine()
        wr = WorkflowRegistry()
        _workflow(
            wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", actor_id="alice")],
            actor_participations=[WorkflowActorParticipation(actor_id="alice", role_in_workflow="initiator")],
            entity_participations=[WorkflowEntityParticipation(entity_id="job")],
        )
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Job Total", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Job Total", "6")
        _observe(engine, before, after, actor="bob", entity_registry=er, workflow_registry=wr)
        deps = [d for d in engine.registry.all_dependencies() if "job" in d.source_entity_ids]
        assert any(d.is_cross_role() and d.verification_requirements for d in deps)

    def test_processing_actor_between_producer_and_consumer(self):
        engine = DependencyDiscoveryEngine()
        wr = WorkflowRegistry()
        _workflow(
            wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job", actor_id="alice")],
            actor_participations=[
                WorkflowActorParticipation(actor_id="alice", role_in_workflow="initiator"),
                WorkflowActorParticipation(actor_id="carol", role_in_workflow="approver"),
            ],
            entity_participations=[WorkflowEntityParticipation(entity_id="job")],
        )
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Job Total", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Job Total", "6")
        _observe(engine, before, after, actor="bob", entity_registry=er, workflow_registry=wr)
        dep = next(d for d in engine.registry.all_dependencies() if "job" in d.source_entity_ids)
        step_types = [r.step_type for r in dep.verification_requirements]
        assert step_types.count("login_as_actor") >= 2

    def test_unresolved_verification_actor(self):
        engine = DependencyDiscoveryEngine()
        wr = WorkflowRegistry()
        _workflow(
            wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job")],
            entity_participations=[WorkflowEntityParticipation(entity_id="job")],
        )
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Job Total", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Job Total", "6")
        _observe(engine, before, after, entity_registry=er, workflow_registry=wr)
        dep = next(d for d in engine.registry.all_dependencies() if "job" in d.source_entity_ids)
        assert dep.requires_actor_switch()

    def test_single_role_application_no_cross_role_dependencies(self):
        engine = DependencyDiscoveryEngine()
        wr = WorkflowRegistry()
        _workflow(
            wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job")],
            actor_participations=[WorkflowActorParticipation(actor_id="current session", role_in_workflow="initiator")],
            entity_participations=[WorkflowEntityParticipation(entity_id="job")],
        )
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Job Total", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Job Total", "6")
        _observe(engine, before, after, entity_registry=er, workflow_registry=wr)
        assert engine.registry.cross_role_dependencies() == []


# ---------------------------------------------------------------------------
# E. Aggregation
# ---------------------------------------------------------------------------


class TestAggregation:
    def test_count_all_rule(self):
        output = DerivedOutputDescriptor(canonical_label="Total Jobs", output_type="kpi_card", raw_value="12", parsed_value=12.0, is_aggregate=True)
        metric = MetricDescriptor(output_id=output.output_id, metric_semantic_type="total")
        rules = AggregationRuleInferer().infer_rules(output, metric)
        assert any(r.rule_type == "count_all" for r in rules)

    def test_filtered_count_rule(self):
        output = DerivedOutputDescriptor(canonical_label="Open Jobs", output_type="kpi_card", raw_value="5", parsed_value=5.0)
        metric = MetricDescriptor(output_id=output.output_id, metric_semantic_type="count", candidate_state_terms=["open"])
        rules = AggregationRuleInferer().infer_rules(output, metric)
        assert any(r.rule_type == "count_in_state" and r.group_by_term == "open" for r in rules)

    def test_state_count_rule(self):
        output = DerivedOutputDescriptor(canonical_label="Closed Jobs", output_type="kpi_card", raw_value="9", parsed_value=9.0)
        metric = MetricDescriptor(output_id=output.output_id, metric_semantic_type="backlog", candidate_state_terms=["closed"])
        rules = AggregationRuleInferer().infer_rules(output, metric)
        assert rules[0].rule_type == "count_in_state"

    def test_sum_rule(self):
        output = DerivedOutputDescriptor(canonical_label="Total Revenue", output_type="kpi_card", raw_value="$500", parsed_value=500.0, unit="currency")
        metric = MetricDescriptor(output_id=output.output_id, metric_semantic_type="revenue_like")
        rules = AggregationRuleInferer().infer_rules(output, metric)
        assert any(r.rule_type == "sum_field" for r in rules)

    def test_average_rule(self):
        output = DerivedOutputDescriptor(canonical_label="Average Resolution Time", output_type="kpi_card", raw_value="4", parsed_value=4.0)
        metric = MetricDescriptor(output_id=output.output_id, metric_semantic_type="average")
        rules = AggregationRuleInferer().infer_rules(output, metric)
        assert any(r.rule_type == "average_field" for r in rules)

    def test_percentage_rule(self):
        output = DerivedOutputDescriptor(canonical_label="Completion Rate", output_type="percentage", raw_value="80%", parsed_value=80.0, unit="percent")
        metric = MetricDescriptor(output_id=output.output_id, metric_semantic_type="percentage")
        rules = AggregationRuleInferer().infer_rules(output, metric)
        assert any(r.rule_type == "percentage_of_total" for r in rules)

    def test_grouped_chart_rule(self):
        output = DerivedOutputDescriptor(canonical_label="Jobs by Status", output_type="chart_segment", raw_value="", is_aggregate=True)
        metric = MetricDescriptor(output_id=output.output_id, metric_semantic_type="status_distribution")
        rules = AggregationRuleInferer().infer_rules(output, metric)
        assert any(r.rule_type == "group_by_state" for r in rules)

    def test_ambiguous_formula_low_confidence(self):
        output = DerivedOutputDescriptor(canonical_label="Mystery Widget", output_type="unknown", raw_value="3", parsed_value=3.0)
        metric = MetricDescriptor(output_id=output.output_id, metric_semantic_type="unknown")
        rules = AggregationRuleInferer().infer_rules(output, metric)
        assert rules[0].rule_type == "unknown" and rules[0].confidence < 0.2

    def test_competing_formulas_stored(self):
        output = DerivedOutputDescriptor(canonical_label="Open and Closed Jobs", output_type="kpi_card", raw_value="5", parsed_value=5.0)
        metric = MetricDescriptor(output_id=output.output_id, metric_semantic_type="count", candidate_state_terms=["open", "closed"])
        rules = AggregationRuleInferer().infer_rules(output, metric)
        assert len(rules) >= 2


# ---------------------------------------------------------------------------
# F. Scope
# ---------------------------------------------------------------------------


class TestScope:
    def test_date_filter_scope_detected(self):
        model = _model("https://x.example/dash", headings=[HeadingDescriptor(stable_id="h1", text="This Month's Activity")])
        output = DerivedOutputDescriptor(canonical_label="Activity", output_type="kpi_card", raw_value="5")
        dims, temporal, _, _, _ = ScopeDimensionAnalyzer().analyze(model, output)
        assert temporal is not None and temporal.is_relative

    def test_tenant_scope_detected(self):
        from app.perception.models import BreadcrumbDescriptor
        model = _model("https://x.example/dash", breadcrumbs=[BreadcrumbDescriptor(stable_id="b1", text="Acme Organisation")])
        output = DerivedOutputDescriptor(canonical_label="Jobs", output_type="kpi_card", raw_value="5")
        dims, _, _, tenant, _ = ScopeDimensionAnalyzer().analyze(model, output)
        assert tenant is not None

    def test_actor_scope_detected(self):
        model = _model("https://x.example/dash")
        output = DerivedOutputDescriptor(canonical_label="Jobs", output_type="kpi_card", raw_value="5", current_actor_term="alice")
        dims, _, actor_scope, _, _ = ScopeDimensionAnalyzer().analyze(model, output)
        assert actor_scope is not None and actor_scope.actor_term == "alice"

    def test_selected_tab_scope_detected(self):
        tab_group = TabGroup(stable_id="tg1", selected_tab_id="t1", tabs=[TabDescriptor(stable_id="t1", element_id="t1", text="Active")])
        model = _model("https://x.example/dash", tabs=[tab_group])
        output = DerivedOutputDescriptor(canonical_label="Jobs", output_type="kpi_card", raw_value="5")
        dims, _, _, _, _ = ScopeDimensionAnalyzer().analyze(model, output)
        assert any(d.dimension_type == "selected_tab" and d.value == "Active" for d in dims)

    def test_status_filter_scope_detected(self):
        field_ = FormFieldDescriptor(stable_id="f1", label="Status Filter", current_value="Open")
        form = FormDescriptor(form_id="f1", field_descriptors=[field_])
        model = _model("https://x.example/dash", forms=[form])
        output = DerivedOutputDescriptor(canonical_label="Jobs", output_type="kpi_card", raw_value="5")
        dims, _, _, _, filter_scope = ScopeDimensionAnalyzer().analyze(model, output)
        assert filter_scope is not None and filter_scope.filter_value == "Open"

    def test_incompatible_observations_not_compared(self):
        a = [ScopeDimension(dimension_type="actor", value="alice")]
        b = [ScopeDimension(dimension_type="actor", value="bob")]
        assert scopes_compatible(a, b) is False

    def test_compatible_observations_correlated(self):
        a = [ScopeDimension(dimension_type="actor", value="alice"), ScopeDimension(dimension_type="selected_tab", value="Active")]
        b = [ScopeDimension(dimension_type="actor", value="alice")]
        assert scopes_compatible(a, b) is True


# ---------------------------------------------------------------------------
# G. Before-and-after correlation
# ---------------------------------------------------------------------------


class TestBeforeAfterCorrelation:
    def test_expected_increase_supported(self):
        obs = DependencyCorrelator().correlate(CorrelationInput(predicted_direction="increase", action_succeeded=True, before_value=5, after_value=6))
        assert obs.result == "supported"

    def test_expected_decrease_supported(self):
        obs = DependencyCorrelator().correlate(CorrelationInput(predicted_direction="decrease", action_succeeded=True, before_value=5, after_value=4))
        assert obs.result == "supported"

    def test_expected_category_movement_supported(self):
        obs = DependencyCorrelator().correlate(CorrelationInput(predicted_direction="move_between_categories", action_succeeded=True, before_value=5, after_value=4))
        assert obs.result == "supported"

    def test_no_change_unsupported(self):
        obs = DependencyCorrelator().correlate(CorrelationInput(predicted_direction="increase", action_succeeded=True, before_value=5, after_value=5))
        assert obs.result == "unsupported"

    def test_wrong_magnitude_still_supported_direction_only(self):
        # Direction-only claims: any increase counts as supported regardless
        # of magnitude — the engine never claims to predict a precise delta.
        obs = DependencyCorrelator().correlate(CorrelationInput(predicted_direction="increase", action_succeeded=True, before_value=5, after_value=50))
        assert obs.result == "supported"

    def test_output_changes_without_known_source_is_not_correlated(self):
        # No predicted direction -> nothing to compare against -> inconclusive.
        obs = DependencyCorrelator().correlate(CorrelationInput(predicted_direction="unknown", action_succeeded=True, before_value=5, after_value=9))
        assert obs.result == "inconclusive"

    def test_source_action_failed_is_inconclusive(self):
        obs = DependencyCorrelator().correlate(CorrelationInput(predicted_direction="increase", action_succeeded=False, before_value=5, after_value=6))
        assert obs.result == "inconclusive"

    def test_incompatible_scope_is_not_compared(self):
        obs = DependencyCorrelator().correlate(
            CorrelationInput(
                predicted_direction="increase", action_succeeded=True, before_value=5, after_value=6,
                before_scope=[ScopeDimension(dimension_type="actor", value="alice")],
                after_scope=[ScopeDimension(dimension_type="actor", value="bob")],
            )
        )
        assert obs.result == "scope_incompatible" and obs.scope_compatible is False

    def test_contradicted_direction(self):
        obs = DependencyCorrelator().correlate(CorrelationInput(predicted_direction="increase", action_succeeded=True, before_value=5, after_value=3))
        assert obs.result == "contradicted"

    def test_delayed_change_detected_on_second_observation(self):
        engine = DependencyDiscoveryEngine()
        wr = WorkflowRegistry()
        _workflow(
            wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job")],
            actor_participations=[WorkflowActorParticipation(actor_id="current session", role_in_workflow="initiator")],
            entity_participations=[WorkflowEntityParticipation(entity_id="job")],
        )
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        # Round 1: value doesn't move yet (stale cache) -> unsupported.
        before1 = _kpi_model("https://x.example/dash", "fp1", "r1", "Job Total", "5")
        after1 = _kpi_model("https://x.example/dash", "fp2", "r1", "Job Total", "5")
        _observe(engine, before1, after1, iteration=1, entity_registry=er, workflow_registry=wr)
        # Round 2: same anchor, value now caught up -> should reclassify as delayed.
        before2 = _kpi_model("https://x.example/dash", "fp2", "r1", "Job Total", "5")
        after2 = _kpi_model("https://x.example/dash", "fp3", "r1", "Job Total", "6")
        _observe(engine, before2, after2, iteration=2, entity_registry=er, workflow_registry=wr)
        dep = next(d for d in engine.registry.all_dependencies() if "job" in d.source_entity_ids)
        assert dep.status == "verified"


# ---------------------------------------------------------------------------
# H. Registry and merging
# ---------------------------------------------------------------------------


class TestRegistryAndMerging:
    def test_duplicate_kpi_across_pages_merges(self):
        engine = DependencyDiscoveryEngine()
        m1 = _kpi_model("https://x.example/dash", "fp1", "r1", "Open Jobs", "5")
        m2 = _kpi_model("https://x.example/reports", "fp2", "r1", "Open Jobs", "5")
        _observe(engine, m1, m1)
        _observe(engine, m2, m2)
        outputs = [o for o in engine.registry.all_outputs() if o.canonical_label == "Open Jobs"]
        assert len(outputs) == 1
        assert outputs[0].observation_count >= 2

    def test_same_metric_across_actors_merges(self):
        engine = DependencyDiscoveryEngine()
        m1 = _kpi_model("https://x.example/dash", "fp1", "r1", "Open Jobs", "5")
        m2 = _kpi_model("https://x.example/dash", "fp2", "r1", "Open Jobs", "5")
        _observe(engine, m1, m1, actor="alice")
        _observe(engine, m2, m2, actor="bob")
        outputs = [o for o in engine.registry.all_outputs() if o.canonical_label == "Open Jobs"]
        assert len(outputs) == 1

    def test_same_dependency_across_sessions_merges(self):
        engine = DependencyDiscoveryEngine()
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        m1 = _kpi_model("https://x.example/dash", "fp1", "r1", "Job Total", "5")
        m2 = _kpi_model("https://x.example/dash", "fp2", "r1", "Job Total", "5")
        _observe(engine, m1, m1, entity_registry=er)
        _observe(engine, m2, m2, entity_registry=er)
        deps = [d for d in engine.registry.all_dependencies() if "job" in d.source_entity_ids]
        assert len(deps) == 1
        assert deps[0].observation_count >= 2

    def test_repeated_evidence_deduped(self):
        engine = DependencyDiscoveryEngine()
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        m = _kpi_model("https://x.example/dash", "fp1", "r1", "Job Total", "5")
        _observe(engine, m, m, entity_registry=er)
        _observe(engine, m, m, entity_registry=er)
        dep = next(d for d in engine.registry.all_dependencies() if "job" in d.source_entity_ids)
        exact = [(e.source_kind, e.observed_text, e.page_url) for e in dep.supporting_evidence]
        assert len(exact) == len(set(exact))

    def test_contradiction_merging_retained(self):
        engine = DependencyDiscoveryEngine()
        wr = WorkflowRegistry()
        _workflow(
            wr, "entity:job", "job workflow", steps=[_step("create", entity_id="job")],
            actor_participations=[WorkflowActorParticipation(actor_id="current session", role_in_workflow="initiator")],
            entity_participations=[WorkflowEntityParticipation(entity_id="job")],
        )
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        before = _kpi_model("https://x.example/dash", "fp1", "r1", "Job Total", "5")
        after = _kpi_model("https://x.example/dash", "fp2", "r1", "Job Total", "3")  # opposite of predicted increase
        _observe(engine, before, after, entity_registry=er, workflow_registry=wr)
        dep = next(d for d in engine.registry.all_dependencies() if "job" in d.source_entity_ids)
        assert dep.contradictions

    def test_stable_dependency_ids_across_observations(self):
        engine = DependencyDiscoveryEngine()
        er = EntityRegistry()
        er.memory.records["job"] = _entity("job")
        m = _kpi_model("https://x.example/dash", "fp1", "r1", "Job Total", "5")
        _observe(engine, m, m, entity_registry=er)
        dep_id_1 = next(d.dependency_id for d in engine.registry.all_dependencies() if "job" in d.source_entity_ids)
        _observe(engine, m, m, entity_registry=er)
        dep_id_2 = next(d.dependency_id for d in engine.registry.all_dependencies() if "job" in d.source_entity_ids)
        assert dep_id_1 == dep_id_2


# ---------------------------------------------------------------------------
# I. Confidence
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_correlated_change_outranks_label_match(self):
        label_only = DependencyDescriptor(canonical_name="label only", target_output_ids=["o1"], source_entity_ids=["job"])
        label_only.supporting_evidence = [dependency_confidence.DependencyEvidence(source_kind="label_match", observed_text="x")]
        composite_label, *_ = dependency_confidence.score_confidence(label_only)

        correlated = DependencyDescriptor(canonical_name="correlated", target_output_ids=["o1"], source_entity_ids=["job"], status="verified")
        composite_correlated, *_ = dependency_confidence.score_confidence(correlated)
        assert composite_correlated > composite_label

    def test_contradictory_evidence_lowers_confidence(self):
        clean = DependencyDescriptor(canonical_name="clean", target_output_ids=["o1"])
        contradicted = DependencyDescriptor(canonical_name="contradicted", target_output_ids=["o1"])
        contradicted.contradictions.append(DependencyContradiction(description="x"))
        composite_clean, *_ = dependency_confidence.score_confidence(clean)
        composite_contra, *_ = dependency_confidence.score_confidence(contradicted)
        assert composite_contra <= composite_clean

    def test_duplicate_evidence_does_not_inflate_endlessly(self):
        dep = DependencyDescriptor(canonical_name="dep", target_output_ids=["o1"])
        for _ in range(20):
            dep.supporting_evidence.append(
                dependency_confidence.DependencyEvidence(source_kind="entity_registry", observed_text="same", page_url="https://x.example/p")
            )
        score = dependency_confidence.relationship_confidence(dep)
        assert score <= 1.0

    def test_inferred_remains_below_verified(self):
        inferred = DependencyDescriptor(canonical_name="inferred", target_output_ids=["o1"], status="inferred")
        verified = DependencyDescriptor(canonical_name="verified", target_output_ids=["o1"], status="verified")
        c_inferred, *_ = dependency_confidence.score_confidence(inferred)
        c_verified, *_ = dependency_confidence.score_confidence(verified)
        assert c_verified > c_inferred

    def test_incompatible_scope_does_not_raise_confidence(self):
        obs = DependencyCorrelator().correlate(
            CorrelationInput(
                predicted_direction="increase", action_succeeded=True, before_value=5, after_value=10,
                before_scope=[ScopeDimension(dimension_type="tenant", value="acme")],
                after_scope=[ScopeDimension(dimension_type="tenant", value="globex")],
            )
        )
        assert obs.result == "scope_incompatible"


# ---------------------------------------------------------------------------
# J. Application neutrality
# ---------------------------------------------------------------------------


class TestApplicationNeutrality:
    def test_no_business_domain_vocabulary_in_package_code(self):
        import ast

        import app.intelligence.dependency_discovery as pkg

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
        assert not offenders, "business vocabulary hardcoded in dependency_discovery: " + "; ".join(offenders)

    def test_generic_fixture_logistics_application(self):
        engine = DependencyDiscoveryEngine()
        er = EntityRegistry()
        er.memory.records["shipment"] = _entity("shipment", states=["in transit", "delivered"])
        wr = WorkflowRegistry()
        _workflow(
            wr, "entity:shipment", "shipment workflow", steps=[_step("create", entity_id="shipment")],
            actor_participations=[WorkflowActorParticipation(actor_id="current session", role_in_workflow="initiator")],
            entity_participations=[WorkflowEntityParticipation(entity_id="shipment")],
        )
        before = _kpi_model("https://logistics.example/dash", "fp1", "r1", "In Transit Shipments", "10")
        after = _kpi_model("https://logistics.example/dash", "fp2", "r1", "In Transit Shipments", "11")
        summary = _observe(engine, before, after, entity_registry=er, workflow_registry=wr)
        assert any("shipment" in d["source_entity_ids"] for d in summary["dependencies_touched"])

    def test_generic_fixture_dashboard_application(self):
        engine = DependencyDiscoveryEngine()
        er = EntityRegistry()
        er.memory.records["widget"] = _entity("widget", states=["active", "archived"])
        before = _kpi_model("https://board.example/", "fp1", "r1", "Active Widgets", "8")
        after = _kpi_model("https://board.example/", "fp2", "r1", "Active Widgets", "8")
        summary = _observe(engine, before, after, entity_registry=er)
        assert any("widget" in d["source_entity_ids"] for d in summary["dependencies_touched"])

    def test_unknown_output_types_preserved(self):
        output = DerivedOutputDescriptor(canonical_label="Something Unusual", output_type="unknown", raw_value="7", parsed_value=7.0)
        metric_type = classify_metric_semantic_type(output)
        assert metric_type == "unknown"
