"""Dependency confidence model — transparent, corroboration-based, and
explicitly NOT computed from evidence-kind count alone.

Extends the same "distinct kinds beat repetition, richness bonus,
contradiction penalty, diminishing repeats" model already proven in
entity/actor/workflow discovery, but the task requires this package to keep
FIVE separate sub-scores rather than one composite:

- relationship_confidence — is the source-to-target LINK itself well
  evidenced (right entity/workflow, right output)?
- formula_confidence — how sure are we of the AGGREGATION RULE (count vs.
  sum vs. filtered count, ...)? Ambiguity (competing rules) lowers this.
- scope_confidence — how well do we understand WHICH scope (actor/tenant/
  time/filter) this value applies under?
- effect_direction_confidence — how sure are we of increase-vs-decrease-
  vs-other?
- verification_confidence — has an actual before/after observation
  SUPPORTED this, or is it still purely structural inference?

The composite `confidence` on `DependencyDescriptor` is always DERIVED from
these five, never computed independently — so "why is this 0.6?" always has
a five-part answer.
"""

from __future__ import annotations

from app.intelligence.dependency_discovery.schemas import DependencyDescriptor, DependencyEvidence

SOURCE_KIND_WEIGHTS: dict[str, float] = {
    "before_after_observation": 0.30,
    "workflow_transition": 0.25,
    "network_aggregate_field": 0.22,
    "network_endpoint": 0.12,
    "table_total": 0.18,
    "table_header": 0.12,
    "chart_legend": 0.15,
    "chart_image": 0.10,
    "entity_registry": 0.15,
    "actor_registry": 0.12,
    "workflow_registry": 0.15,
    "alert": 0.10,
    "dialog": 0.08,
    "interactive_element": 0.06,
    "heading": 0.06,
    "text_block": 0.05,
    "terminology_match": 0.08,
    "label_match": 0.06,
    "application_memory": 0.05,
}

MIN_DISTINCT_SOURCES_OBSERVED = 2
OBSERVED_MIN_CONFIDENCE = 0.35
REPEAT_DECAY = 0.15


def score_evidence(evidence: list[DependencyEvidence]) -> float:
    seen_exact: set[tuple[str, str, str]] = set()
    seen_kinds: set[str] = set()
    total = 0.0
    for ev in evidence:
        weight = SOURCE_KIND_WEIGHTS.get(ev.source_kind, 0.05)
        key = (ev.source_kind, ev.observed_text, ev.page_url)
        if key in seen_exact:
            continue
        seen_exact.add(key)
        if ev.source_kind in seen_kinds:
            total += weight * REPEAT_DECAY
        else:
            total += weight
            seen_kinds.add(ev.source_kind)
    return min(1.0, total)


def relationship_confidence(descriptor: DependencyDescriptor) -> float:
    base = score_evidence(descriptor.supporting_evidence)
    bonus = 0.0
    if descriptor.source_entity_ids or descriptor.source_workflow_ids:
        bonus += 0.15
    if descriptor.target_output_ids:
        bonus += 0.10
    if descriptor.known_producers and descriptor.known_consumers:
        bonus += 0.10
    penalty = 0.15 * len(descriptor.contradictions)
    return max(0.0, min(1.0, base + bonus - penalty))


def formula_confidence(descriptor: DependencyDescriptor) -> float:
    if descriptor.aggregation_rule is None:
        return 0.0
    base = descriptor.aggregation_rule.confidence
    # Ambiguity (multiple competing explanations) means we know SOMETHING
    # aggregates the value, but not precisely which rule — never claim a
    # precise formula while alternatives remain unresolved.
    ambiguity_penalty = 0.08 * len(descriptor.competing_aggregation_rules)
    richness = 0.05 * min(2, len(descriptor.inclusion_rules) + len(descriptor.exclusion_rules))
    return max(0.0, min(1.0, base + richness - ambiguity_penalty))


