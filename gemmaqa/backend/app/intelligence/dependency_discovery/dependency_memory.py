"""Dependency memory — persistence and merging of discovered outputs and
dependencies across observations.

Two anchor-keyed stores (mirrors `entity_memory`/`workflow_memory`'s
pattern), because a dependency's TARGET must itself be a stable, deduped
identity before the dependency can be:

1. `outputs`: anchored by `(normalized_label, output_type)` — the same KPI
   card seen on different pages/sessions/actors merges into ONE
   `DerivedOutputDescriptor`, never duplicated per-page.
2. `records`: anchored by `(relationship_type, source_key, output_anchor)`
   — the same source-to-target claim merges regardless of which
   observation surfaced it.
"""

from __future__ import annotations

from datetime import datetime

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term
from app.intelligence.dependency_discovery import dependency_confidence
from app.intelligence.dependency_discovery.schemas import (
    AggregationRule,
    DependencyContradiction,
    DependencyDescriptor,
    DependencyEvidence,
    DependencyExecutionObservation,
    DerivedOutputDescriptor,
    ScopeDimension,
    StateExclusionRule,
    StateInclusionRule,
    VerificationRequirement,
)

MAX_EVIDENCE_PER_DEPENDENCY = 80


def _dedupe_evidence(existing: list[DependencyEvidence], new: list[DependencyEvidence]) -> list[DependencyEvidence]:
    seen = {(e.source_kind, e.observed_text, e.page_url) for e in existing}
    merged = list(existing)
    for ev in new:
        key = (ev.source_kind, ev.observed_text, ev.page_url)
        if key in seen:
            continue
        seen.add(key)
        merged.append(ev)
    return merged[-MAX_EVIDENCE_PER_DEPENDENCY:]


def output_anchor(label: str, output_type: str) -> str:
    return f"output:{normalize_term(label)}:{output_type}"


def dependency_anchor(relationship_type: str, source_key: str, output_anchor_: str) -> str:
    return f"dep:{relationship_type}:{source_key}:{output_anchor_}"


def source_key_for(*, entity_id: str | None, workflow_id: str | None, actor_id: str | None, state_label: str | None) -> str:
    if state_label:
        return f"state:{normalize_term(state_label)}"
    if entity_id:
        return f"entity:{entity_id}"
    if workflow_id:
        return f"workflow:{workflow_id}"
    if actor_id:
        return f"actor:{actor_id}"
    return "unknown"


