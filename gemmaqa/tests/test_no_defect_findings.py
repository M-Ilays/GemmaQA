"""A model reporting "nothing is wrong" must not produce a defect.

Live on Bedrock/Nova Pro, run 47704fde: **27 of 32 recorded "defects" were the
model saying there was no defect** — titles like "No defects detected on the
login page", severity minor, one per action. The report's bug count was
meaningless.

Why the existing filter did not catch it. The prompt said:

    "If nothing looks defective, use classification 'observation' with low
     confidence."

and the parser dropped observations below confidence 0.35. That reads the
confidence field as "how likely is this a defect". A real model reads it as "how
sure are you" — and it is VERY sure the page is fine. So a confident "nothing is
wrong" arrived as a high-confidence observation and sailed through the gate.

The fields, taken verbatim from that run:

    genuine finding   expected: "No sensitive information ... should be displayed"
                      actual:   "An alert containing default login credentials
                                 ... is displayed"          <- a real discrepancy

    false positive    expected: "The login page should display without errors"
                      actual:   "The login page displayed as expected with no
                                 console errors"            <- expectation MET

The fix gives the model an unambiguous way to say it (`no_defect`) and stops the
parser depending on a numeric side-channel whose meaning was ambiguous.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app.gemma.parser import bug_analysis_to_defect, parse_bug_analysis  # noqa: E402
from app.schemas import BugAnalysisResult, BugClassification  # noqa: E402

RUN = "run_1"
URL = "https://opensource-demo.orangehrmlive.com/web/index.php/auth/login"

# Verbatim from run 47704fde's final_report.json.
NOVA_FALSE_POSITIVE = {
    "classification": "observation",
    "title": "No defects detected on the login page",
    "severity": "low",
    "steps": ["Navigate to the URL: " + URL],
    "expected_result": "The login page should display without any errors or issues.",
    "actual_result": "The login page displayed as expected with no console errors or network failures.",
    "possible_root_cause": "Hypothesis: No root cause identified as no defects were detected.",
    "confidence": 0.95,
}
NOVA_GENUINE = {
    "classification": "observation",
    "title": "Display of sensitive information in alert",
    "severity": "high",
    "steps": ["Navigate to the URL: " + URL, "Observe the alerts displayed."],
    "expected_result": "No sensitive information should be displayed in alerts.",
    "actual_result": "An alert containing default login credentials is displayed.",
    "confidence": 0.9,
}


# ===========================================================================
# A -- the explicit classification
# ===========================================================================


def test_no_defect_is_a_classification_the_model_can_return():
    """Before this, OBSERVATION meant both "noteworthy non-defect" and "nothing
    at all", so the model had no way to distinguish them."""
    assert BugClassification("no_defect") is BugClassification.NO_DEFECT


def test_a_no_defect_result_never_becomes_a_defect():
    analysis = BugAnalysisResult(
        classification=BugClassification.NO_DEFECT,
        title="No defects detected on the login page",
        actual_result="The page displayed as expected.",
        confidence=0.99,
    )

    assert bug_analysis_to_defect(analysis, RUN, page_url=URL) is None


def test_high_confidence_does_not_rescue_a_no_defect():
    """The whole bug in one assertion: certainty that a page is FINE is not
    certainty that a finding is REAL."""
    analysis = BugAnalysisResult(classification=BugClassification.NO_DEFECT, confidence=1.0, title="t")

    assert bug_analysis_to_defect(analysis, RUN) is None


def test_the_prompt_offers_the_classification_and_defines_confidence():
    """A parser gate the prompt never mentions is a gate the model cannot satisfy."""
    from app.gemma.prompts import BUG_SYSTEM_PROMPT

    assert "no_defect" in BUG_SYSTEM_PROMPT
    assert "THE DEFECT YOU DESCRIBED IS REAL" in BUG_SYSTEM_PROMPT


# ===========================================================================
# B -- the parser holds even when the model ignores the instruction
# ===========================================================================


def test_an_observation_with_no_actual_result_is_dropped():
    """Second gate: a finding must state what was actually seen. Catches a model
    that keeps using `observation` but leaves the discrepancy fields empty."""
    analysis = BugAnalysisResult(
        classification=BugClassification.OBSERVATION,
        title="Something happened",
        actual_result="   ",
        confidence=0.9,
    )

    assert bug_analysis_to_defect(analysis, RUN) is None


def test_a_low_confidence_observation_is_still_dropped():
    """Third gate, the original one. Kept for a weak hunch, no longer the only
    thing between a clean page and a bug report."""
    analysis = BugAnalysisResult(
        classification=BugClassification.OBSERVATION,
        title="Maybe something",
        actual_result="Possibly odd layout",
        confidence=0.2,
    )

    assert bug_analysis_to_defect(analysis, RUN) is None


def test_an_untitled_observation_is_still_dropped():
    analysis = BugAnalysisResult(
        classification=BugClassification.OBSERVATION, actual_result="x", confidence=0.9
    )

    assert bug_analysis_to_defect(analysis, RUN) is None


# ===========================================================================
# C -- genuine findings still get through
# ===========================================================================