def scope_confidence(descriptor: DependencyDescriptor) -> float:
    known = sum(
        1
        for scope in (descriptor.temporal_scope, descriptor.actor_scope, descriptor.tenant_scope, descriptor.filter_scope)
        if scope is not None
    )
    dims = min(3, len(descriptor.scope_dimensions))
    return min(1.0, 0.15 * known + 0.1 * dims)


def effect_direction_confidence(descriptor: DependencyDescriptor) -> float:
    if descriptor.effect_direction == "unknown":
        return 0.0
    supporting = [c for c in descriptor.contradictions]
    base = 0.35
    base -= 0.1 * len(supporting)
    return max(0.0, min(1.0, base))


def verification_confidence(descriptor: DependencyDescriptor) -> float:
    """Only actual before/after correlation moves this — never label
    matching or terminology alone. This is the sub-score that gates
    `status_for()`'s "verified" outcome, so it must stay conservative."""
    if descriptor.status == "verified":
        return max(0.6, min(1.0, 0.6 + 0.1 * (descriptor.observation_count - 1)))
    return 0.0


def dependency_richness_bonus(descriptor: DependencyDescriptor) -> float:
    bonus = 0.0
    if descriptor.aggregation_rule is not None:
        bonus += 0.10
    if descriptor.scope_dimensions:
        bonus += 0.05
    if descriptor.known_producers and descriptor.known_consumers:
        bonus += 0.10
    if descriptor.is_cross_role():
        bonus += 0.10
    if descriptor.verification_requirements:
        bonus += 0.05
    return bonus


def contradiction_penalty(descriptor: DependencyDescriptor) -> float:
    if descriptor.status == "contradicted":
        return 0.35
    return min(0.5, 0.15 * len(descriptor.contradictions))


def score_confidence(descriptor: DependencyDescriptor) -> tuple[float, float, float, float, float, float]:
    """Returns (composite, relationship, formula, scope, effect_direction,
    verification) — all five sub-scores computed once, so callers never
    recompute them independently and risk drift."""
    rel = relationship_confidence(descriptor)
    formula = formula_confidence(descriptor)
    scope = scope_confidence(descriptor)
    effect = effect_direction_confidence(descriptor)
    verification = verification_confidence(descriptor)
    bonus = dependency_richness_bonus(descriptor)
    penalty = contradiction_penalty(descriptor)
    # Relationship strength dominates (it is the core claim); the other four
    # sub-scores refine it. Weighted, not averaged evenly, so a dependency
    # with a rock-solid link but unknown formula/scope isn't punished as
    # hard as one with a weak link but a clean formula.
    composite = 0.45 * rel + 0.2 * formula + 0.15 * scope + 0.1 * effect + 0.1 * verification
    composite = max(0.0, min(1.0, composite + bonus - penalty))
    return composite, rel, formula, scope, effect, verification


def status_for(descriptor: DependencyDescriptor, *, verified_observed: bool = False) -> str:
    """`verified_observed` is passed explicitly by the correlator/memory
    layer (never re-derived here) — true only when at least one
    `DependencyExecutionObservation` with `result == "supported"` AND
    `scope_compatible` is attached. This is the ONLY path to "verified":
    label/terminology matching, however confident, can never produce it."""
    if contradiction_penalty(descriptor) > 0 and not verified_observed:
        return "contradicted"
    if verified_observed:
        return "verified"
    if descriptor.requires_actor_switch() and not descriptor.known_producers:
        return "blocked"
    distinct_kinds = {ev.source_kind for ev in descriptor.supporting_evidence}
    direct_kinds = distinct_kinds & {"before_after_observation", "workflow_transition", "network_aggregate_field"}
    if direct_kinds and descriptor.target_output_ids and (descriptor.source_entity_ids or descriptor.source_workflow_ids):
        return "observed"
    if len(distinct_kinds) < MIN_DISTINCT_SOURCES_OBSERVED or descriptor.confidence < OBSERVED_MIN_CONFIDENCE:
        return "candidate"
    if descriptor.target_output_ids and (descriptor.source_entity_ids or descriptor.source_workflow_ids):
        return "partially_observed"
    return "inferred"
