"""Business Dependency Discovery Engine — the orchestrator.

Like Workflow Discovery, this needs a BEFORE/AFTER `CanonicalPageModel` pair
plus the executed action: whether a visible output actually moved the way a
structural effect-direction hypothesis predicted is meaningless without
knowing what changed and what caused it. `observe()` is called once per
executed action, from the same controller hook workflow discovery already
uses.

Deterministic, read-only over its inputs, never touches the browser, never
calls an LLM.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.intelligence.dependency_discovery import dependency_memory
from app.intelligence.dependency_discovery.aggregation_rule_inferer import AggregationRuleInferer
from app.intelligence.dependency_discovery.dependency_candidate_builder import DependencyCandidateBuilder
from app.intelligence.dependency_discovery.dependency_correlator import CorrelationInput, DependencyCorrelator
from app.intelligence.dependency_discovery.dependency_gap_analyzer import DependencyGapAnalyzer
from app.intelligence.dependency_discovery.dependency_registry import DependencyRegistry
from app.intelligence.dependency_discovery.dependency_relationship_builder import DependencyRelationshipBuilder
from app.intelligence.dependency_discovery.dependency_verification_planner import DependencyVerificationPlanner
from app.intelligence.dependency_discovery.metric_semantic_analyzer import MetricSemanticAnalyzer
from app.intelligence.dependency_discovery.output_candidate_builder import OutputCandidateBuilder
from app.intelligence.dependency_discovery.scope_dimension_analyzer import ScopeDimensionAnalyzer
from app.utils.exploration_trace import record as trace_record
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from app.intelligence.actor_discovery import ActorRegistry
    from app.intelligence.entity_discovery import EntityRegistry
    from app.intelligence.workflow_discovery import WorkflowRegistry
    from app.perception.models import CanonicalPageModel

logger = get_logger("intelligence.dependency_discovery")


class DependencyDiscoveryEngine:
    def __init__(self, registry: DependencyRegistry | None = None) -> None:
        self.registry = registry or DependencyRegistry()
        self.output_builder = OutputCandidateBuilder()
        self.metric_analyzer = MetricSemanticAnalyzer()
        self.candidate_builder = DependencyCandidateBuilder()
        self.relationship_builder = DependencyRelationshipBuilder()
        self.aggregation_inferer = AggregationRuleInferer()
        self.scope_analyzer = ScopeDimensionAnalyzer()
        self.correlator = DependencyCorrelator()
        self.gap_analyzer = DependencyGapAnalyzer()
        self.verification_planner = DependencyVerificationPlanner()
        # Runtime-only cache: last computed scope per output anchor, so a
        # cross-iteration correlation (the same KPI revisited later) can
        # still check scope compatibility even though `DerivedOutputDescriptor`
        # itself only stores scope dimension IDs, not full scope objects.
        self._last_scope_by_anchor: dict[str, list] = {}

    def observe(
        self,
        *,
        before_model: "CanonicalPageModel",
        after_model: "CanonicalPageModel",
        executed_element_id: str | None,
        action_succeeded: bool,
        iteration: int = 0,
        current_actor_term: str | None = None,
        tenant_term: str | None = None,
        entity_registry: "EntityRegistry | None" = None,
        actor_registry: "ActorRegistry | None" = None,
        workflow_registry: "WorkflowRegistry | None" = None,
        analyze_gaps: bool = True,
    ) -> dict[str, Any]:
        before_raw = self.output_builder.build(before_model, current_actor_term=current_actor_term, tenant_term=tenant_term)
        after_raw = self.output_builder.build(after_model, current_actor_term=current_actor_term, tenant_term=tenant_term)

        before_by_anchor = {dependency_memory.output_anchor(o.canonical_label, o.output_type): o for o in before_raw}
        merged_raw = {dependency_memory.output_anchor(o.canonical_label, o.output_type): o for o in before_raw}
        merged_raw.update({dependency_memory.output_anchor(o.canonical_label, o.output_type): o for o in after_raw})

        touched_dependency_ids: set[str] = set()
        touched_output_ids: set[str] = set()
        gaps_this_round: list = []

        for anchor, raw_output in merged_raw.items():
            same_iter_before = before_by_anchor.get(anchor)
            prior_registered = self.registry.memory.outputs.get(anchor)
            prior_value = prior_registered.parsed_value if prior_registered is not None else None
            prior_scope = self._last_scope_by_anchor.get(anchor, [])

            merged_output = self.registry.memory.get_or_create_output(raw_output)
            touched_output_ids.add(merged_output.output_id)

            metric = self.metric_analyzer.analyze(merged_output, entity_registry=entity_registry, workflow_registry=workflow_registry)
            candidates = self.candidate_builder.build(
                merged_output, metric, entity_registry=entity_registry, actor_registry=actor_registry,
                workflow_registry=workflow_registry, network_evidence=after_model.network_evidence,
            )
            scope_dims, temporal, actor_scope, tenant_scope, filter_scope = self.scope_analyzer.analyze(after_model, merged_output)
            agg_rules = self.aggregation_inferer.infer_rules(merged_output, metric, scope_dimensions=scope_dims)
            inclusions, exclusions = self.aggregation_inferer.infer_state_rules(merged_output, metric, entity_registry=entity_registry)

            if same_iter_before is not None:
                before_value = same_iter_before.parsed_value
                before_scope = self.scope_analyzer.analyze(before_model, same_iter_before)[0]
            else:
                before_value = prior_value
                before_scope = prior_scope

            for candidate in candidates:
                effect = self.relationship_builder.infer_effect(candidate, workflow_registry=workflow_registry)
                source_key = dependency_memory.source_key_for(
                    entity_id=candidate.source.entity_id, workflow_id=candidate.source.workflow_id,
                    actor_id=candidate.source.actor_id, state_label=candidate.source.state_label,
                )
                dep_anchor = dependency_memory.dependency_anchor(candidate.relationship_type, source_key, anchor)
                descriptor = self.registry.memory.get_or_create(
                    dep_anchor, canonical_name=f"{source_key} -> {merged_output.canonical_label}",
                    dependency_type=candidate.dependency_type, relationship_type=candidate.relationship_type,
                )
                touched_dependency_ids.add(descriptor.dependency_id)

                self.registry.memory.merge_sources(
                    descriptor, entity_id=candidate.source.entity_id, workflow_id=candidate.source.workflow_id, actor_id=candidate.source.actor_id
                )
                self.registry.memory.merge_target(descriptor, merged_output.output_id)
                self.registry.memory.merge_evidence(descriptor, [candidate.evidence, *metric.evidence])
                self.registry.memory.merge_effect_direction(descriptor, effect.direction)
                for rule in agg_rules:
                    self.registry.memory.merge_aggregation_rule(descriptor, rule)
                self.registry.memory.merge_state_rules(descriptor, inclusions, exclusions)
                self.registry.memory.merge_scope(descriptor, scope_dims, temporal, actor_scope, tenant_scope, filter_scope)

                cross_role = self.relationship_builder.build_cross_role_finding(
                    candidate, consumer_actor_term=current_actor_term, workflow_registry=workflow_registry, actor_registry=actor_registry
                )
                if cross_role is not None:
                    self.registry.memory.merge_cross_role(
                        descriptor, producer=cross_role.producer_actor_term, processor=cross_role.processor_actor_term,
                        consumer=cross_role.consumer_actor_term, unresolved=cross_role.unresolved_actor, workflow_term=cross_role.workflow_term,
                    )
                    plan = self.verification_planner.build_plan(descriptor, cross_role_requirements=cross_role.verification_requirements)
                else:
                    plan = self.verification_planner.build_plan(descriptor)
                self.registry.memory.merge_verification_requirements(descriptor, plan)

                verified_now = False
                if before_value is not None and merged_output.parsed_value is not None and effect.direction != "unknown":
                    ci = CorrelationInput(
                        predicted_direction=effect.direction, action_succeeded=action_succeeded,
                        before_scope=before_scope, after_scope=scope_dims,
                        before_value=before_value, after_value=merged_output.parsed_value, observed_at_iteration=iteration,
                    )
                    observation = self.correlator.correlate(ci)
                    verified_now = self.registry.memory.merge_execution_observation(descriptor, observation)

                already_verified = descriptor.status == "verified"
                self.registry.memory.finalize(descriptor, page_url=after_model.url, verified_observed=verified_now or already_verified)

            self._last_scope_by_anchor[anchor] = scope_dims

        if analyze_gaps and (entity_registry is not None or workflow_registry is not None):
            gaps_this_round = self.gap_analyzer.analyze(self.registry, entity_registry, workflow_registry)

        summary = self._summarize(after_model, touched_output_ids, touched_dependency_ids, gaps_this_round)
        self._trace(summary)
        return summary

    def _summarize(self, model, touched_output_ids, touched_dependency_ids, gaps) -> dict[str, Any]:
        touched_outputs = [o for o in self.registry.all_outputs() if o.output_id in touched_output_ids]
        touched_deps = [d for d in self.registry.all_dependencies() if d.dependency_id in touched_dependency_ids]
        return {
            "url": model.url,
            "state_fingerprint": model.state_fingerprint,
            "outputs_discovered": [
                {"output_id": o.output_id, "label": o.canonical_label, "type": o.output_type, "value": o.raw_value, "confidence": round(o.confidence, 3)}
                for o in touched_outputs
            ],
            "dependencies_touched": [d.to_summary_dict() for d in touched_deps],
            "gaps_generated": [g.gap_type for g in gaps],
            "registry_totals": {
                "total": len(self.registry.records),
                "known": len(self.registry.known_dependencies()),
                "high_confidence": len(self.registry.high_confidence_dependencies()),
                "cross_role": len(self.registry.cross_role_dependencies()),
                "verified": len(self.registry.dependencies_with_compatible_before_after_evidence()),
                "gap_count": len(self.registry.gaps),
            },
        }

    @staticmethod
    def _trace(summary: dict[str, Any]) -> None:
        trace_record("dependency_discovery.observation", **summary)
