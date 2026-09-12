"""Visual analyzer orchestration — turns a `VisualObservationDecision` into
zero or more validated `VisualEvidence` records via exactly ONE bounded model
call per observation (never one call per element).

This module builds crops and assembles the existing DOM/accessibility/
nearby-text evidence for each target, then delegates the actual model call to
`GemmaProvider.analyze_visual_elements()` — the same provider abstraction
already used for action selection, so no separate model stack is introduced.
Nothing here executes a browser action or invents an element; unknown/
invented element_ids are dropped by the parser before this module ever sees
them (see app.gemma.parser.parse_visual_analysis).
"""

from __future__ import annotations

from typing import Any

from app.gemma.base import GemmaProvider
from app.perception.accessibility_extractor import AccessibilityInfo
from app.perception.dom_extractor import RawObservation
from app.perception.evidence_primitives import (
    accessibility_evidence_for,
    dom_evidence_for,
    nearby_text_for,
)
from app.perception.models import VisualEvidence
from app.perception.screenshot_utils import crop_bounding_box
from app.perception.visual_policy import VisualObservationDecision
from app.utils.logging import get_logger

logger = get_logger("perception.visual_analyzer")

# One visual-model call is asked about at most this many elements — the
# policy may flag more, but the call itself stays bounded and cheap rather
# than growing unbounded on a page with many ambiguous controls.
MAX_TARGETS_PER_CALL = 12


def _raw_index(raw: RawObservation) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for el in raw.elements:
        dom_id = el.get("dom_id")
        if dom_id:
            index[dom_id] = el
    for img in raw.images:
        dom_id = img.get("dom_id")
        if dom_id:
            index.setdefault(dom_id, img)
    for canvas in raw.canvases:
        dom_id = canvas.get("dom_id")
        if dom_id:
            index.setdefault(dom_id, canvas)
    return index


# These three are now shared with the general evidence retriever
# (app/gemma/evidence_retrieval.py) — one implementation, two consumers. The
# private aliases are kept so existing imports and call sites are unchanged.
_dom_evidence_for = dom_evidence_for
_accessibility_evidence_for = accessibility_evidence_for
_nearby_text_for = nearby_text_for


class VisualAnalyzer:
    """Bounded orchestrator: crops targets, assembles existing evidence, and
    delegates ONE model call to `provider.analyze_visual_elements()`."""

    def __init__(self, provider: GemmaProvider, *, max_targets: int = MAX_TARGETS_PER_CALL) -> None:
        self.provider = provider
        self.max_targets = max_targets

    async def analyze(
        self,
        decision: VisualObservationDecision,
        *,
        raw: RawObservation,
        accessibility_index: dict[str, AccessibilityInfo],
        screenshot_path: str,
        crop_dir: str | None = None,
    ) -> list[VisualEvidence]:
        if not decision.should_run or not screenshot_path:
            return []

        targets = decision.targets[: self.max_targets]
        if not targets:
            return []
        dropped = len(decision.targets) - len(targets)
        if dropped > 0:
            logger.info(
                "Visual policy selected %d targets; capping to %d per call (dropped %d)",
                len(decision.targets),
                self.max_targets,
                dropped,
            )

        raw_index = _raw_index(raw)
        images: list[str] = []
        target_payload: list[dict[str, Any]] = []
        dom_evidence: dict[str, Any] = {}
        accessibility_evidence: dict[str, Any] = {}
        nearby_text: dict[str, Any] = {}

        for target in targets:
            crop_path = None
            if target.bounding_box:
                crop_path = crop_bounding_box(
                    screenshot_path,
                    target.bounding_box,
                    element_id=target.element_id,
                    out_dir=crop_dir,
                )
            image_path = crop_path or screenshot_path
            if image_path not in images:
                images.append(image_path)

            raw_el = raw_index.get(target.element_id, {})
            dom_evidence[target.element_id] = _dom_evidence_for(raw_el)
            accessibility_evidence[target.element_id] = _accessibility_evidence_for(
                accessibility_index.get(target.element_id)
            )
            nearby_text[target.element_id] = _nearby_text_for(raw_el)
            target_payload.append(
                {
                    "element_id": target.element_id,
                    "reasons": target.reasons,
                    "bounding_box": target.bounding_box,
                    "image": image_path,
                }
            )

        if decision.needs_full_page and screenshot_path not in images:
            images.append(screenshot_path)

        # Validate strictly against the targets actually SENT in this call
        # (post-cap) — never a broader "everything on the page" set — so the
        # parser cannot accept a reference to an element the model was never
        # shown evidence for in this round.
        known_element_ids = {t.element_id for t in targets}

        evidence = await self.provider.analyze_visual_elements(
            targets=target_payload,
            dom_evidence=dom_evidence,
            accessibility_evidence=accessibility_evidence,
            nearby_text=nearby_text,
            trigger_reasons=decision.reasons,
            images=images,
            known_element_ids=known_element_ids,
        )

        target_reasons = {t.element_id: t.reasons for t in targets}
        for item in evidence:
            item.trigger_reasons = target_reasons.get(item.element_id, [])
        return evidence
