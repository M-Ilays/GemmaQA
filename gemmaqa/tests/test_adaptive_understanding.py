"""Adaptive Application Understanding Engine
(app/intelligence/adaptive_understanding/).

Covers: readiness signals (A), stability comparison (B), readiness estimation
and decisions (C), evidence-based state classification (D), observation
confidence (E), contradiction detection (F), transitions (G), memory and
query API (H), controller/RunMemory integration (I), determinism (J),
framework- and application-neutrality (K).

The neutrality tests in group K are the load-bearing ones: they assert by
construction that this engine cannot be reading URLs or framework
signatures, which is the entire justification for its existence.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest

from app.intelligence.adaptive_understanding import AdaptiveApplicationUnderstandingEngine
from app.intelligence.adaptive_understanding.observation_delta import compare, stability_signals
from app.intelligence.adaptive_understanding.readiness_estimator import (
    CRITICALLY_UNREADY_THRESHOLD,
    MAX_OBSERVATION_PASSES,
    READY_THRESHOLD,
    REOBSERVATION_BASE_INTERVAL_MS,
    REOBSERVATION_MAX_INTERVAL_MS,
    decide,
    estimate_observation_confidence,
    estimate_readiness,
    reobservation_interval_ms,
)
from app.intelligence.adaptive_understanding.readiness_signals import extract_signals
from app.intelligence.adaptive_understanding.schemas import (
    APPLICATION_STATES,
    OBSERVATION_DECISIONS,
    POSITIVE_READINESS_SIGNALS,
    READINESS_SIGNAL_KINDS,
    UNDERSTANDING_CONTRADICTION_TYPES,
)
from app.intelligence.adaptive_understanding.state_classifier import classify
from app.intelligence.adaptive_understanding.understanding_contradictions import (
    detect_behaviour_contradiction,
    detect_navigation_contradiction,
    detect_permission_contradiction,
    detect_state_contradiction,
    detect_validation_contradiction,
)

# ---------------------------------------------------------------------------
# Lightweight duck-typed doubles.
#
# Deliberately NOT full CanonicalPageModel instances: the engine is specified
# to depend only on observable shape, so testing it through minimal objects
# proves it is not secretly reaching for a field it should not know about.
# ---------------------------------------------------------------------------


class El:
    def __init__(self, element_id, *, tag="button", role="button", input_type=None, disabled=False, aria_busy=None):
        self.element_id = element_id
        self.tag = tag
        self.role = role
        self.input_type = input_type
        self.disabled = disabled
        self.aria_busy = aria_busy
        self.accessible_name = element_id


class Field:
    def __init__(self, field_type, current_value="", disabled=False):
        self.field_type = field_type
        self.current_value = current_value
        self.disabled = disabled


class Form:
    def __init__(self, form_id, fields=None):
        self.form_id = form_id
        self.fields = fields or []


class Text:
    def __init__(self, text=""):
        self.text = text


class Region:
    def __init__(self, stable_id="r1", region_type="main", attributes=None):
        self.stable_id = stable_id
        self.region_type = region_type
        self.attributes = attributes or {}


class Dialog:
    def __init__(self, stable_id="d1", is_open=True, contained_element_ids=None, accessible_name="dialog"):
        self.stable_id = stable_id
        self.is_open = is_open
        self.contained_element_ids = contained_element_ids or []
        self.accessible_name = accessible_name


class Alert:
    def __init__(self, severity="info", stable_id="a1"):
        self.severity = severity
        self.stable_id = stable_id


class Net:
    def __init__(self, http_status=None, failed=False):
        self.http_status = http_status
        self.failed = failed


class Row:
    def __init__(self, cell_values=None):
        self.cell_values = cell_values or []


class Coll:
    def __init__(self, row_count=0, visible_rows=None, stable_id="c1"):
        self.row_count = row_count
        self.visible_rows = visible_rows or []
        self.stable_id = stable_id


class Model:
    """Minimal stand-in for CanonicalPageModel."""

    def __init__(self, **kw):
        self.url = kw.pop("url", "https://app.example.test/anything")
        self.state_fingerprint = kw.pop("fp", "fp-default")
        self.interactive_elements = []
        self.headings = []
        self.text_blocks = []
        self.forms = []
        self.tables = []
        self.collections = []
        self.dialogs = []
        self.alerts = []
        self.network_evidence = []
        self.regions = []
        self.unknown_components = []
        for key, value in kw.items():
            setattr(self, key, value)


class PS:
    def __init__(self, console_errors=None):
        self.console_errors = console_errors or []


def _ready_model(fp="fp-ready", **kw):
    """A normal, fully-rendered screen."""
    defaults = dict(
        fp=fp,
        interactive_elements=[El("a"), El("b"), El("c")],
        headings=[Text("Title")],
        text_blocks=[Text("Body copy"), Text("More copy")],
    )
    defaults.update(kw)
    return Model(**defaults)


def _empty_model(fp="fp-empty"):
    """The exact failure shape observed live: shell served, nothing rendered."""
    return Model(fp=fp)


# ---------------------------------------------------------------------------
# A. Readiness signals
# ---------------------------------------------------------------------------


class TestReadinessSignals:
    def test_empty_document_produces_a_single_decisive_negative_signal(self):
        signals = extract_signals(_empty_model(), PS())
        kinds = {s.kind for s in signals}
        assert "empty_document" in kinds
        assert all(not s.supports_ready for s in signals)

    def test_rendered_page_produces_positive_signals(self):
        signals = extract_signals(_ready_model(), PS())
        kinds = {s.kind for s in signals if s.supports_ready}
        assert "interactive_surface_present" in kinds
        assert "content_present" in kinds

    def test_aria_busy_is_detected_as_not_ready(self):
        model = _ready_model(regions=[Region(attributes={"aria-busy": "true"})])
        kinds = {s.kind for s in extract_signals(model, PS())}
        assert "busy_region_present" in kinds

    def test_progressbar_role_is_detected_as_not_ready(self):
        model = _ready_model(interactive_elements=[El("p", role="progressbar"), El("a"), El("b")])
        kinds = {s.kind for s in extract_signals(model, PS())}
        assert "progress_indicator_present" in kinds

    def test_repeated_empty_rows_detected_as_placeholder_scaffolding(self):
        model = _ready_model(collections=[Coll(row_count=3, visible_rows=[Row([""]), Row([" "]), Row([""])])])
        kinds = {s.kind for s in extract_signals(model, PS())}
        assert "repeated_empty_containers" in kinds

    def test_populated_rows_are_not_mistaken_for_placeholders(self):
        model = _ready_model(collections=[Coll(row_count=3, visible_rows=[Row(["x"]), Row(["y"]), Row(["z"])])])
        kinds = {s.kind for s in extract_signals(model, PS())}
        assert "repeated_empty_containers" not in kinds

    def test_pending_and_failed_network_are_distinguished(self):
        model = _ready_model(network_evidence=[Net(http_status=None), Net(failed=True)])
        kinds = {s.kind for s in extract_signals(model, PS())}
        assert "pending_network_activity" in kinds
        assert "failed_network_activity" in kinds

    def test_console_errors_recorded_as_readiness_concern(self):
        kinds = {s.kind for s in extract_signals(_ready_model(), PS(console_errors=["boom"]))}
        assert "console_errors_present" in kinds

    def test_every_emitted_signal_kind_is_in_the_closed_vocabulary(self):
        model = _ready_model(
            regions=[Region(attributes={"aria-busy": "true"})],
            network_evidence=[Net(http_status=None), Net(failed=True)],
        )
        for signal in extract_signals(model, PS(console_errors=["e"])):
            assert signal.kind in READINESS_SIGNAL_KINDS

    def test_positive_signal_direction_matches_the_declared_vocabulary(self):
        model = _ready_model()
        for signal in extract_signals(model, PS()):
            if signal.kind in POSITIVE_READINESS_SIGNALS:
                assert signal.supports_ready is True


# ---------------------------------------------------------------------------
# B. Stability comparison
# ---------------------------------------------------------------------------


class TestStability:
    def test_single_observation_is_never_reported_as_stable(self):
        stability = compare(None, _ready_model())
        assert stability.compared is False
        assert stability.stable is False

    def test_identical_consecutive_observations_are_stable(self):
        model = _ready_model()
        stability = compare(model, model)
        assert stability.compared is True
        assert stability.stable is True

    def test_growing_content_is_detected_as_still_changing(self):
        before = Model(fp="fp-x", interactive_elements=[El("a")], headings=[Text("t")])
        after = Model(fp="fp-x", interactive_elements=[El("a"), El("b"), El("c")], headings=[Text("t"), Text("u")])
        stability = compare(before, after)
        assert stability.stable is False
        assert stability.interactive_delta == 2
        assert stability.content_delta == 1

    def test_growth_emits_a_content_growing_signal(self):
        before = Model(fp="fp-x", interactive_elements=[El("a")])
        after = Model(fp="fp-x", interactive_elements=[El("a"), El("b")])
        kinds = {s.kind for s in stability_signals(compare(before, after))}
        assert "dom_still_changing" in kinds
        assert "content_growing" in kinds

    def test_settled_comparison_emits_a_positive_signal(self):
        model = _ready_model()
        signals = stability_signals(compare(model, model))
        assert [s.kind for s in signals] == ["dom_settled"]
        assert signals[0].supports_ready is True

    def test_uncompared_stability_emits_no_signals(self):
        assert stability_signals(compare(None, _ready_model())) == []

    def test_fingerprint_change_alone_marks_instability(self):
        before = _ready_model(fp="fp-1")
        after = _ready_model(fp="fp-2")
        stability = compare(before, after)
        assert stability.fingerprint_changed is True
        assert stability.stable is False


# ---------------------------------------------------------------------------
# C. Readiness estimation and decisions
# ---------------------------------------------------------------------------


class TestReadinessEstimation:
    def test_no_signals_yields_genuine_uncertainty_not_confidence(self):
        assessment = estimate_readiness([])
        assert assessment.readiness_score == 0.5

    def test_empty_document_scores_zero_readiness(self):
        assessment = estimate_readiness(extract_signals(_empty_model(), PS()))
        assert assessment.readiness_score == 0.0
        assert "empty_document" in assessment.blocking_signal_kinds

    def test_rendered_page_scores_above_the_ready_threshold(self):
        assessment = estimate_readiness(extract_signals(_ready_model(), PS()))
        assert assessment.readiness_score >= READY_THRESHOLD

    def test_readiness_score_is_always_bounded(self):
        for model in (_empty_model(), _ready_model(), _ready_model(network_evidence=[Net(failed=True)])):
            assessment = estimate_readiness(extract_signals(model, PS()))
            assert 0.0 <= assessment.readiness_score <= 1.0

    def test_readiness_assessment_is_explainable(self):
        assessment = estimate_readiness(extract_signals(_empty_model(), PS()))
        assert assessment.explanation
        assert assessment.signals

    def test_weak_evidence_decides_reobserve(self):
        readiness = estimate_readiness(extract_signals(_empty_model(), PS()))
        confidence = estimate_observation_confidence(readiness, state_confidence=0.0, compared=False)
        decision, reason = decide(readiness, confidence, observation_pass=1)
        assert decision == "reobserve"
        assert reason

    def test_strong_evidence_decides_accept(self):
        model = _ready_model()
        signals = extract_signals(model, PS()) + stability_signals(compare(model, model))
        readiness = estimate_readiness(signals)
        confidence = estimate_observation_confidence(readiness, state_confidence=0.8, compared=True)
        decision, _ = decide(readiness, confidence, observation_pass=1)
        assert decision == "accept"

    def test_persistently_weak_evidence_escalates_to_accept_degraded_not_an_infinite_loop(self):
        readiness = estimate_readiness(extract_signals(_empty_model(), PS()))
        confidence = estimate_observation_confidence(readiness, state_confidence=0.0, compared=True)
        decisions = [
            decide(readiness, confidence, observation_pass=p)[0]
            for p in range(1, MAX_OBSERVATION_PASSES + 1)
        ]
        assert decisions[-1] == "accept_degraded"
        assert decisions[:-1] == ["reobserve"] * (MAX_OBSERVATION_PASSES - 1)

    def test_settled_emptiness_is_never_read_as_readiness(self):
        """Regression (found during live verification): two identical
        observations of a blank page fired the positive `dom_settled` signal,
        so an application that never rendered scored progressively HIGHER the
        longer it failed to render. Consistent emptiness must count against
        readiness, not for it."""
        empty = _empty_model()
        signals = extract_signals(empty, PS()) + stability_signals(compare(empty, empty))
        assert "dom_settled" in {s.kind for s in signals}  # the raw signal is still produced
        assessment = estimate_readiness(signals)
        assert assessment.readiness_score == 0.0, (
            f"settled emptiness scored {assessment.readiness_score}; it must not raise readiness"
        )
        settled = next(s for s in assessment.signals if s.kind == "dom_settled")
        assert settled.supports_ready is False

    def test_settled_non_emptiness_still_counts_as_readiness(self):
        """The suppression above must apply ONLY when the screen is empty --
        a settled, populated screen is genuinely more trustworthy."""
        ready = _ready_model()
        signals = extract_signals(ready, PS()) + stability_signals(compare(ready, ready))
        assessment = estimate_readiness(signals)
        settled = next(s for s in assessment.signals if s.kind == "dom_settled")
        assert settled.supports_ready is True
        assert assessment.readiness_score >= READY_THRESHOLD

    def test_reobservation_interval_grows_geometrically_and_is_capped(self):
        """Regression (found during live verification): a flat interval closed
        the observation window before a genuinely slow application rendered.
        The interval must grow so slow deployments are still observed, and be
        capped so it cannot grow without bound."""
        intervals = [reobservation_interval_ms(p) for p in range(1, 8)]
        assert intervals[0] == REOBSERVATION_BASE_INTERVAL_MS
        # strictly increasing until the cap, then flat
        for earlier, later in zip(intervals, intervals[1:]):
            assert later >= earlier
        assert max(intervals) == REOBSERVATION_MAX_INTERVAL_MS
        assert all(i <= REOBSERVATION_MAX_INTERVAL_MS for i in intervals)

    def test_cumulative_observation_window_accommodates_a_slow_application(self):
        """The total window across all passes must be long enough to be useful
        on a slow deployment -- several seconds, not a few hundred ms."""
        total = sum(reobservation_interval_ms(p) for p in range(1, MAX_OBSERVATION_PASSES))
        assert total >= 5000, f"cumulative observation window is only {total} ms"

    def test_a_fast_application_pays_no_reobservation_cost(self):
        """The interval is only ever consulted after a `reobserve` decision, so
        an already-rendered screen waits zero milliseconds. Asserted through
        the decision rather than the clock."""
        model = _ready_model()
        signals = extract_signals(model, PS()) + stability_signals(compare(model, model))
        readiness = estimate_readiness(signals)
        confidence = estimate_observation_confidence(readiness, state_confidence=0.9, compared=True)
        decision, _ = decide(readiness, confidence, observation_pass=1)
        assert decision == "accept"

    def test_every_decision_is_in_the_closed_vocabulary(self):
        readiness = estimate_readiness(extract_signals(_empty_model(), PS()))
        confidence = estimate_observation_confidence(readiness, state_confidence=0.0, compared=False)
        for p in range(1, MAX_OBSERVATION_PASSES + 2):
            decision, _ = decide(readiness, confidence, observation_pass=p)
            assert decision in OBSERVATION_DECISIONS


# ---------------------------------------------------------------------------
# D. Evidence-based state classification
# ---------------------------------------------------------------------------


class TestStateClassification:
    def test_password_field_identifies_authentication_without_any_url_hint(self):
        model = _ready_model(
            url="https://app.example.test/totally-unrelated-path",
            forms=[Form("f1", [Field("text"), Field("password")])],
        )
        assessment = classify(model, readiness_score=1.0, page_state=PS())
        assert assessment.primary_state == "authentication"

    def test_authentication_is_identified_even_when_url_suggests_a_dashboard(self):
        """A URL saying 'dashboard' must not override a visible password field."""
        model = _ready_model(
            url="https://app.example.test/dashboard/home/overview",
            forms=[Form("f1", [Field("password")])],
        )
        assert classify(model, readiness_score=1.0, page_state=PS()).primary_state == "authentication"

    def test_server_error_status_identifies_backend_failure(self):
        model = _ready_model(network_evidence=[Net(http_status=500)])
        assert classify(model, readiness_score=1.0, page_state=PS()).primary_state == "backend_failure"

    def test_forbidden_status_identifies_permission_denied(self):
        model = _ready_model(network_evidence=[Net(http_status=403)])
        states = {h.state for h in classify(model, readiness_score=1.0, page_state=PS()).hypotheses}
        assert "permission_denied" in states

    def test_empty_document_is_initializing_not_an_empty_state(self):
        """Misreading an unrendered page as a legitimately-empty dataset would
        be a serious and misleading error."""
        assessment = classify(_empty_model(), readiness_score=0.0, page_state=PS())
        assert assessment.primary_state == "initializing"
        assert "empty_state" not in {h.state for h in assessment.hypotheses}

    def test_collection_with_rows_is_a_collection(self):
        model = _ready_model(collections=[Coll(row_count=5, visible_rows=[Row(["a"])])])
        assert classify(model, readiness_score=1.0, page_state=PS()).primary_state == "collection"

    def test_collection_without_rows_is_an_empty_state(self):
        model = _ready_model(collections=[Coll(row_count=0)])
        assessment = classify(model, readiness_score=1.0, page_state=PS())
        assert assessment.primary_state == "empty_state"

    def test_prepopulated_form_is_edit_and_blank_form_is_create(self):
        edit = _ready_model(forms=[Form("f", [Field("text", "existing value"), Field("text", "another")])])
        create = _ready_model(forms=[Form("f", [Field("text"), Field("text")])])
        edit_states = {h.state for h in classify(edit, readiness_score=1.0, page_state=PS()).hypotheses}
        create_states = {h.state for h in classify(create, readiness_score=1.0, page_state=PS()).hypotheses}
        assert "edit" in edit_states
        assert "create" in create_states

    def test_open_dialog_yields_a_modal_hypothesis(self):
        model = _ready_model(dialogs=[Dialog(contained_element_ids=["x", "y"])])
        states = {h.state for h in classify(model, readiness_score=1.0, page_state=PS()).hypotheses}
        assert "modal" in states

    def test_small_input_free_dialog_is_also_a_confirmation(self):
        model = _ready_model(dialogs=[Dialog(contained_element_ids=["yes", "no"])])
        states = {h.state for h in classify(model, readiness_score=1.0, page_state=PS()).hypotheses}
        assert "confirmation" in states

    def test_error_alert_on_a_form_screen_yields_validation_failure(self):
        model = _ready_model(forms=[Form("f", [Field("text")])], alerts=[Alert(severity="error")])
        states = {h.state for h in classify(model, readiness_score=1.0, page_state=PS()).hypotheses}
        assert "validation_failure" in states

    def test_fully_disabled_surface_yields_feature_disabled(self):
        model = _ready_model(interactive_elements=[El("a", disabled=True), El("b", disabled=True), El("c", disabled=True)])
        states = {h.state for h in classify(model, readiness_score=1.0, page_state=PS()).hypotheses}
        assert "feature_disabled" in states

    def test_console_errors_yield_frontend_failure(self):
        states = {h.state for h in classify(_ready_model(), readiness_score=1.0, page_state=PS(["e1", "e2", "e3"])).hypotheses}
        assert "frontend_failure" in states

    def test_hypotheses_are_ranked_by_confidence(self):
        model = _ready_model(
            forms=[Form("f", [Field("password")])],
            network_evidence=[Net(http_status=500)],
            collections=[Coll(row_count=3, visible_rows=[Row(["a"])])],
        )
        hypotheses = classify(model, readiness_score=1.0, page_state=PS()).hypotheses
        confidences = [h.confidence for h in hypotheses]
        assert confidences == sorted(confidences, reverse=True)

    def test_every_state_is_in_the_closed_vocabulary(self):
        model = _ready_model(
            forms=[Form("f", [Field("password")])],
            dialogs=[Dialog(contained_element_ids=["a"])],
            alerts=[Alert(severity="error")],
            collections=[Coll(row_count=0)],
            network_evidence=[Net(http_status=403), Net(http_status=500)],
        )
        assessment = classify(model, readiness_score=1.0, page_state=PS(["e"]))
        assert assessment.primary_state in APPLICATION_STATES
        for hypothesis in assessment.hypotheses:
            assert hypothesis.state in APPLICATION_STATES

    def test_hypotheses_carry_supporting_evidence(self):
        assessment = classify(_ready_model(forms=[Form("f", [Field("password")])]), readiness_score=1.0, page_state=PS())
        assert all(h.supporting_evidence for h in assessment.hypotheses)

    def test_unrecognised_shape_is_unknown_with_a_stated_reason(self):
        """An unclassifiable screen is a finding, not a crash."""
        model = Model(fp="fp-weird", text_blocks=[Text("just prose")])
        assessment = classify(model, readiness_score=0.7, page_state=PS())
        if assessment.primary_state == "unknown":
            assert assessment.unknown_reason


# ---------------------------------------------------------------------------
# E. Observation confidence
# ---------------------------------------------------------------------------


class TestObservationConfidence:
    def test_a_single_uncompared_observation_can_never_reach_full_confidence(self):
        readiness = estimate_readiness(extract_signals(_ready_model(), PS()))
        confidence = estimate_observation_confidence(readiness, state_confidence=1.0, compared=False)
        assert confidence.value <= 0.85
        assert any("second observation" in m for m in confidence.missing_evidence)

    def test_low_confidence_carries_an_actionable_recommendation(self):
        readiness = estimate_readiness(extract_signals(_empty_model(), PS()))
        confidence = estimate_observation_confidence(readiness, state_confidence=0.0, compared=False)
        assert confidence.value < READY_THRESHOLD
        assert confidence.recommended_next_observation

    def test_blocking_signals_are_surfaced_as_contradicting_evidence(self):
        readiness = estimate_readiness(extract_signals(_empty_model(), PS()))
        confidence = estimate_observation_confidence(readiness, state_confidence=0.0, compared=True)
        assert "empty_document" in confidence.contradicting_evidence

    def test_insufficient_evidence_basis_for_very_weak_observations(self):
        readiness = estimate_readiness(extract_signals(_empty_model(), PS()))
        confidence = estimate_observation_confidence(readiness, state_confidence=0.0, compared=True)
        assert confidence.value < CRITICALLY_UNREADY_THRESHOLD
        assert confidence.basis == "insufficient_evidence"

    def test_readiness_dominates_state_confidence(self):
        """A confidently-identified state on an unrendered screen is still a
        poor basis for planning."""
        weak = estimate_readiness(extract_signals(_empty_model(), PS()))
        confident_state = estimate_observation_confidence(weak, state_confidence=1.0, compared=True)
        assert confident_state.value < READY_THRESHOLD


# ---------------------------------------------------------------------------
# F. Contradiction detection
# ---------------------------------------------------------------------------


class TestContradictions:
    def test_same_screen_understood_differently_is_a_state_contradiction(self):
        c = detect_state_contradiction(
            fingerprint="fp-1", url="https://app.example.test/x",
            previous_state="collection", current_state="empty_state",
            previous_confidence=0.8, current_confidence=0.75,
        )
        assert c is not None
        assert c.contradiction_type == "state_contradiction"
        assert c.previous_observation and c.current_observation

    def test_low_confidence_reclassification_is_learning_not_a_contradiction(self):
        assert detect_state_contradiction(
            fingerprint="fp-1", url="u", previous_state="collection", current_state="detail",
            previous_confidence=0.3, current_confidence=0.9,
        ) is None

    def test_identical_classification_raises_nothing(self):
        assert detect_state_contradiction(
            fingerprint="fp-1", url="u", previous_state="detail", current_state="detail",
            previous_confidence=0.9, current_confidence=0.9,
        ) is None

    def test_route_that_stops_working_is_a_navigation_contradiction(self):
        c = detect_navigation_contradiction(
            url="https://app.example.test/x", previously_reachable=True,
            currently_reachable=False, failure_detail="timeout",
        )
        assert c is not None and c.contradiction_type == "navigation_contradiction"

    def test_still_reachable_route_raises_nothing(self):
        assert detect_navigation_contradiction(
            url="u", previously_reachable=True, currently_reachable=True,
        ) is None

    def test_access_change_within_a_run_is_a_permission_contradiction(self):
        c = detect_permission_contradiction(
            actor_term="session", surface_key="surface-a",
            previously_permitted=True, currently_permitted=False,
        )
        assert c is not None and c.contradiction_type == "permission_contradiction"

    def test_validation_appearing_or_disappearing_is_a_validation_contradiction(self):
        c = detect_validation_contradiction(
            form_key="form-a", previously_validated=True, currently_validated=False,
        )
        assert c is not None and c.contradiction_type == "validation_contradiction"

    def test_same_action_different_destination_is_a_behaviour_contradiction(self):
        c = detect_behaviour_contradiction(
            action_signature="collection|click|open",
            previous_outcome_state="detail", current_outcome_state="permission_denied",
        )
        assert c is not None and c.contradiction_type == "behaviour_contradiction"

    def test_contradiction_ids_are_deterministic(self):
        kwargs = dict(
            fingerprint="fp-1", url="u", previous_state="collection", current_state="detail",
            previous_confidence=0.8, current_confidence=0.8,
        )
        assert detect_state_contradiction(**kwargs).contradiction_id == detect_state_contradiction(**kwargs).contradiction_id

    def test_every_contradiction_type_is_in_the_closed_vocabulary(self):
        produced = [
            detect_state_contradiction(fingerprint="f", url="u", previous_state="a" if False else "collection",
                                       current_state="detail", previous_confidence=0.8, current_confidence=0.8),
            detect_navigation_contradiction(url="u", previously_reachable=True, currently_reachable=False),
            detect_permission_contradiction(actor_term="s", surface_key="k", previously_permitted=True, currently_permitted=False),
            detect_validation_contradiction(form_key="f", previously_validated=True, currently_validated=False),
            detect_behaviour_contradiction(action_signature="s", previous_outcome_state="detail", current_outcome_state="modal"),
        ]
        for c in produced:
            assert c is not None
            assert c.contradiction_type in UNDERSTANDING_CONTRADICTION_TYPES


# ---------------------------------------------------------------------------
# G. Engine assessment, transitions, memory
# ---------------------------------------------------------------------------


class TestEngineAssessment:
    def test_empty_observation_is_assessed_as_not_ready_and_asks_to_reobserve(self):
        """The exact live-observed failure: shell served, DOM empty."""
        engine = AdaptiveApplicationUnderstandingEngine()
        assessment = engine.assess(_empty_model(), page_state=PS(), observation_pass=1)
        assert assessment.decision == "reobserve"
        assert assessment.is_ready is False
        assert assessment.state.primary_state == "initializing"

    def test_rendered_observation_is_accepted_without_extra_passes(self):
        engine = AdaptiveApplicationUnderstandingEngine()
        model = _ready_model()
        assessment = engine.assess(model, page_state=PS(), previous_model=model, observation_pass=1)
        assert assessment.decision == "accept"
        assert assessment.is_ready is True

    def test_assessment_is_recorded_in_memory(self):
        engine = AdaptiveApplicationUnderstandingEngine()
        engine.assess(_ready_model(), page_state=PS())
        assert len(engine.query_engine.assessment_history()) == 1
        assert engine.query_engine.latest_assessment() is not None

    def test_record_false_leaves_memory_untouched(self):
        engine = AdaptiveApplicationUnderstandingEngine()
        engine.assess(_ready_model(), page_state=PS(), record=False)
        assert engine.query_engine.assessment_history() == []

    def test_reclassifying_the_same_fingerprint_raises_a_contradiction(self):
        engine = AdaptiveApplicationUnderstandingEngine()
        collection = _ready_model(fp="same-fp", collections=[Coll(row_count=4, visible_rows=[Row(["a"])])])
        engine.assess(collection, page_state=PS(), previous_model=collection)
        auth = _ready_model(fp="same-fp", forms=[Form("f", [Field("password")])])
        engine.assess(auth, page_state=PS(), previous_model=auth)
        types = {c.contradiction_type for c in engine.query_engine.contradictions()}
        assert "state_contradiction" in types

    def test_transition_is_recorded_between_two_assessments(self):
        engine = AdaptiveApplicationUnderstandingEngine()
        before = engine.assess(_ready_model(fp="fp-a", collections=[Coll(row_count=3, visible_rows=[Row(["a"])])]),
                               page_state=PS(), previous_model=_ready_model(fp="fp-a"))
        after = engine.assess(_ready_model(fp="fp-b", forms=[Form("f", [Field("text", "v"), Field("text", "w")])]),
                              page_state=PS(), previous_model=_ready_model(fp="fp-b"))
        transition = engine.note_transition(before=before, after=after, action_type="click", action_label="open")
        assert transition is not None
        assert transition.from_state == before.state.primary_state
        assert transition.to_state == after.state.primary_state
        assert engine.query_engine.transitions()

    def test_repeating_a_transition_raises_its_confidence_without_duplicating_it(self):
        engine = AdaptiveApplicationUnderstandingEngine()
        model = _ready_model(fp="fp-t")
        a = engine.assess(model, page_state=PS(), previous_model=model)
        b = engine.assess(model, page_state=PS(), previous_model=model)
        first = engine.note_transition(before=a, after=b, action_type="click", action_label="x")
        second = engine.note_transition(before=a, after=b, action_type="click", action_label="x")
        assert len(engine.query_engine.transitions()) == 1
        assert second.observation_count == 2
        assert second.confidence >= first.confidence

    def test_route_becoming_unreachable_is_recorded_as_a_contradiction(self):
        engine = AdaptiveApplicationUnderstandingEngine()
        engine.note_navigation_result("https://app.example.test/x", reachable=True)
        engine.note_navigation_result("https://app.example.test/x", reachable=False, failure_detail="timeout")
        types = {c.contradiction_type for c in engine.query_engine.contradictions()}
        assert "navigation_contradiction" in types

    def test_transition_needs_both_endpoints(self):
        engine = AdaptiveApplicationUnderstandingEngine()
        assert engine.note_transition(before=None, after=None) is None


# ---------------------------------------------------------------------------
# H. Query API and statistics
# ---------------------------------------------------------------------------


class TestQueryAPI:
    def _engine_with_history(self):
        engine = AdaptiveApplicationUnderstandingEngine()
        engine.assess(_empty_model(fp="fp-1"), page_state=PS())
        ready = _ready_model(fp="fp-2")
        engine.assess(ready, page_state=PS(), previous_model=ready)
        return engine

    def test_statistics_totals_match_history(self):
        engine = self._engine_with_history()
        stats = engine.statistics()
        assert stats.total_assessments == len(engine.query_engine.assessment_history())

    def test_statistics_are_bounded_averages(self):
        stats = self._engine_with_history().statistics()
        assert 0.0 <= stats.average_readiness_score <= 1.0
        assert 0.0 <= stats.average_observation_confidence <= 1.0

    def test_low_confidence_and_unknown_queries_partition_correctly(self):
        engine = self._engine_with_history()
        low = engine.query_engine.low_confidence_assessments(0.6)
        assert all(a.observation_confidence.value < 0.6 for a in low)

    def test_assessments_by_state_filters(self):
        engine = self._engine_with_history()
        for a in engine.query_engine.assessments_by_state("initializing"):
            assert a.state.primary_state == "initializing"

    def test_known_states_is_sorted_and_deduplicated(self):
        states = self._engine_with_history().query_engine.known_states()
        assert states == sorted(set(states))

    def test_empty_engine_statistics_do_not_divide_by_zero(self):
        stats = AdaptiveApplicationUnderstandingEngine().statistics()
        assert stats.total_assessments == 0
        assert stats.average_readiness_score == 0.0


# ---------------------------------------------------------------------------
# I. Controller / RunMemory integration
# ---------------------------------------------------------------------------


class TestMemoryAndController:
    def _run_memory(self):
        from app.agent.memory import RunMemory
        return RunMemory(run_id="r1", start_url="https://example.test")

    def test_degrades_gracefully_with_no_engine_attached(self):
        mem = self._run_memory()
        assert mem.latest_understanding() is None
        assert mem.understanding_history() == []
        assert mem.understanding_statistics() is None
        assert mem.understanding_contradictions() == []
        assert mem.observed_state_transitions() == []
        assert mem.known_application_states() == []
        assert mem.low_confidence_observations() == []
        assert mem.unknown_state_observations() == []

    def test_memory_snapshot_includes_understanding_block_when_attached(self):
        mem = self._run_memory()
        mem.understanding_engine = AdaptiveApplicationUnderstandingEngine()
        mem.understanding_engine.assess(_ready_model(), page_state=PS())
        snap = mem.memory_snapshot()
        assert "adaptive_understanding" in snap
        assert snap["adaptive_understanding"]["total_assessments"] == 1

    def test_controller_attaches_the_engine_unconditionally(self):
        """This engine replaces an assumption every run previously made
        silently, so there is no flag to opt into."""
        import inspect
        from app.agent import controller as controller_module

        source = inspect.getsource(controller_module)
        assert "self.memory.understanding_engine = AdaptiveApplicationUnderstandingEngine()" in source

    def test_controller_observation_loop_is_evidence_gated_not_time_gated(self):
        import inspect
        from app.agent import controller as controller_module

        source = inspect.getsource(controller_module)
        # The loop must exit on the engine's decision, not on a timer.
        assert 'if assessment.decision != "reobserve":' in source
        assert "engine.note_reobservation()" in source

    def test_transition_recording_hook_runs_after_investigation_hook(self):
        import inspect
        from app.agent import controller as controller_module

        source = inspect.getsource(controller_module)
        assert "self._record_state_transition(" in source
        assert source.index("self._run_autonomous_investigation(") < source.index("self._record_state_transition(")

    def test_transition_hook_never_raises_on_bad_input(self):
        import types
        from app.agent.controller import AgentController

        fake_self = types.SimpleNamespace(
            memory=types.SimpleNamespace(understanding_engine=object()),
            _understanding_before_action=object(),
            _last_understanding=object(),
        )
        AgentController._record_state_transition(fake_self, action=object(), result=object())

    def test_engine_package_never_touches_the_browser_or_a_model(self):
        package_dir = BACKEND / "app" / "intelligence" / "adaptive_understanding"
        for path in package_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "import playwright" not in text.lower()
            assert "playwright.sync_api" not in text.lower()
            assert "page.locator" not in text.lower()
            assert "BrowserAdapter(" not in text
            assert "ActionExecutor(" not in text
            assert "GemmaProvider" not in text


# ---------------------------------------------------------------------------
# J. Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_repeated_assessment_of_identical_input_is_identical(self):
        model = _ready_model()
        a = AdaptiveApplicationUnderstandingEngine().assess(model, page_state=PS(), previous_model=model, record=False)
        b = AdaptiveApplicationUnderstandingEngine().assess(model, page_state=PS(), previous_model=model, record=False)
        assert a.readiness.readiness_score == b.readiness.readiness_score
        assert a.state.primary_state == b.state.primary_state
        assert a.observation_confidence.value == b.observation_confidence.value
        assert a.decision == b.decision

    def test_hypothesis_ordering_is_stable_across_engines(self):
        model = _ready_model(
            forms=[Form("f", [Field("password")])],
            collections=[Coll(row_count=2, visible_rows=[Row(["a"])])],
            dialogs=[Dialog(contained_element_ids=["a", "b"])],
        )
        first = [h.state for h in classify(model, readiness_score=1.0, page_state=PS()).hypotheses]
        second = [h.state for h in classify(model, readiness_score=1.0, page_state=PS()).hypotheses]
        assert first == second

    def test_assessment_ids_are_derived_not_random(self):
        model = _ready_model(fp="stable-fp")
        a = AdaptiveApplicationUnderstandingEngine().assess(model, page_state=PS(), observation_pass=1, record=False)
        b = AdaptiveApplicationUnderstandingEngine().assess(model, page_state=PS(), observation_pass=1, record=False)
        assert a.assessment_id == b.assessment_id
        assert "stable-fp" in a.assessment_id

    def test_version_does_not_churn_when_nothing_changed(self):
        engine = AdaptiveApplicationUnderstandingEngine()
        model = _ready_model(fp="fp-same")
        engine.assess(model, page_state=PS(), previous_model=model)
        version_after_first = engine.memory.understanding_version
        engine.assess(model, page_state=PS(), previous_model=model)
        assert engine.memory.understanding_version == version_after_first


# ---------------------------------------------------------------------------
# K. Framework- and application-neutrality (the load-bearing constraints)
# ---------------------------------------------------------------------------

FORBIDDEN_FRAMEWORK_WORDS = {
    "react", "vue", "angular", "svelte", "ember", "backbone", "jquery",
    "nextjs", "nuxt", "htmx", "alpine", "webpack", "vite", "spa",
}

FORBIDDEN_BUSINESS_WORDS = {
    "invoice", "customer", "job", "ticket", "order", "product", "cart", "checkout",
    "employee", "vehicle", "haulvana", "serviceflow", "saucedemo", "insightboard",
    "orangehrm",
}


def _non_docstring_string_constants(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstring_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                docstring_ids.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_ids
    ]


class TestNeutrality:
    def test_no_framework_vocabulary_in_engine_logic(self):
        package_dir = BACKEND / "app" / "intelligence" / "adaptive_understanding"
        offenders = []
        for path in sorted(package_dir.glob("*.py")):
            for value in _non_docstring_string_constants(path):
                hit = set(re.findall(r"[a-z]+", value.lower())) & FORBIDDEN_FRAMEWORK_WORDS
                if hit:
                    offenders.append((path.name, value, hit))
        assert not offenders, f"Framework-specific vocabulary found: {offenders}"

    def test_no_business_vocabulary_in_engine_logic(self):
        package_dir = BACKEND / "app" / "intelligence" / "adaptive_understanding"
        offenders = []
        for path in sorted(package_dir.glob("*.py")):
            for value in _non_docstring_string_constants(path):
                hit = set(re.findall(r"[a-z]+", value.lower())) & FORBIDDEN_BUSINESS_WORDS
                if hit:
                    offenders.append((path.name, value, hit))
        assert not offenders, f"Application-specific vocabulary found: {offenders}"

    def test_state_classifier_never_reads_the_url(self):
        """The classifier must not consult the URL at all -- not the path, the
        query, or the host. This is asserted structurally rather than
        behaviourally so it cannot regress unnoticed."""
        source = (BACKEND / "app" / "intelligence" / "adaptive_understanding" / "state_classifier.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstring_ids = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                    docstring_ids.add(id(body[0].value))
        # No attribute access to `.url`, and no urlparse import, outside docstrings.
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "url", "state_classifier must not read a url attribute"
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_ids:
                assert node.value != "url", "state_classifier must not look up a 'url' key"
        assert "urlparse" not in source
        assert "urllib" not in source

    def test_classification_is_invariant_to_the_url(self):
        """Behavioural companion to the structural test above: the same
        structure classified identically regardless of the URL it was served
        from -- including URLs that would fool a path-matching classifier."""
        misleading_urls = [
            "https://app.example.test/dashboard",
            "https://app.example.test/auth/login",
            "https://app.example.test/settings/new/edit",
            "https://other.example.test/",
            "https://app.example.test/",
        ]
        results = {
            classify(
                _ready_model(url=u, collections=[Coll(row_count=3, visible_rows=[Row(["a"])])]),
                readiness_score=1.0,
                page_state=PS(),
            ).primary_state
            for u in misleading_urls
        }
        assert len(results) == 1, f"classification varied with URL: {results}"
        assert results == {"collection"}

    def test_readiness_is_invariant_to_the_url(self):
        scores = {
            estimate_readiness(extract_signals(_ready_model(url=u), PS())).readiness_score
            for u in ["https://a.example.test/x", "https://b.example.test/y/z?q=1"]
        }
        assert len(scores) == 1

    def test_engine_never_sleeps_or_sets_a_timeout(self):
        """Readiness must be decided by evidence. The engine itself must
        contain no sleeping or timing primitives at all."""
        package_dir = BACKEND / "app" / "intelligence" / "adaptive_understanding"
        for path in package_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    assert node.attr not in {"sleep", "wait_for_timeout"}, f"{path.name} must not sleep"
                if isinstance(node, ast.Name):
                    assert node.id != "sleep", f"{path.name} must not sleep"
            source = path.read_text(encoding="utf-8")
            assert "import time" not in source
            assert "asyncio.sleep" not in source
