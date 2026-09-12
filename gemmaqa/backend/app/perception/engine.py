"""Universal Page Perception Engine — the orchestrator that turns one raw DOM
observation into a `CanonicalPageModel`.

Runs entirely client-side/deterministically: one `page.evaluate()` DOM walk
(`dom_extractor`), then a fixed pipeline of pure-Python extractor modules, then
evidence merging and unknown-component detection. No LLM call, no hypothesis
logic, no browser action — this is a read-only observation layer, exactly like
`PageObserver`, and preserves the same `BrowserAdapter` boundary: the direct-
Playwright path gets the full, rich extraction; any other adapter degrades to
the already-existing, already-tested `CanonicalPageModel.from_page_state()`
adapter over whatever generic `PageState` that adapter can produce.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlparse

from playwright.async_api import Page

if TYPE_CHECKING:
    from app.gemma.base import GemmaProvider

from app.perception import (
    accessibility_extractor,
    action_semantics,
    collection_extractor,
    dialog_extractor,
    dom_extractor,
    form_extractor,
    form_intent_classifier,
    image_extractor,
    interactive_detector,
    navigation_extractor,
    region_classifier,
    state_builder,
    table_extractor,
    unknown_detector,
)
from app.perception.evidence_merger import merge_duplicate_elements, merge_visual_evidence
from app.perception.models import CanonicalPageModel, EvidenceReference
from app.perception.visual_analyzer import VisualAnalyzer
from app.perception.visual_policy import VisualObservationDecision, VisualObservationPolicy
from app.schemas import NetworkEntry, PageState
from app.utils.exploration_trace import record as trace_record
from app.utils.logging import get_logger

logger = get_logger("perception.engine")


class PerceptionEngine:
    """Deterministic DOM -> CanonicalPageModel pipeline, with an OPTIONAL
    additive visual layer.

    Gemma is never involved in the CORE extraction (per the task's explicit
    constraint) — every field the deterministic pipeline produces comes from
    a fixed rule over the raw DOM observation, and `observe()`/
    `observe_adapter()` remain fully deterministic when no `visual_provider`
    is configured (the default in every existing test). When a
    `visual_provider` IS supplied, a `VisualObservationPolicy` decides — per
    observation, from deterministic signals only — whether a bounded, single
    visual-model call is warranted; screenshot analysis is never mandatory
    and a visual-analysis failure never breaks the deterministic result. See
    docs/VISUAL_OBSERVATION_POLICY.md.
    """

    def __init__(
        self,
        *,
        max_elements: int = dom_extractor.MAX_ELEMENTS,
        visual_provider: "GemmaProvider | None" = None,
        visual_policy: VisualObservationPolicy | None = None,
        crop_dir: str | None = None,
    ) -> None:
        self.max_elements = max_elements
        self.visual_provider = visual_provider
        self.visual_policy = visual_policy or VisualObservationPolicy()
        self.crop_dir = crop_dir

    async def observe(
        self,
        page: Page,
        *,
        screenshot_path: str | None = None,
        known_role: str | None = None,
        network_entries: list[NetworkEntry] | None = None,
    ) -> CanonicalPageModel:
        """Full, rich extraction against a live Playwright page — the direct
        BrowserAdapter path."""
        raw = await dom_extractor.extract_raw(page, max_elements=self.max_elements)
        accessibility_index = accessibility_extractor.build_accessibility_index(raw)
        model = self._build_from_raw(
            raw,
            screenshot_path=screenshot_path,
            known_role=known_role,
            network_entries=network_entries or [],
            accessibility_index=accessibility_index,
        )
        model = await self._maybe_apply_visual(
            model, raw=raw, accessibility_index=accessibility_index, screenshot_path=screenshot_path
        )
        self._trace(model)
        return model

    async def observe_adapter(
        self,
        adapter: Any,
        page_state: PageState,
        *,
        known_role: str | None = None,
    ) -> CanonicalPageModel:
        """MCP-safe path: an adapter without a native Playwright `Page` can't
        run the rich JS extraction, so this degrades to the deterministic
        `CanonicalPageModel.from_page_state()` adapter over whatever
        `PageState` the existing `PageObserver.observe_adapter()` already
        produced for it — preserving the BrowserAdapter boundary rather than
        reaching into adapter internals it doesn't expose. No raw DOM
        observation exists on this path, so the visual layer (which needs raw
        icon/canvas/svg signals) does not run here either."""
        model = CanonicalPageModel.from_page_state(page_state)
        if known_role:
            # from_page_state has no role concept (PageState doesn't carry
            # one) — the caller's known role is layered on afterward via the
            # same state_builder fingerprint logic for consistency.
            model = model.model_copy(
                update={
                    "page_state": state_builder.build_page_state(
                        url=model.url,
                        tabs=model.tabs,
                        dialogs=model.dialogs,
                        interactive_elements=model.interactive_elements,
                        pagination=model.pagination,
                        regions=model.regions,
                        known_role=known_role,
                    )
                }
            )
        self._trace(model)
        return model

    # -- visual layer (optional, additive) ---------------------------------

    async def _maybe_apply_visual(
        self,
        model: CanonicalPageModel,
        *,
        raw: dom_extractor.RawObservation,
        accessibility_index: dict,
        screenshot_path: str | None,
    ) -> CanonicalPageModel:
        if self.visual_provider is None or not screenshot_path:
            return model

        decision = self.visual_policy.evaluate(
            raw=raw,
            interactive_elements=model.interactive_elements,
            images=model.images,
            unknown_components=model.unknown_components,
        )
        self._trace_visual_decision(model, decision)
        if not decision.should_run:
            return model

        try:
            analyzer = VisualAnalyzer(self.visual_provider)
            visual_evidence = await analyzer.analyze(
                decision,
                raw=raw,
                accessibility_index=accessibility_index,
                screenshot_path=screenshot_path,
                crop_dir=self.crop_dir,
            )
        except Exception as exc:
            logger.warning(
                "Visual analysis failed for %s (%s); continuing with deterministic evidence only",
                model.url,
                exc,
            )
            return model

        return merge_visual_evidence(model, visual_evidence)

    @staticmethod
    def _trace_visual_decision(model: CanonicalPageModel, decision: VisualObservationDecision) -> None:
        trace_record(
            "perception.visual_decision",
            url=model.url,
            should_run=decision.should_run,
            reasons=decision.reasons,
            target_count=len(decision.targets),
            needs_full_page=decision.needs_full_page,
        )

    # -- internal ---------------------------------------------------------

    def _build_from_raw(
        self,
        raw: dom_extractor.RawObservation,
        *,
        screenshot_path: str | None,
        known_role: str | None,
        network_entries: list[NetworkEntry],
        accessibility_index: dict | None = None,
    ) -> CanonicalPageModel:
        accessibility_index = (
            accessibility_index
            if accessibility_index is not None
            else accessibility_extractor.build_accessibility_index(raw)
        )

        headings = dom_extractor.extract_headings(raw)
        text_blocks = dom_extractor.extract_text_blocks(raw)
        links = dom_extractor.extract_links(raw)
        interactive_elements = interactive_detector.detect_interactive_elements(raw, accessibility_index)
        navigation_regions = navigation_extractor.extract_navigation_regions(raw)
        breadcrumbs = navigation_extractor.extract_breadcrumbs(raw)
        pagination = navigation_extractor.extract_pagination(raw)
        tabs = navigation_extractor.extract_tabs(raw)
        forms = form_extractor.extract_forms(raw)
        tables = table_extractor.extract_tables(raw)
        action_semantics_list = action_semantics.classify_actions(raw)
        action_semantics_map = action_semantics.action_semantics_by_id(action_semantics_list)
        collections = collection_extractor.extract_collections(raw, action_semantics_map)
        images = image_extractor.extract_images(raw)
        dialogs = dialog_extractor.extract_dialogs(raw)
        alerts = dialog_extractor.extract_alerts(raw)

        landmark_regions = region_classifier.classify_landmark_regions(raw)
        structural_regions = region_classifier.synthesize_structural_regions(raw)
        card_group_regions = region_classifier.synthesize_card_group_region(interactive_elements)
        utility_regions = region_classifier.synthesize_utility_region(raw)
        regions = merge_duplicate_elements(
            [*landmark_regions, *structural_regions, *card_group_regions, *utility_regions],
            lambda r: r.stable_id,
        )
        visual_regions = region_classifier.classify_visual_regions(raw)

        claimed_dom_ids = self._claimed_ids(
            interactive_elements, links, forms, tables, images, dialogs, headings
        )
        unknown_components = unknown_detector.detect_unknown_components(raw, claimed_dom_ids)

        network_evidence = [
            CanonicalPageModel._network_entry_to_evidence(entry) for entry in network_entries
        ]

        page_state_descriptor = state_builder.build_page_state(
            url=raw.url,
            tabs=tabs,
            dialogs=dialogs,
            interactive_elements=interactive_elements,
            pagination=pagination,
            regions=regions,
            known_role=known_role,
        )

        screenshot_reference = (
            EvidenceReference(kind="screenshot", reference=screenshot_path) if screenshot_path else None
        )

        model = CanonicalPageModel(
            url=raw.url,
            domain=urlparse(raw.url).hostname or "",
            title=raw.title,
            state_fingerprint=page_state_descriptor.fingerprint,
            page_state=page_state_descriptor,
            regions=regions,
            navigation_regions=navigation_regions,
            headings=headings,
            text_blocks=text_blocks,
            breadcrumbs=breadcrumbs,
            pagination=pagination,
            links=links,
            tabs=tabs,
            forms=forms,
            tables=tables,
            collections=collections,
            action_semantics=action_semantics_list,
            images=images,
            dialogs=dialogs,
            alerts=alerts,
            visual_regions=visual_regions,
            interactive_elements=interactive_elements,
            unknown_components=unknown_components,
            network_evidence=network_evidence,
            screenshot_reference=screenshot_reference,
        )
        # Form intent classification needs the fully-built model (dialogs,
        # collections, headings) as context — computed as a second pass over
        # the already-constructed model rather than threading every field
        # through as loose arguments.
        model.form_intents = form_intent_classifier.classify_form_intents(model, action_semantics_map)
        return model

    @staticmethod
    def _claimed_ids(*collections) -> set[str]:
        claimed: set[str] = set()
        for collection in collections:
            for item in collection:
                dom_id = getattr(item, "element_id", None) or getattr(item, "stable_id", None)
                if dom_id:
                    claimed.add(dom_id)
        return claimed

    @staticmethod
    def _trace(model: CanonicalPageModel) -> None:
        trace_record(
            "perception.observation",
            url=model.url,
            state_fingerprint=model.state_fingerprint,
            regions=[{"region_type": r.region_type, "confidence": r.confidence.value} for r in model.regions],
            element_counts={
                "interactive_elements": len(model.interactive_elements),
                "links": len(model.links),
                "forms": len(model.forms),
                "tables": len(model.tables),
                "collections": len(model.collections),
                "form_intents": len(model.form_intents),
                "action_semantics": len(model.action_semantics),
                "images": len(model.images),
                "dialogs": len(model.dialogs),
                "headings": len(model.headings),
                "tabs": sum(len(tg.tabs) for tg in model.tabs),
                "visual_evidence": len(model.visual_evidence),
            },
            unknown_element_count=len(model.unknown_components),
            unknown_elements=[
                {"element_id": u.element_id, "reason": u.detection_reason} for u in model.unknown_components[:20]
            ],
            confidence_sources=sorted(
                {
                    el.source
                    for el in model.interactive_elements
                    if getattr(el, "source", None)
                }
            ),
        )
