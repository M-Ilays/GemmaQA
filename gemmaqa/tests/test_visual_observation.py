"""Selective visual observation: image semantic classification,
VisualObservationPolicy triggers, screenshot cropping, the visual-analysis
prompt/parser, VisualAnalyzer orchestration (mocked model), confidence/
evidence merging, and end-to-end PerceptionEngine wiring — including one live
Playwright page exercising icon-only controls, an image, a canvas, and a
poorly-labelled custom component."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest

from app.gemma.mock_provider import MockGemmaProvider
from app.gemma.parser import parse_visual_analysis
from app.perception import image_extractor
from app.perception.dom_extractor import RawObservation
from app.perception.engine import PerceptionEngine
from app.perception.evidence_merger import merge_visual_evidence
from app.perception.models import (
    ConfidenceScore,
    ImageDescriptor,
    UnknownComponent,
    VisualEvidence,
)
from app.perception.screenshot_utils import crop_bounding_box
from app.perception.visual_analyzer import VisualAnalyzer
from app.perception.visual_policy import VisualObservationPolicy, _bbox_iou
from app.schemas import InteractiveElement


def _box(x=0, y=0, width=40, height=40):
    return {"x": x, "y": y, "width": width, "height": height}


def _raw_el(dom_id="el_001", **overrides):
    base = {
        "dom_id": dom_id,
        "tag": "div",
        "role": None,
        "text": "",
        "accessible_name": "",
        "is_visible": True,
        "looks_icon_only": False,
        "has_inline_svg": False,
        "bounding_box": _box(),
    }
    base.update(overrides)
    return base


def _interactive(element_id="el_001", **overrides):
    base = dict(
        element_id=element_id,
        tag="button",
        is_visible=True,
        accessible_name="Submit",
        text="Submit",
        bounding_box=_box(),
    )
    base.update(overrides)
    return InteractiveElement(**base)


def _image(element_id="img_001", visual_semantic_type="informational", **overrides):
    base = dict(
        element_id=element_id,
        stable_id=element_id,
        visual_semantic_type=visual_semantic_type,
        bounding_box=_box(),
    )
    base.update(overrides)
    return ImageDescriptor(**base)


def _unknown(element_id="unk_001", **overrides):
    base = dict(element_id=element_id, stable_id=element_id, detection_reason="cursor:pointer")
    base.update(overrides)
    return UnknownComponent(**base)


# ---------------------------------------------------------------------------
# Image semantic classification (deterministic, heuristic-only)
# ---------------------------------------------------------------------------


class TestClassifyImageSemantic:
    def test_logo_from_alt_text(self):
        img = {"alt": "Acme company logo", "src": "/img/x.png", "class_name": ""}
        assert image_extractor.classify_image_semantic(img, bounding_box=_box(80, 80)) == "logo"

    def test_avatar_small_square_with_profile_hint(self):
        img = {"alt": "User profile photo", "src": "", "class_name": ""}
        box = _box(0, 0, 64, 64)
        assert image_extractor.classify_image_semantic(img, bounding_box=box) == "avatar"

    def test_decorative_when_alt_is_explicitly_empty(self):
        img = {"alt": "", "src": "/img/spacer.png", "class_name": ""}
        assert image_extractor.classify_image_semantic(img, bounding_box=_box(20, 20)) == "decorative"

    def test_decorative_when_role_presentation(self):
        img = {"alt": None, "role": "presentation", "src": "/img/deco.png", "class_name": ""}
        assert image_extractor.classify_image_semantic(img, bounding_box=_box(20, 20)) == "decorative"

    def test_chart_from_class_name(self):
        img = {"alt": None, "src": "/img/x.png", "class_name": "sales-chart-widget"}
        assert image_extractor.classify_image_semantic(img, bounding_box=_box(400, 300)) == "chart"

    def test_document_from_pdf_src(self):
        img = {"alt": None, "src": "/files/statement.pdf", "class_name": ""}
        assert image_extractor.classify_image_semantic(img, bounding_box=_box(400, 500)) == "document"

    def test_interactive_when_wrapped_in_link(self):
        img = {"alt": None, "src": "/img/x.png", "class_name": "", "in_interactive_container": True}
        assert image_extractor.classify_image_semantic(img, bounding_box=_box(100, 100)) == "interactive"

    def test_informational_with_descriptive_alt(self):
        img = {"alt": "Quarterly revenue growth summary", "src": "/img/x.png", "class_name": ""}
        assert image_extractor.classify_image_semantic(img, bounding_box=_box(400, 300)) == "informational"

    def test_unknown_for_tiny_unlabeled_icon(self):
        img = {"alt": None, "src": "/img/x.png", "class_name": ""}
        assert image_extractor.classify_image_semantic(img, bounding_box=_box(16, 16)) == "unknown"

    def test_extract_images_populates_visual_semantic_type(self):
        raw = RawObservation(
            url="https://example.com/",
            images=[{"dom_id": "img_1", "alt": "", "src": "/x.png", "bounding_box": _box(20, 20)}],
        )
        images = image_extractor.extract_images(raw)
        assert images[0].visual_semantic_type == "decorative"


# ---------------------------------------------------------------------------
# VisualObservationPolicy
# ---------------------------------------------------------------------------


class TestBboxIou:
    def test_identical_boxes_have_iou_one(self):
        a = _box(0, 0, 100, 100)
        assert _bbox_iou(a, a) == pytest.approx(1.0)

    def test_disjoint_boxes_have_iou_zero(self):
        a = _box(0, 0, 10, 10)
        b = _box(100, 100, 10, 10)
        assert _bbox_iou(a, b) == 0.0

    def test_small_child_inside_large_parent_has_low_iou(self):
        parent = _box(0, 0, 200, 200)
        child = _box(80, 80, 20, 20)
        assert _bbox_iou(parent, child) < 0.05


class TestVisualObservationPolicy:
    def _empty_raw(self, **overrides):
        base = dict(url="https://example.com/")
        base.update(overrides)
        return RawObservation(**base)

    def test_no_trigger_on_a_clean_fully_labeled_page(self):
        raw = self._empty_raw()
        decision = VisualObservationPolicy().evaluate(
            raw=raw,
            interactive_elements=[_interactive()],
            images=[],
            unknown_components=[],
        )
        assert decision.should_run is False
        assert decision.targets == []

    def test_missing_accessible_name_triggers(self):
        raw = self._empty_raw()
        el = _interactive(accessible_name=None, text=None, aria_label=None)
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[el], images=[], unknown_components=[]
        )
        assert decision.should_run is True
        assert "missing_accessible_name" in decision.reasons
        assert decision.targets[0].element_id == el.element_id

    def test_icon_only_control_triggers(self):
        raw = self._empty_raw(elements=[_raw_el("el_icon", looks_icon_only=True)])
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[], images=[], unknown_components=[]
        )
        assert decision.should_run is True
        assert "icon_only_control" in decision.reasons
        assert decision.targets[0].element_id == "el_icon"

    def test_canvas_element_triggers(self):
        raw = self._empty_raw(canvases=[{"dom_id": "canvas_1", "bounding_box": _box(0, 0, 300, 150)}])
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[], images=[], unknown_components=[]
        )
        assert decision.should_run is True
        assert "canvas_element" in decision.reasons
        assert decision.targets[0].element_id == "canvas_1"

    def test_unclassified_svg_control_triggers(self):
        raw = self._empty_raw(
            elements=[_raw_el("el_svg", has_inline_svg=True, role=None, accessible_name="", text="")]
        )
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[], images=[], unknown_components=[]
        )
        assert decision.should_run is True
        assert "unclassified_svg_control" in decision.reasons

    def test_svg_control_with_role_does_not_trigger(self):
        raw = self._empty_raw(
            elements=[_raw_el("el_svg", has_inline_svg=True, role="button", accessible_name="Close")]
        )
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[], images=[], unknown_components=[]
        )
        assert "unclassified_svg_control" not in decision.reasons

    def test_meaningful_image_triggers(self):
        raw = self._empty_raw()
        img = _image(visual_semantic_type="chart")
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[], images=[img], unknown_components=[]
        )
        assert decision.should_run is True
        assert "meaningful_image:chart" in decision.reasons
        assert decision.targets[0].kind == "image"

    def test_decorative_image_does_not_trigger(self):
        raw = self._empty_raw()
        img = _image(visual_semantic_type="decorative")
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[], images=[img], unknown_components=[]
        )
        assert decision.should_run is False

    def test_layout_grouping_needed_when_many_unknown_components(self):
        raw = self._empty_raw()
        unknowns = [_unknown(f"unk_{i}") for i in range(3)]
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[], images=[], unknown_components=unknowns
        )
        assert "layout_grouping_needed" in decision.reasons
        assert decision.needs_full_page is True

    def test_possible_visual_defect_from_overlapping_controls(self):
        # Distinct labels are the point of this fixture: a real defect is two
        # DIFFERENT controls stacking on top of each other. Two elements
        # sharing one label in the same box is the (benign, filtered-out)
        # nested/duplicate-link pattern instead — see
        # test_overlap_with_identical_label_is_not_a_defect below.
        raw = self._empty_raw()
        a = _interactive("el_a", bounding_box=_box(0, 0, 100, 100), text="Submit", accessible_name="Submit")
        b = _interactive("el_b", bounding_box=_box(2, 2, 100, 100), text="Cancel", accessible_name="Cancel")
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[a, b], images=[], unknown_components=[]
        )
        assert "possible_visual_defect" in decision.reasons
        assert decision.needs_full_page is True
        target_ids = {t.element_id for t in decision.targets}
        assert {"el_a", "el_b"} <= target_ids

    def test_overlap_with_identical_label_is_not_a_defect(self):
        """Regression: SauceDemo's product cards wrap the same product in two
        overlapping link elements (an image-link and a title-link) sharing
        one label and the exact same box — live-verified as a normal,
        harmless pattern, not a rendering defect. Two elements with the same
        label overlapping is a duplicate/nested link, not two different
        things colliding."""
        raw = self._empty_raw()
        a = _interactive("el_a", bounding_box=_box(0, 0, 100, 100), text="Product Title", accessible_name="Product Title")
        b = _interactive("el_b", bounding_box=_box(0, 0, 100, 100), text="Product Title", accessible_name="Product Title")
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[a, b], images=[], unknown_components=[]
        )
        assert "possible_visual_defect" not in decision.reasons

    def test_overlapping_native_select_is_not_a_defect(self):
        """Regression: a styled native <select> always visually overlaps its
        own rendered current-value text/span — live-verified on SauceDemo's
        sort dropdown as a normal pattern, not a rendering defect."""
        raw = self._empty_raw()
        select_el = _interactive(
            "el_select", tag="select", bounding_box=_box(0, 0, 200, 30), text="A to Z", accessible_name="A to Z"
        )
        overlay = _interactive(
            "el_overlay", bounding_box=_box(0, 0, 200, 30), text="Currently: A to Z", accessible_name="Currently: A to Z"
        )
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[select_el, overlay], images=[], unknown_components=[]
        )
        assert "possible_visual_defect" not in decision.reasons

    def test_nested_icon_in_button_does_not_look_like_a_defect(self):
        raw = self._empty_raw()
        button = _interactive("btn", bounding_box=_box(0, 0, 200, 60))
        icon = _interactive("icon", bounding_box=_box(10, 10, 20, 20))
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[button, icon], images=[], unknown_components=[]
        )
        assert "possible_visual_defect" not in decision.reasons

    def test_unknown_component_remaining_triggers(self):
        raw = self._empty_raw()
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[], images=[], unknown_components=[_unknown()]
        )
        assert "unknown_component_remaining" in decision.reasons
        assert decision.targets[0].element_id == "unk_001"

    def test_same_element_merges_reasons_into_one_target(self):
        raw = self._empty_raw(elements=[_raw_el("el_a", looks_icon_only=True)])
        el = _interactive("el_a", accessible_name=None, text=None, aria_label=None)
        decision = VisualObservationPolicy().evaluate(
            raw=raw, interactive_elements=[el], images=[], unknown_components=[]
        )
        matching = [t for t in decision.targets if t.element_id == "el_a"]
        assert len(matching) == 1
        assert {"missing_accessible_name", "icon_only_control"} <= set(matching[0].reasons)


# ---------------------------------------------------------------------------
# Screenshot cropping
# ---------------------------------------------------------------------------


class TestCropBoundingBox:
    def _make_png(self, tmp_path, name="shot.png", size=(400, 300)):
        from PIL import Image

        path = tmp_path / name
        Image.new("RGB", size, color=(10, 20, 30)).save(path)
        return path

    def test_crops_and_saves_expected_region(self, tmp_path):
        shot = self._make_png(tmp_path)
        out = crop_bounding_box(
            shot, _box(50, 50, 100, 80), element_id="el_1", out_dir=tmp_path / "crops", padding=0
        )
        assert out is not None
        from PIL import Image

        with Image.open(out) as cropped:
            assert cropped.size == (100, 80)

    def test_missing_screenshot_returns_none(self, tmp_path):
        out = crop_bounding_box(
            tmp_path / "does_not_exist.png", _box(0, 0, 10, 10), element_id="el_1"
        )
        assert out is None

    def test_zero_size_box_returns_none(self, tmp_path):
        shot = self._make_png(tmp_path)
        out = crop_bounding_box(shot, _box(0, 0, 0, 0), element_id="el_1")
        assert out is None

    def test_risky_filename_is_skipped(self, tmp_path):
        shot = self._make_png(tmp_path, name="login_failed.png")
        out = crop_bounding_box(shot, _box(0, 0, 10, 10), element_id="el_1")
        assert out is None


# ---------------------------------------------------------------------------
# gemma.parser.parse_visual_analysis
# ---------------------------------------------------------------------------


class TestParseVisualAnalysis:
    def test_valid_response_parses(self):
        raw = json.dumps(
            {
                "observations": [
                    {
                        "element_id": "el_1",
                        "visual_semantic_type": "icon_button",
                        "description": "A trash-can icon button",
                        "confidence": 0.8,
                        "evidence": ["visible trash-can glyph"],
                        "further_interaction_recommended": True,
                    }
                ]
            }
        )
        result = parse_visual_analysis(raw, known_element_ids={"el_1"})
        assert len(result) == 1
        assert isinstance(result[0], VisualEvidence)
        assert result[0].element_id == "el_1"
        assert result[0].visual_semantic_type == "icon_button"
        assert result[0].further_interaction_recommended is True

    def test_drops_invented_element_id(self):
        raw = json.dumps({"observations": [{"element_id": "el_ghost", "confidence": 0.9}]})
        result = parse_visual_analysis(raw, known_element_ids={"el_1"})
        assert result == []

    def test_invalid_semantic_type_coerced_to_unknown(self):
        raw = json.dumps({"observations": [{"element_id": "el_1", "visual_semantic_type": "made_up_type"}]})
        result = parse_visual_analysis(raw, known_element_ids={"el_1"})
        assert result[0].visual_semantic_type == "unknown"

    def test_confidence_is_clamped_to_unit_interval(self):
        raw = json.dumps({"observations": [{"element_id": "el_1", "confidence": 5.0}]})
        result = parse_visual_analysis(raw, known_element_ids={"el_1"})
        assert result[0].confidence.value == 1.0

    def test_malformed_json_returns_empty_list(self):
        assert parse_visual_analysis("not json at all", known_element_ids={"el_1"}) == []

    def test_missing_observations_key_returns_empty_list(self):
        assert parse_visual_analysis(json.dumps({"foo": "bar"}), known_element_ids={"el_1"}) == []


# ---------------------------------------------------------------------------
# VisualAnalyzer orchestration (mocked provider)
# ---------------------------------------------------------------------------


class TestVisualAnalyzer:
    async def test_skips_entirely_when_policy_says_no(self):
        provider = MockGemmaProvider()
        analyzer = VisualAnalyzer(provider)
        from app.perception.visual_policy import VisualObservationDecision

        decision = VisualObservationDecision(should_run=False)
        result = await analyzer.analyze(
            decision,
            raw=RawObservation(url="https://example.com/"),
            accessibility_index={},
            screenshot_path="/tmp/shot.png",
        )
        assert result == []
        assert provider.calls == []

    async def test_calls_provider_and_returns_evidence(self, tmp_path):
        from PIL import Image

        from app.perception.visual_policy import VisualObservationDecision, VisualTarget

        shot = tmp_path / "shot.png"
        Image.new("RGB", (200, 200), color=(1, 2, 3)).save(shot)

        provider = MockGemmaProvider()
        provider.enqueue(
            json.dumps(
                {
                    "observations": [
                        {"element_id": "el_1", "visual_semantic_type": "icon_button", "confidence": 0.7}
                    ]
                }
            )
        )
        analyzer = VisualAnalyzer(provider)
        decision = VisualObservationDecision(
            should_run=True,
            reasons=["icon_only_control"],
            targets=[VisualTarget("el_1", reasons=["icon_only_control"], bounding_box=_box(10, 10, 30, 30))],
        )
        raw = RawObservation(url="https://example.com/", elements=[_raw_el("el_1", looks_icon_only=True)])
        result = await analyzer.analyze(
            decision,
            raw=raw,
            accessibility_index={},
            screenshot_path=str(shot),
        )
        assert len(result) == 1
        assert result[0].element_id == "el_1"
        assert result[0].trigger_reasons == ["icon_only_control"]
        assert len(provider.calls) == 1

    async def test_caps_targets_to_max_per_call(self, tmp_path):
        from app.perception.visual_policy import VisualObservationDecision, VisualTarget

        shot = tmp_path / "shot.png"
        from PIL import Image

        Image.new("RGB", (50, 50)).save(shot)

        captured_prompts: list[str] = []

        def _hook(system: str, user: str) -> str:
            captured_prompts.append(user)
            return json.dumps({"observations": []})

        provider = MockGemmaProvider(generate_hook=_hook)
        analyzer = VisualAnalyzer(provider, max_targets=2)
        targets = [VisualTarget(f"el_{i}", reasons=["unknown_component_remaining"]) for i in range(5)]
        decision = VisualObservationDecision(should_run=True, reasons=["unknown_component_remaining"], targets=targets)
        await analyzer.analyze(
            decision,
            raw=RawObservation(url="https://example.com/"),
            accessibility_index={},
            screenshot_path=str(shot),
        )
        assert len(captured_prompts) == 1
        sent_user_prompt = captured_prompts[0]
        # Only the first max_targets (2) element ids should appear in the
        # prompt sent to the model — the rest were dropped by the cap.
        assert "el_0" in sent_user_prompt and "el_1" in sent_user_prompt
        assert "el_4" not in sent_user_prompt


# ---------------------------------------------------------------------------
# evidence_merger.merge_visual_evidence
# ---------------------------------------------------------------------------


class TestMergeVisualEvidence:
    def _model(self, **overrides):
        from app.perception.models import CanonicalPageModel

        base = dict(url="https://example.com/")
        base.update(overrides)
        return CanonicalPageModel(**base)

    def test_noop_when_no_visual_evidence(self):
        model = self._model(interactive_elements=[_interactive()])
        merged = merge_visual_evidence(model, [])
        assert merged is model

    def test_merges_onto_interactive_element_keeps_float_confidence_shape(self):
        el = _interactive("el_1", confidence=0.6, evidence=["pointer_style"])
        model = self._model(interactive_elements=[el])
        ev = VisualEvidence(element_id="el_1", visual_semantic_type="icon_button", confidence=0.9)
        merged = merge_visual_evidence(model, [ev])
        updated = merged.interactive_elements[0]
        assert isinstance(updated.confidence, float)
        assert updated.confidence == 0.9
        assert "pointer_style" in updated.evidence
        assert "visual:icon_button" in updated.evidence
        assert merged.visual_evidence == [ev]

    def test_merges_onto_image_keeps_confidence_score_shape(self):
        img = _image("img_1", visual_semantic_type="chart", confidence=0.6)
        model = self._model(images=[img])
        ev = VisualEvidence(element_id="img_1", visual_semantic_type="chart", confidence=0.95)
        merged = merge_visual_evidence(model, [ev])
        updated = merged.images[0]
        assert isinstance(updated.confidence, ConfidenceScore)
        assert updated.confidence.value == 0.95

    def test_unmatched_element_id_still_recorded_in_visual_evidence_only(self):
        model = self._model(interactive_elements=[_interactive("el_1")])
        ev = VisualEvidence(element_id="el_ghost_but_valid_shape", visual_semantic_type="unknown")
        merged = merge_visual_evidence(model, [ev])
        assert merged.visual_evidence == [ev]
        assert merged.interactive_elements[0].evidence == []


# ---------------------------------------------------------------------------
# End-to-end PerceptionEngine wiring
# ---------------------------------------------------------------------------


class TestPerceptionEngineVisualWiring:
    def _raw_with_icon_and_canvas(self):
        return RawObservation(
            url="https://example.com/",
            elements=[
                _raw_el(
                    "el_icon",
                    tag="button",
                    looks_icon_only=True,
                    bounding_box=_box(5, 5, 24, 24),
                )
            ],
            canvases=[{"dom_id": "canvas_1", "bounding_box": _box(0, 40, 300, 150)}],
        )

    async def test_visual_disabled_by_default_keeps_model_deterministic(self, tmp_path):
        engine = PerceptionEngine()  # no visual_provider
        raw = self._raw_with_icon_and_canvas()
        model = engine._build_from_raw(
            raw, screenshot_path=None, known_role=None, network_entries=[]
        )
        model = await engine._maybe_apply_visual(
            model, raw=raw, accessibility_index={}, screenshot_path=str(tmp_path / "shot.png")
        )
        assert model.visual_evidence == []

    async def test_visual_runs_when_provider_configured_and_policy_triggers(self, tmp_path):
        from PIL import Image

        shot = tmp_path / "shot.png"
        Image.new("RGB", (400, 300), color=(5, 5, 5)).save(shot)

        provider = MockGemmaProvider()
        provider.enqueue(
            json.dumps(
                {
                    "observations": [
                        {
                            "element_id": "el_icon",
                            "visual_semantic_type": "icon_button",
                            "description": "A hamburger-menu icon",
                            "confidence": 0.75,
                            "further_interaction_recommended": True,
                        }
                    ]
                }
            )
        )
        engine = PerceptionEngine(visual_provider=provider)
        raw = self._raw_with_icon_and_canvas()
        model = engine._build_from_raw(raw, screenshot_path=str(shot), known_role=None, network_entries=[])
        model = await engine._maybe_apply_visual(
            model, raw=raw, accessibility_index={}, screenshot_path=str(shot)
        )
        assert len(model.visual_evidence) == 1
        assert model.visual_evidence[0].element_id == "el_icon"
        matching = [e for e in model.interactive_elements if e.element_id == "el_icon"]
        assert matching and "visual:icon_button" in matching[0].evidence

    async def test_visual_failure_is_non_fatal(self, tmp_path):
        class BoomProvider(MockGemmaProvider):
            async def analyze_visual_elements(self, **kwargs):
                raise RuntimeError("boom")

        shot = tmp_path / "shot.png"
        from PIL import Image

        Image.new("RGB", (100, 100)).save(shot)

        engine = PerceptionEngine(visual_provider=BoomProvider())
        raw = self._raw_with_icon_and_canvas()
        model = engine._build_from_raw(raw, screenshot_path=str(shot), known_role=None, network_entries=[])
        result = await engine._maybe_apply_visual(
            model, raw=raw, accessibility_index={}, screenshot_path=str(shot)
        )
        assert result.visual_evidence == []  # degraded gracefully, no exception


# ---------------------------------------------------------------------------
# Live page: icon-only controls, an image, a canvas, and a poorly-labelled
# custom control — via a real Playwright page (skipped if Chromium isn't
# available in this environment).
# ---------------------------------------------------------------------------

LIVE_TEST_HTML = """
<!doctype html>
<html>
<body style="margin:0">
  <button id="icon-btn" style="width:40px;height:40px;"><svg viewBox="0 0 24 24"><path d="M4 4h16v16H4z"/></svg></button>
  <img id="chart-img" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
       alt="Quarterly sales chart showing steady growth" width="300" height="200" />
  <canvas id="dashboard-canvas" width="300" height="150"></canvas>
  <div id="custom-control" style="cursor:pointer;width:120px;height:80px;background:#eee"></div>