class DependencyMemory:
    def __init__(self) -> None:
        self.outputs: dict[str, DerivedOutputDescriptor] = {}
        self.records: dict[str, DependencyDescriptor] = {}
        # Last correlation result per dependency — used only to detect a
        # DELAYED update: a predicted change that didn't show up immediately
        # ("unsupported") but DID show up on a later re-check with the same
        # expected direction. Never persisted beyond the process; a fresh
        # engine naturally has no delay history, which just means it can't
        # yet tell a delay apart from "still no change".
        self._last_observation: dict[str, DependencyExecutionObservation] = {}

    # -- outputs --------------------------------------------------------------

    def get_or_create_output(self, candidate: DerivedOutputDescriptor, *, now: datetime | None = None) -> DerivedOutputDescriptor:
        now = now or datetime.utcnow()
        anchor = output_anchor(candidate.canonical_label, candidate.output_type)
        existing = self.outputs.get(anchor)
        if existing is None:
            candidate.first_seen = now
            candidate.last_seen = now
            self.outputs[anchor] = candidate
            return candidate

        existing.raw_value = candidate.raw_value or existing.raw_value
        existing.parsed_value = candidate.parsed_value if candidate.parsed_value is not None else existing.parsed_value
        existing.unit = candidate.unit or existing.unit
        existing.format = candidate.format or existing.format
        existing.page_url = candidate.page_url or existing.page_url
        existing.state_fingerprint = candidate.state_fingerprint or existing.state_fingerprint
        existing.evidence = _dedupe_evidence(existing.evidence, candidate.evidence)
        existing.nearby_context = list({*existing.nearby_context, *candidate.nearby_context})[:10]
        if candidate.current_actor_term and candidate.current_actor_term not in existing.aliases:
            pass  # actor identity isn't an alias of the output; tracked separately if needed
        existing.is_aggregate = existing.is_aggregate or candidate.is_aggregate
        existing.observation_count += 1
        existing.last_seen = now
        existing.confidence = min(1.0, existing.confidence + 0.05 * min(4, existing.observation_count - 1))
        return existing

    def output_by_id(self, output_id: str) -> DerivedOutputDescriptor | None:
        for out in self.outputs.values():
            if out.output_id == output_id:
                return out
        return None

    # -- dependencies -----------------------------------------------------------

    def get_or_create(
        self, anchor: str, *, canonical_name: str, dependency_type: str, relationship_type: str, now: datetime | None = None
    ) -> DependencyDescriptor:
        now = now or datetime.utcnow()
        existing = self.records.get(anchor)
        if existing is not None:
            return existing
        descriptor = DependencyDescriptor(
            canonical_name=canonical_name, dependency_type=dependency_type, relationship_type=relationship_type,
            first_seen=now, last_seen=now,
        )
        self.records[anchor] = descriptor
        return descriptor

    def merge_sources(self, descriptor: DependencyDescriptor, *, entity_id=None, workflow_id=None, actor_id=None) -> None:
        if entity_id and entity_id not in descriptor.source_entity_ids:
            descriptor.source_entity_ids.append(entity_id)
        if workflow_id and workflow_id not in descriptor.source_workflow_ids:
            descriptor.source_workflow_ids.append(workflow_id)
        if actor_id and actor_id not in descriptor.source_actor_ids:
            descriptor.source_actor_ids.append(actor_id)

    def merge_target(self, descriptor: DependencyDescriptor, output_id: str) -> None:
        if output_id not in descriptor.target_output_ids:
            descriptor.target_output_ids.append(output_id)

    def merge_evidence(self, descriptor: DependencyDescriptor, evidence: list[DependencyEvidence]) -> None:
        descriptor.supporting_evidence = _dedupe_evidence(descriptor.supporting_evidence, evidence)

    def merge_effect_direction(self, descriptor: DependencyDescriptor, direction: str) -> None:
        if direction == "unknown":
            return
        if descriptor.effect_direction == "unknown":
            descriptor.effect_direction = direction
        elif descriptor.effect_direction != direction:
            descriptor.contradictions.append(
                DependencyContradiction(description=f"effect direction conflict: previously '{descriptor.effect_direction}', now '{direction}'")
            )

    def merge_aggregation_rule(self, descriptor: DependencyDescriptor, rule: AggregationRule) -> None:
        if descriptor.aggregation_rule is None:
            descriptor.aggregation_rule = rule
            return
        if descriptor.aggregation_rule.rule_type == rule.rule_type and descriptor.aggregation_rule.group_by_term == rule.group_by_term:
            descriptor.aggregation_rule.confidence = min(1.0, descriptor.aggregation_rule.confidence + 0.05)
            descriptor.aggregation_rule.evidence = _dedupe_evidence(descriptor.aggregation_rule.evidence, rule.evidence)
            return
        existing_competing = {(r.rule_type, r.group_by_term) for r in descriptor.competing_aggregation_rules}
        if (rule.rule_type, rule.group_by_term) not in existing_competing:
            descriptor.competing_aggregation_rules.append(rule)

    def merge_state_rules(
        self, descriptor: DependencyDescriptor, inclusions: list[StateInclusionRule], exclusions: list[StateExclusionRule]
    ) -> None:
        existing_inclusions = {r.state_label for r in descriptor.inclusion_rules}
        for rule in inclusions:
            if rule.state_label not in existing_inclusions:
                descriptor.inclusion_rules.append(rule)
                existing_inclusions.add(rule.state_label)
        existing_exclusions = {r.state_label for r in descriptor.exclusion_rules}
        for rule in exclusions:
            if rule.state_label not in existing_exclusions:
                descriptor.exclusion_rules.append(rule)
                existing_exclusions.add(rule.state_label)

    def merge_scope(self, descriptor: DependencyDescriptor, dims: list[ScopeDimension], temporal=None, actor=None, tenant=None, filter_=None) -> None:
        existing = {(d.dimension_type, d.value) for d in descriptor.scope_dimensions}
        for d in dims:
            if (d.dimension_type, d.value) not in existing:
                descriptor.scope_dimensions.append(d)
                existing.add((d.dimension_type, d.value))
        if temporal is not None:
            descriptor.temporal_scope = temporal
        if actor is not None:
            descriptor.actor_scope = actor
        if tenant is not None:
            descriptor.tenant_scope = tenant
        if filter_ is not None:
            descriptor.filter_scope = filter_

    def merge_cross_role(self, descriptor: DependencyDescriptor, *, producer: str | None, processor: str | None, consumer: str | None, unresolved: bool, workflow_term: str | None) -> None:
        if producer and producer not in descriptor.known_producers:
            descriptor.known_producers.append(producer)
        if processor and processor not in descriptor.known_producers and processor not in descriptor.known_consumers:
            descriptor.known_consumers.append(processor)
        if consumer and consumer not in descriptor.known_consumers:
            descriptor.known_consumers.append(consumer)
        if unresolved:
            marker = f"required_actor:{workflow_term or 'unknown'}"
            if marker not in descriptor.prerequisites:
                descriptor.prerequisites.append(marker)

    def merge_verification_requirements(self, descriptor: DependencyDescriptor, requirements: list[VerificationRequirement]) -> None:
        existing = {(r.step_type, r.description) for r in descriptor.verification_requirements}
        for req in requirements:
            if (req.step_type, req.description) not in existing:
                descriptor.verification_requirements.append(req)
                existing.add((req.step_type, req.description))

    def merge_execution_observation(self, descriptor: DependencyDescriptor, observation: DependencyExecutionObservation) -> bool:
        """Returns True if this observation qualifies the descriptor for
        `status='verified'` (a real, scope-compatible, supported-or-delayed
        before/after correlation) — the ONLY path to that status."""
        observation.dependency_id = descriptor.dependency_id
        prior = self._last_observation.get(descriptor.dependency_id)
        if (
            prior is not None and prior.result == "unsupported" and observation.result == "supported"
            and prior.expected_direction == observation.expected_direction and prior.scope_compatible
        ):
            observation.result = "delayed"
            observation.delay_iterations = max(0, observation.observed_at_iteration - prior.observed_at_iteration)
        self._last_observation[descriptor.dependency_id] = observation
        if observation.result == "contradicted":
            descriptor.contradictions.append(
                DependencyContradiction(
                    description=f"before/after correlation contradicted expected direction '{observation.expected_direction}' (observed '{observation.observed_direction}')",
                    evidence=list(observation.evidence),
                )
            )
        return observation.result in {"supported", "delayed"} and observation.scope_compatible

    def finalize(self, descriptor: DependencyDescriptor, *, page_url: str = "", now: datetime | None = None, verified_observed: bool = False) -> None:
        now = now or datetime.utcnow()
        composite, rel, formula, scope, effect, verification = dependency_confidence.score_confidence(descriptor)
        descriptor.confidence = composite
        descriptor.relationship_confidence = rel
        descriptor.formula_confidence = formula
        descriptor.scope_confidence = scope
        descriptor.effect_direction_confidence = effect
        descriptor.verification_confidence = verification
        descriptor.status = dependency_confidence.status_for(descriptor, verified_observed=verified_observed)
        descriptor.last_seen = now
        descriptor.observation_count += 1
        descriptor.version += 1
