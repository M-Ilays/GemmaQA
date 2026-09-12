"""Visual Observation Policy — decides WHETHER and WHAT the (comparatively
expensive, non-deterministic) visual model should look at. Screenshot
analysis is never mandatory: by default a page produces
`CanonicalPageModel.visual_evidence == []` with zero visual-model calls.

The policy only ever reads already-extracted, deterministic facts (the raw
DOM pass, the interactive/image/unknown-component descriptors the rest of the
engine already built) — it makes no browser calls and no LLM calls itself.
It is pure decision logic: given the same inputs, it always returns the same
`VisualObservationDecision`.

See docs/VISUAL_OBSERVATION_POLICY.md for the full rationale behind each
trigger condition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.perception.dom_extractor import RawObservation
from app.perception.image_extractor import MEANINGFUL_IMAGE_TYPES
from app.perception.models import ImageDescriptor, UnknownComponent
from app.schemas import InteractiveElement

# A cluster of unknown components this size or larger suggests the page's
# structure is ambiguous enough that overall visual layout/grouping context
# would help interpret it — not any single element, the page as a whole.
LAYOUT_GROUPING_UNKNOWN_THRESHOLD = 3

# Two distinct visible controls whose bounding boxes overlap this much (IoU)
# are either genuinely stacked (a real rendering defect) or the same control
# double-counted — either way worth a visual look. Ordinary DOM nesting (an
# icon inside a button) has a LOW IoU (the child is much smaller than the
# parent), so this threshold does not fire on normal containment.
OVERLAP_IOU_THRESHOLD = 0.6
MAX_OVERLAP_CANDIDATES = 150


@dataclass
class VisualTarget:
    """One element/region the policy wants the visual model to look at,
    together with WHY (so the analyzer/report can explain any resulting
    evidence rather than presenting an unexplained crop)."""

    element_id: str
    reasons: list[str] = field(default_factory=list)
    bounding_box: dict[str, float] | None = None
    kind: str = "element"  # element | image | region

    def to_dict(self) -> dict[str, Any]:
        return {
            "element_id": self.element_id,
            "reasons": list(self.reasons),
            "bounding_box": self.bounding_box,
            "kind": self.kind,
        }


@dataclass
class VisualObservationDecision:
    should_run: bool
    reasons: list[str] = field(default_factory=list)
    targets: list[VisualTarget] = field(default_factory=list)
    needs_full_page: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "should_run": self.should_run,
            "reasons": list(self.reasons),
            "targets": [t.to_dict() for t in self.targets],
            "needs_full_page": self.needs_full_page,
        }


def _bbox_iou(a: dict[str, float], b: dict[str, float]) -> float:
    ax1, ay1 = float(a.get("x", 0) or 0), float(a.get("y", 0) or 0)
    ax2, ay2 = ax1 + float(a.get("width", 0) or 0), ay1 + float(a.get("height", 0) or 0)
    bx1, by1 = float(b.get("x", 0) or 0), float(b.get("y", 0) or 0)
    bx2, by2 = bx1 + float(b.get("width", 0) or 0), by1 + float(b.get("height", 0) or 0)

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _detect_overlapping_pairs(
    elements: list[InteractiveElement],
) -> list[tuple[InteractiveElement, InteractiveElement]]:
    candidates = [
        el
        for el in elements
        if el.is_visible
        and el.bounding_box
        and float(el.bounding_box.get("width", 0) or 0) > 0
        and float(el.bounding_box.get("height", 0) or 0) > 0
    ][:MAX_OVERLAP_CANDIDATES]
    pairs: list[tuple[InteractiveElement, InteractiveElement]] = []
    for i, a in enumerate(candidates):
        for b in candidates[i + 1 :]:
            if _bbox_iou(a.bounding_box, b.bounding_box) < OVERLAP_IOU_THRESHOLD:
                continue
            if _is_benign_overlap(a, b):
                continue
            pairs.append((a, b))
    return pairs


def _is_benign_overlap(a: InteractiveElement, b: InteractiveElement) -> bool:
    """Two well-understood, structurally NORMAL overlap patterns that a bare
    geometry check can't tell apart from a genuine rendering defect —
    live-verified on SauceDemo (both patterns fired on every run, on a page
    with no actual visual bug):

    1. A native <select> always visually overlaps its own rendered current-
       value text — that's how every styled dropdown looks, not a collision
       between two different things.
    2. Two elements with the exact same label sharing the exact same box are
       almost always a nested/duplicate link wrapping one piece of content
       (e.g. a product card's image-link and title-link both targeting the
       same product) — a real defect is two DIFFERENT things stacking, not
       one thing linked twice.
    """
    if (a.tag or "").lower() == "select" or (b.tag or "").lower() == "select":
        return True
    label_a = (a.accessible_name or a.text or "").strip()
    label_b = (b.accessible_name or b.text or "").strip()
    if label_a and label_a == label_b:
        return True
    return False


class VisualObservationPolicy:
    """Deterministic gate for the visual layer. `evaluate()` never raises for
    normal inputs and never calls out to a model — it only decides."""

    def evaluate(
        self,
        *,
        raw: RawObservation,
        interactive_elements: list[InteractiveElement],
        images: list[ImageDescriptor],
        unknown_components: list[UnknownComponent],
    ) -> VisualObservationDecision:
        reasons: set[str] = set()
        targets: dict[str, VisualTarget] = {}
        needs_full_page = False

        def _add_target(element_id: str | None, reason: str, bounding_box: Any, kind: str = "element") -> None:
            if not element_id:
                return
            reasons.add(reason)
            existing = targets.get(element_id)
            if existing is None:
                targets[element_id] = VisualTarget(
                    element_id=element_id, reasons=[reason], bounding_box=bounding_box, kind=kind
                )
            elif reason not in existing.reasons:
                existing.reasons.append(reason)
                if existing.bounding_box is None:
                    existing.bounding_box = bounding_box

        # 1. Important visible controls with no accessible name.
        for el in interactive_elements:
            if not el.is_visible:
                continue
            has_name = bool(el.accessible_name or el.visible_text or el.text or el.aria_label)
            if not has_name:
                _add_target(el.element_id, "missing_accessible_name", el.bounding_box)

        # 2. Icon-only controls (raw signal — every one the DOM pass flagged,
        # whether or not it made it into `interactive_elements`).
        for raw_el in raw.elements:
            if raw_el.get("looks_icon_only") and raw_el.get("is_visible", True):
                _add_target(raw_el.get("dom_id"), "icon_only_control", raw_el.get("bounding_box"))

        # 3. Canvas elements (opaque pixel content by construction).
        for canvas in raw.canvases:
            _add_target(canvas.get("dom_id"), "canvas_element", canvas.get("bounding_box"))

        # 4. SVG controls that carry no role/accessible-name signal at all —
        # a bare inline <svg> icon button GemmaQA cannot semantically classify
        # from markup alone.
        for raw_el in raw.elements:
            if (
                raw_el.get("has_inline_svg")
                and not raw_el.get("role")
                and not (raw_el.get("accessible_name") or "").strip()
                and not (raw_el.get("text") or "").strip()
            ):
                _add_target(raw_el.get("dom_id"), "unclassified_svg_control", raw_el.get("bounding_box"))

        # 5. Images that heuristically look informational / like a scanned
        # document / a chart (never decorative/avatar/logo — those already
        # carry enough evidence without a visual-model call).
        for img in images:
            if img.visual_semantic_type in MEANINGFUL_IMAGE_TYPES:
                _add_target(
                    img.element_id,
                    f"meaningful_image:{img.visual_semantic_type}",
                    img.bounding_box,
                    kind="image",
                )

        # 6. Visual layout or grouping needed — a large residue of elements
        # nothing could structurally classify suggests the page's layout
        # itself is ambiguous, not just one element.
        if len(unknown_components) >= LAYOUT_GROUPING_UNKNOWN_THRESHOLD:
            reasons.add("layout_grouping_needed")
            needs_full_page = True

        # 7. Possible visual defect — two distinct visible controls whose
        # boxes overlap heavily (a real stacking/z-index bug candidate).
        for a, b in _detect_overlapping_pairs(interactive_elements):
            reasons.add("possible_visual_defect")
            needs_full_page = True
            _add_target(a.element_id, "possible_visual_defect", a.bounding_box)
            _add_target(b.element_id, "possible_visual_defect", b.bounding_box)

        # 8. Unknown interactive components remaining after DOM+accessibility.
        for u in unknown_components:
            _add_target(u.element_id or u.stable_id, "unknown_component_remaining", u.bounding_box)

        should_run = bool(reasons)
        return VisualObservationDecision(
            should_run=should_run,
            reasons=sorted(reasons),
            targets=list(targets.values()),
            needs_full_page=needs_full_page,
        )
