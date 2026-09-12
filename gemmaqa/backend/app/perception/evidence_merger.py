"""Evidence merging — combine DOM- and accessibility-sourced descriptions of the
SAME element without creating duplicate objects.

Multiple extractors (or multiple detection rules within one extractor) can
independently flag the same physical DOM element — e.g. a `<button
aria-haspopup>` matches both a native-tag rule and an ARIA-role rule in
`interactive_detector`. This module is the single place that collapses those
into one object, combining their evidence/confidence rather than silently
keeping only the first or emitting two disconnected records.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from app.perception.models import CanonicalPageModel, ConfidenceScore, EvidenceReference, VisualEvidence
from app.schemas import InteractiveElement

T = TypeVar("T")


def combine_evidence(*evidence_lists) -> list:
    """Concatenate evidence lists, deduping by identity.

    Polymorphic over the two evidence shapes used in this codebase: the rich
    `EvidenceReference` objects (perception-native descriptors, deduped by
    `evidence_id`) and the plain `list[str]` used on the schemas.py-extended
    `InteractiveElement`/`FormDescriptor`/`TableDescriptor` (deduped by value).
    """
    seen: set[str] = set()
    combined: list = []
    for lst in evidence_lists:
        for item in lst:
            key = item.evidence_id if isinstance(item, EvidenceReference) else str(item)
            if key in seen:
                continue
            seen.add(key)
            combined.append(item)
    return combined


def merge_confidence(*scores):
    """The highest-confidence value wins.

    Polymorphic over the rich `ConfidenceScore` object and the plain `float`
    used on the schemas.py-extended elements — returns whichever shape it was
    given.
    """
    if not scores:
        return ConfidenceScore()
    if all(isinstance(s, (int, float)) for s in scores):
        return max(scores)
    scored = [s if isinstance(s, ConfidenceScore) else ConfidenceScore(value=float(s)) for s in scores]
    best = max(scored, key=lambda s: s.value)
    other_bases = sorted({s.basis for s in scored if s is not best})
    notes = best.notes
    if other_bases:
        extra = f"Also supported by: {', '.join(other_bases)}."
        notes = f"{notes} {extra}".strip()
    return ConfidenceScore(value=best.value, basis=best.basis, notes=notes)


def merge_duplicate_elements(
    items: list[T],
    key_fn: Callable[[T], str],
    *,
    merge_fields: dict[str, Callable[[T, T], object]] | None = None,
) -> list[T]:
    """Collapse items sharing the same key (usually a raw `dom_id`/`element_id`)
    into one, combining their `evidence`/`confidence` if the item type has those
    attributes. The FIRST item seen for a key is kept as the base; later
    duplicates only contribute evidence/confidence unless `merge_fields`
    supplies an explicit combiner for another attribute.

    Deterministic: iteration order is preserved (first-seen key order), and
    ties are always resolved the same way for the same input.
    """
    order: list[str] = []
    grouped: dict[str, list[T]] = {}
    for item in items:
        key = key_fn(item)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(item)

    merged: list[T] = []
    for key in order:
        group = grouped[key]
        base = group[0]
        if len(group) == 1:
            merged.append(base)
            continue

        updates: dict[str, object] = {}
        if hasattr(base, "evidence"):
            updates["evidence"] = combine_evidence(*(getattr(g, "evidence", []) for g in group))
        if hasattr(base, "confidence"):
            confidences = [
                getattr(g, "confidence")
                for g in group
                if isinstance(getattr(g, "confidence", None), (ConfidenceScore, int, float))
            ]
            if confidences:
                updates["confidence"] = merge_confidence(*confidences)
        for field_name, combiner in (merge_fields or {}).items():
            updates[field_name] = combiner(base, group[-1])

        if updates and hasattr(base, "model_copy"):
            merged.append(base.model_copy(update=updates))  # type: ignore[attr-defined]
        else:
            merged.append(base)
    return merged


def _confidence_like(existing: object, incoming_value: float, *, basis: str = "llm_hypothesis") -> object:
    """Merge a new confidence VALUE into an existing confidence field,
    preserving whatever shape that field already uses (a plain float on the
    schemas.py-extended `InteractiveElement`, a `ConfidenceScore` object on the
    perception-native descriptors) — `model_copy` does not re-validate, so
    writing the wrong shape back would silently corrupt the field."""
    if isinstance(existing, ConfidenceScore):
        return merge_confidence(existing, ConfidenceScore(value=incoming_value, basis=basis))
    existing_value = float(existing) if isinstance(existing, (int, float)) else 1.0
    return max(existing_value, incoming_value)


def merge_visual_evidence(model: CanonicalPageModel, visual_evidence: list[VisualEvidence]) -> CanonicalPageModel:
    """Attach visual-model output to the canonical model.

    Every `VisualEvidence` record is appended verbatim to
    `model.visual_evidence` (nothing is ever silently dropped). Additionally,
    for whichever existing descriptor (interactive element / image / unknown
    component) shares its `element_id`, the visual confidence is MERGED — via
    `_confidence_like`/`merge_confidence`, never a bare overwrite — and a short
    evidence note records that a visual signal contributed, alongside whatever
    structural/accessibility evidence the element already had. Visual evidence
    for an element_id nothing else recognizes is still kept in
    `visual_evidence` — it simply has no other descriptor to merge onto.
    """
    if not visual_evidence:
        return model

    by_id: dict[str, VisualEvidence] = {}
    for ev in visual_evidence:
        by_id[ev.element_id] = ev  # last one wins on a duplicate id within one call

    def _bump(collection: list[T]) -> list[T]:
        updated: list[T] = []
        for item in collection:
            key = getattr(item, "element_id", None) or getattr(item, "stable_id", None)
            ev = by_id.get(key) if key else None
            if ev is None:
                updated.append(item)
                continue
            if isinstance(item, InteractiveElement):
                new_evidence = combine_evidence(item.evidence, [f"visual:{ev.visual_semantic_type}"])
            else:
                new_evidence = combine_evidence(
                    item.evidence,
                    [
                        EvidenceReference(
                            kind="screenshot",
                            reference=(ev.screenshot_reference.reference if ev.screenshot_reference else ""),
                            description=ev.description,
                        )
                    ],
                )
            updated.append(
                item.model_copy(
                    update={
                        "evidence": new_evidence,
                        "confidence": _confidence_like(item.confidence, ev.confidence.value),
                    }
                )
            )
        return updated

    return model.model_copy(
        update={
            "interactive_elements": _bump(model.interactive_elements),
            "images": _bump(model.images),
            "unknown_components": _bump(model.unknown_components),
            "visual_evidence": [*model.visual_evidence, *visual_evidence],
        }
    )