# What live Nova Pro returns AFTER the fix, for the same credentials alert.
# Captured 2026-08-04 against amazon.nova-pro-v1:0. Two things changed: it now
# classifies the finding `suspected_bug` rather than `observation`, and it CITES
# the alert — an id that exists only because alerts became registerable evidence.
NOVA_GENUINE_AFTER_FIX = {
    "classification": "suspected_bug",
    "title": "Potential security risk due to exposed credentials in alert",
    "severity": "high",
    "expected_result": "Credentials should not be displayed.",
    "actual_result": "An alert displays the default username and password.",
    "confidence": 0.8,
    "evidence_ids": ["ev_ui_alert_809cb0066af7"],
}


def test_the_real_finding_still_reaches_the_report():
    """Under-reporting is not the goal. Verified live: with the new prompt Nova
    reports this as `suspected_bug`, a tier the observation gates never touch."""
    defect = bug_analysis_to_defect(
        parse_bug_analysis(NOVA_GENUINE_AFTER_FIX), RUN, page_url=URL
    )

    assert defect is not None
    assert "credentials" in defect.title.lower()


def test_the_pre_fix_shape_of_that_finding_is_now_dropped():
    """Honest record of gate 4's cost. As Nova ORIGINALLY sent it — `observation`
    tier, `evidence_ids: []` because alerts were not registerable — this genuine
    finding would now be suppressed.

    That is the trade-off accepted: an observation-tier claim citing nothing it
    was given is indistinguishable, structurally, from the model narrating a
    normal page. It is acceptable here only because the live check shows the
    finding no longer arrives in this shape — it arrives as `suspected_bug` with
    a citation. If a future model regresses to uncitable observations, this test
    is where that cost is written down.
    """
    analysis = parse_bug_analysis(NOVA_GENUINE)

    assert analysis.evidence_ids == []
    assert bug_analysis_to_defect(analysis, RUN, page_url=URL) is None


def test_the_false_positive_from_that_run_is_dropped_verbatim():
    """The payload exactly as Nova sent it, with `classification: "observation"`
    and confidence 0.95 — so it passes the classification, title, actual_result
    and confidence gates. Only the structural gate catches it: there is nothing
    for a correctly-behaving page to cite.

    This is the test that would have failed before gate 4 existed, and it is the
    one that does not depend on the model adopting `no_defect`.
    """
    defect = bug_analysis_to_defect(parse_bug_analysis(NOVA_FALSE_POSITIVE), RUN, page_url=URL)

    assert defect is None


def test_an_observation_citing_real_evidence_survives():
    """Gate 4 must not silence genuine observations. One citation is enough."""
    analysis = BugAnalysisResult(
        classification=BugClassification.OBSERVATION,
        title="Default credentials shown in an alert",
        actual_result="An alert displays the default username and password.",
        confidence=0.9,
        evidence_ids=["ev_1"],
    )

    assert bug_analysis_to_defect(analysis, RUN) is not None


def test_confirmed_and_suspected_bugs_are_never_filtered():
    """The gates apply to observations only. A real defect claim must survive
    even with no confidence value and no actual_result."""
    for classification in (BugClassification.CONFIRMED_BUG, BugClassification.SUSPECTED_BUG):
        analysis = BugAnalysisResult(classification=classification, title="Login returns 500")
        assert bug_analysis_to_defect(analysis, RUN) is not None, classification


# ===========================================================================
# D -- the legacy format, and provider agreement
# ===========================================================================


def test_the_legacy_is_bug_false_maps_to_no_defect():
    """It used to map to OBSERVATION, so the old format could produce a finding
    that denied its own existence."""
    parsed = parse_bug_analysis({"is_bug": False, "title": "nothing wrong", "confidence": 0.9})

    assert parsed.classification is BugClassification.NO_DEFECT
    assert bug_analysis_to_defect(parsed, RUN) is None


def test_mock_reports_no_defect_the_same_way_a_real_provider_does():
    """One rule, one spelling. Mock previously expressed "nothing found" as a
    near-zero-confidence OBSERVATION — and it was the confidence spelling that
    let a real model's confident "nothing is wrong" through."""
    import asyncio

    from app.gemma.mock_provider import MockGemmaProvider

    result = asyncio.run(
        MockGemmaProvider().analyze_potential_bug(
            {"url": URL, "title": "Login", "console_errors": [], "network_failures": []}
        )
    )

    assert result.classification is BugClassification.NO_DEFECT
    assert bug_analysis_to_defect(result, RUN) is None


def test_memory_keeps_no_note_for_a_no_defect():
    from app.agent.memory import RunMemory
    from app.utils.ids import new_id

    memory = RunMemory(run_id=new_id(), start_url=URL)
    memory.remember_bug_analysis(
        BugAnalysisResult(
            classification=BugClassification.NO_DEFECT, title="No defects detected", confidence=0.9
        ),
        page_url=URL,
    )

    assert memory.observations == []
    assert memory.suspected_bugs == []


# ===========================================================================
# E -- an alert-based finding can now cite the alert
# ===========================================================================


def test_alerts_and_dialogs_are_registerable_evidence():
    """The genuine finding in that run came back with `evidence_ids: []` because
    alerts were shown to the model but never registered, so there was nothing it
    was permitted to cite. Console errors and network failures were the only
    registered kinds."""
    import inspect

    from app.gemma.base import GemmaProvider

    source = inspect.getsource(GemmaProvider.analyze_potential_bug)

    assert '"alerts"' in source
    assert '"dialogs"' in source
    assert '"console_errors"' in source  # unchanged
