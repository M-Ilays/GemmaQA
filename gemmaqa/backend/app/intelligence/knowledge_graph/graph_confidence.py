"""Graph confidence — node confidence mirrors its source record directly
(the graph does not re-judge an entity/actor/workflow/dependency's own
confidence); edge confidence layers graph-specific factors on top of the
source relationship's own confidence, never discarding it.

Rules enforced here (task section 16):
- directly observed edges outrank inferred edges (inferred edges are
  capped at the weakest input premise's confidence, decayed by inference
  depth, unless independently corroborated).
- duplicate evidence must not inflate confidence indefinitely (handled by
  `knowledge_graph_memory`'s exact-tuple evidence dedup before this ever
  sees a "count").
- stale source records reduce current confidence.
- contradictions reduce confidence without deleting support.
"""

from __future__ import annotations

DEPTH_DECAY = 0.85
STALE_PENALTY_FACTOR = 0.6
CONTRADICTION_PENALTY_PER_ITEM = 0.15
MAX_CONTRADICTION_PENALTY = 0.4
INDEPENDENT_SOURCE_BONUS = 0.05
MAX_INDEPENDENT_SOURCE_BONUS = 0.15


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def node_confidence_from_source(source_confidence: float) -> float:
    """Node confidence is a direct reflection of the source registry
    record's own confidence — the graph adds no topology-derived boost."""
    return _clamp(source_confidence)


def observed_edge_confidence(
    source_confidence: float,
    *,
    contradiction_count: int = 0,
    stale: bool = False,
    independent_source_count: int = 1,
) -> float:
    conf = source_confidence
    if independent_source_count > 1:
        conf = min(1.0, conf + min(MAX_INDEPENDENT_SOURCE_BONUS, INDEPENDENT_SOURCE_BONUS * (independent_source_count - 1)))
    if contradiction_count:
        conf = max(0.0, conf - min(MAX_CONTRADICTION_PENALTY, CONTRADICTION_PENALTY_PER_ITEM * contradiction_count))
    if stale:
        conf *= STALE_PENALTY_FACTOR
    return _clamp(conf)


def inferred_edge_confidence(
    premise_confidences: list[float],
    *,
    depth: int = 1,
    contradiction_count: int = 0,
    stale: bool = False,
    independent_source_count: int = 1,
) -> float:
    """An inferred edge's confidence is capped at the WEAKEST premise it
    depends on, decayed per hop of inference depth — it can never exceed
    what its shakiest input actually supports, absent independent
    corroboration."""
    if not premise_confidences:
        return 0.0
    base = min(premise_confidences) * (DEPTH_DECAY ** max(0, depth - 1))
    return observed_edge_confidence(base, contradiction_count=contradiction_count, stale=stale, independent_source_count=independent_source_count)


def dependency_confidence_attributes(dependency) -> dict[str, float]:
    """Preserve the Dependency Registry's five separate confidence
    sub-scores as edge attributes when projecting a `DependencyDescriptor`
    — never collapsed into the single edge `confidence` value alone."""
    return {
        "relationship_confidence": round(getattr(dependency, "relationship_confidence", 0.0), 4),
        "formula_confidence": round(getattr(dependency, "formula_confidence", 0.0), 4),
        "scope_confidence": round(getattr(dependency, "scope_confidence", 0.0), 4),
        "effect_direction_confidence": round(getattr(dependency, "effect_direction_confidence", 0.0), 4),
        "verification_confidence": round(getattr(dependency, "verification_confidence", 0.0), 4),
    }


def status_for_projection(source_status: str, *, is_inferred: bool = False) -> str:
    """Maps a source registry's own status vocabulary onto the graph's
    status vocabulary, never promoting inferred/candidate to observed."""
    if is_inferred:
        return "inferred"
    mapping = {
        "confirmed": "observed", "partial": "partially_observed", "candidate": "candidate",
        "verified": "verified", "contradicted": "contradicted", "blocked": "blocked",
        "stale": "stale", "observed": "observed", "unverified": "candidate",
        "inferred": "inferred", "unknown": "unknown", "partially_observed": "partially_observed",
        # WorkflowStep's own STEP_STATUSES vocabulary carries one value
        # graph statuses don't: "completed" -- a step that finished
        # successfully is graph-equivalent to "observed" (it was actually
        # seen to happen), never silently dropped to "unknown".
        "completed": "observed",
    }
    return mapping.get(source_status, "unknown")
