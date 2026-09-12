"""Aggregation rule inference — CANDIDATE explanations for how an output's
value is computed. Never asserts a precise formula unless the evidence
actually supports it; stores multiple competing rules when genuinely
ambiguous (task section 8's explicit instruction), rather than collapsing
to a single guess.
"""

from __future__ import annotations

from app.intelligence.dependency_discovery.schemas import (
    AggregationRule,
    DependencyEvidence,
    DerivedOutputDescriptor,
    MetricDescriptor,
    ScopeDimension,
    StateExclusionRule,
    StateInclusionRule,
)

_COUNT_LIKE_TYPES = {"count", "total", "backlog", "alert", "activity", "capacity", "completion", "failure", "warning"}


def _evidence(text: str, *, page_url: str, fingerprint: str) -> DependencyEvidence:
    return DependencyEvidence(source_kind="label_match", observed_text=text[:160], page_url=page_url, state_fingerprint=fingerprint)


class AggregationRuleInferer:
    def infer_rules(
        self,
        output: DerivedOutputDescriptor,
        metric: MetricDescriptor,
        *,
        scope_dimensions: list[ScopeDimension] | None = None,
    ) -> list[AggregationRule]:
        rules: list[AggregationRule] = []
        scope_dimensions = scope_dimensions or []
        ev = _evidence(f"{output.canonical_label}: {output.raw_value}", page_url=output.page_url, fingerprint=output.state_fingerprint)

        if metric.metric_semantic_type == "percentage":
            rules.append(AggregationRule(rule_type="percentage_of_total", confidence=0.3, evidence=[ev]))
        elif metric.metric_semantic_type == "average":
            rules.append(AggregationRule(rule_type="average_field", confidence=0.3, evidence=[ev]))
        elif metric.metric_semantic_type == "ratio":
            rules.append(AggregationRule(rule_type="ratio_between_categories", confidence=0.25, evidence=[ev]))
        elif metric.metric_semantic_type == "status_distribution" or output.output_type == "chart_segment":
            rules.append(AggregationRule(rule_type="group_by_state", confidence=0.25, evidence=[ev]))
        elif metric.metric_semantic_type == "revenue_like" and output.unit == "currency":
            rules.append(AggregationRule(rule_type="sum_field", confidence=0.25, evidence=[ev]))
        elif metric.metric_semantic_type in _COUNT_LIKE_TYPES:
            if metric.candidate_state_terms:
                for state in metric.candidate_state_terms:
                    rules.append(AggregationRule(rule_type="count_in_state", group_by_term=state, confidence=0.3, evidence=[ev]))
            else:
                rules.append(AggregationRule(rule_type="count_all", confidence=0.2, evidence=[ev]))

        if any(d.dimension_type == "actor" for d in scope_dimensions):
            rules.append(AggregationRule(rule_type="count_by_actor", confidence=0.15, evidence=[ev]))
        if any(d.dimension_type == "date_range" for d in scope_dimensions):
            rules.append(AggregationRule(rule_type="count_in_time_range", confidence=0.15, evidence=[ev]))

        if not rules:
            rules.append(AggregationRule(rule_type="unknown", confidence=0.05, evidence=[ev]))

        # Dedupe exact (rule_type, group_by_term) repeats -> never store the
        # same candidate twice, but genuinely DIFFERENT rule_types for the
        # same output are kept side by side as competing explanations.
        seen: set[tuple[str, str | None]] = set()
        deduped: list[AggregationRule] = []
        for rule in rules:
            key = (rule.rule_type, rule.group_by_term)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(rule)
        return deduped

    def infer_state_rules(
        self, output: DerivedOutputDescriptor, metric: MetricDescriptor, *, entity_registry=None
    ) -> tuple[list[StateInclusionRule], list[StateExclusionRule]]:
        """Included states are the ones this output's label/context actually
        named. Excluded states are the SIBLING states of the same entity that
        were NOT named — a structural, never label-only, inference: a count
        of "open X" implicitly excludes whatever other states that entity is
        already known to have."""
        inclusions = [
            StateInclusionRule(
                state_label=state, confidence=0.3,
                evidence=[_evidence(f"'{output.canonical_label}' names state '{state}'", page_url=output.page_url, fingerprint=output.state_fingerprint)],
            )
            for state in metric.candidate_state_terms
        ]
        exclusions: list[StateExclusionRule] = []
        if entity_registry is not None and metric.candidate_entity_terms and metric.candidate_state_terms:
            named = {s.lower() for s in metric.candidate_state_terms}
            for entity_term in metric.candidate_entity_terms:
                record = entity_registry.get(entity_term)
                if record is None:
                    continue
                for state in record.known_states:
                    if state.lower() in named:
                        continue
                    exclusions.append(
                        StateExclusionRule(
                            state_label=state, confidence=0.2,
                            evidence=[_evidence(f"'{output.canonical_label}' does not name sibling state '{state}'", page_url=output.page_url, fingerprint=output.state_fingerprint)],
                        )
                    )
        return inclusions, exclusions