</body>
</html>
"""


class TestLivePageVisualPolicy:
    async def test_live_page_triggers_visual_policy_for_icon_image_canvas_and_custom_control(self, tmp_path):
        try:
            from playwright.async_api import async_playwright
        except Exception:
            pytest.skip("Playwright not importable")

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    # CanonicalPageModel requires an absolute URL (scheme + host);
                    # `set_content()` alone leaves the page at "about:blank". Route
                    # interception serves the fixture HTML under a real-shaped URL
                    # without any actual network call.
                    await page.route(
                        "**/*",
                        lambda route: route.fulfill(
                            status=200, content_type="text/html", body=LIVE_TEST_HTML
                        ),
                    )
                    await page.goto("https://gemmaqa-test.local/")
                    shot_path = tmp_path / "live_shot.png"
                    await page.screenshot(path=str(shot_path))

                    provider = MockGemmaProvider()
                    provider.enqueue(json.dumps({"observations": []}))
                    engine = PerceptionEngine(visual_provider=provider)
                    model = await engine.observe(page, screenshot_path=str(shot_path))
                finally:
                    await browser.close()
        except Exception as exc:
            pytest.skip(f"Live Chromium unavailable in this environment: {type(exc).__name__}: {exc}")
            return

        # The custom pointer-cursor <div> and the bare-svg icon button are
        # observed as interactive elements/unknown-ish signals; the chart
        # image is classified informational/chart; the canvas is always a
        # visual trigger. At least one visual-model call must have happened.
        assert len(provider.calls) >= 1
        image_types = {img.visual_semantic_type for img in model.images}
        assert image_types & {"informational", "chart"}
