"""The activity log: what GemmaQA did, when, and how long it took.

Before this, three things described a run and none answered "what is it doing
right now?": the exploration trace went to stdout behind an environment
variable, the WebSocket timeline lived in browser memory and vanished on
refresh, and the report only existed once the run was over. None recorded
DURATION, so "GemmaQA hung" and "GemmaQA waited nine seconds for a slow page"
looked identical.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.reporting.activity_log import (  # noqa: E402
    ACTIVITY_LOG_FILENAME,
    MAX_DETAIL_CHARS,
    MAX_READ_LIMIT,
    PHASES,
    ActivityLog,
    phase_for,
    summarize,
)


# ===========================================================================
# A -- writing
# ===========================================================================


def test_a_record_is_appended_as_one_json_line(tmp_path: Path):
    log = ActivityLog("run_1", root=tmp_path)
    log.record("page_observed", {"url": "https://app.example.com/records", "elements": 12})

    lines = (tmp_path / ACTIVITY_LOG_FILENAME).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "page_observed"
    assert record["run_id"] == "run_1"
    assert record["detail"]["elements"] == 12


def test_sequence_numbers_are_contiguous_and_start_at_one(tmp_path: Path):
    """The UI polls with `since_seq`; a gap or a restart at zero would make it
    either skip records or replay them."""
    log = ActivityLog("run_1", root=tmp_path)
    seqs = [log.record("state_changed", {"state": f"s{i}"})["seq"] for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]


def test_every_record_carries_an_elapsed_clock(tmp_path: Path):
    log = ActivityLog("run_1", root=tmp_path)
    record = log.record("action_planned", {"action": "click"})
    assert record["elapsed_ms"] >= 0


def test_a_span_records_its_duration_and_accumulates_a_time_budget(tmp_path: Path):
    """The time budget is the answer to "what was it doing all that time?" —
    a list of events, however complete, cannot answer that."""
    log = ActivityLog("run_1", root=tmp_path)
    log.record("phase_timing", {}, phase="execution", duration_ms=1500)
    log.record("phase_timing", {}, phase="execution", duration_ms=500)
    log.record("phase_timing", {}, phase="observation", duration_ms=250)

    assert log.time_budget() == {"execution": 2000, "observation": 250}


def test_records_without_a_duration_do_not_enter_the_time_budget(tmp_path: Path):
    log = ActivityLog("run_1", root=tmp_path)
    log.record("page_observed", {"url": "https://app.example.com"})
    assert log.time_budget() == {}


def test_secrets_never_reach_the_log(tmp_path: Path):
    """The log is a file on disk that outlives the run and gets shared. It goes
    through the project's existing sanitizer rather than a second one."""
    log = ActivityLog("run_1", root=tmp_path)
    log.record(
        "action_planned",
        {"password": "hunter2", "token": "abc123", "action": "fill", "value": "s3cret"},
    )
    written = (tmp_path / ACTIVITY_LOG_FILENAME).read_text(encoding="utf-8")

    assert "hunter2" not in written
    assert "abc123" not in written
    assert "fill" in written  # non-sensitive structure survives


def test_an_enormous_payload_is_truncated_rather_than_written_whole(tmp_path: Path):
    """A canonical page model or full prompt would make the log unreadable and
    the WebSocket frame huge; the full object lives in the run's other
    artifacts."""
    log = ActivityLog("run_1", root=tmp_path)
    record = log.record("page_observed", {"blob": "x" * (MAX_DETAIL_CHARS * 3)})

    assert record["detail"]["truncated"] is True
    assert record["detail"]["original_chars"] > MAX_DETAIL_CHARS
    assert len(record["detail"]["preview"]) <= MAX_DETAIL_CHARS


def test_an_unwritable_location_does_not_raise(tmp_path: Path):
    """An activity log that can abort a QA run is worse than no activity log."""
    log = ActivityLog("run_1", root=tmp_path)
    log.path = tmp_path / "no" / "such" / "dir" / ACTIVITY_LOG_FILENAME
    record = log.record("page_observed", {"url": "https://app.example.com"})
    assert record["seq"] == 1  # the caller still gets its record back


# ===========================================================================
# B -- phases
# ===========================================================================


def test_events_map_to_the_phase_a_reader_expects():
    assert phase_for("page_observed") == "observation"
    assert phase_for("action_planned") == "planning"
    assert phase_for("action_executed") == "execution"
    assert phase_for("cleanup_step") == "cleanup"


def test_an_unmapped_event_still_gets_a_valid_phase():
    """A new event type must degrade to a sensible bucket, never to a phase the
    UI cannot group."""
    assert phase_for("something_invented_later") in PHASES


def test_every_mapped_phase_is_in_the_closed_vocabulary():
    from app.reporting.activity_log import _EVENT_PHASES

    assert set(_EVENT_PHASES.values()) <= PHASES


def test_an_explicit_phase_overrides_the_event_default(tmp_path: Path):
    log = ActivityLog("run_1", root=tmp_path)
    assert log.record("page_observed", {}, phase="cleanup")["phase"] == "cleanup"


def test_an_invalid_explicit_phase_falls_back_rather_than_being_stored(tmp_path: Path):
    log = ActivityLog("run_1", root=tmp_path)
    assert log.record("page_observed", {}, phase="not_a_phase")["phase"] == "observation"


# ===========================================================================
# C -- summaries: the line a person reads
# ===========================================================================


def test_an_observation_reads_as_a_sentence():
    assert summarize("page_observed", {"title": "Contact List", "elements": 5}) == (
        "Observed Contact List (5 interactive elements)"
    )


def test_a_planned_action_names_the_target_and_the_reason():
    text = summarize("action_planned", {"action": "click", "element_id": "el_008", "reason": "Open the record"})
    assert "click" in text and "el_008" in text and "Open the record" in text


def test_a_failed_action_says_so():
    assert "failed" in summarize("action_executed", {"action": "click", "success": False})
    assert "succeeded" in summarize("action_executed", {"action": "click", "success": True})


# Payloads captured from a real run. Summaries are built from what the emitter
# ACTUALLY sends, and guessing those keys has now produced two wrong summaries
# ("Run state: unknown" hundreds of times, and every passing safety check
# rendered as BLOCKED). These fixtures are the guard against a third.
REAL_SAFETY_ALLOW = {
    "proposed_action": "click",
    "action_level": "safe_write",
    "safety_decision": "allow",
    "execution_decision": "execute",
    "block_reason": "",
    "element_id": "el_005",
}
REAL_SAFETY_BLOCK = {
    **REAL_SAFETY_ALLOW,
    "safety_decision": "block",
    "execution_decision": "skip",
    "block_reason": "Risk level 'RiskLevel.HIGH' is blocked",
}


def test_a_passing_safety_check_is_not_reported_as_blocked():
    """It read `allowed`, which SafetyAudit.to_payload() does not send, so every
    allowed action was logged as "Safety check BLOCKED" — wrong and alarming."""
    text = summarize("safety_decision", REAL_SAFETY_ALLOW)
    assert "BLOCKED" not in text
    assert "passed" in text and "click" in text


def test_a_blocked_safety_decision_is_unmistakable():
    text = summarize("safety_decision", REAL_SAFETY_BLOCK)
    assert "BLOCKED" in text
    assert "Risk level 'RiskLevel.HIGH' is blocked" in text


def test_summaries_of_real_payloads_never_say_unknown():
    """Every one of these came off a live run. A summary containing "unknown" or
    "no reason given" means the summariser is reading a key the emitter does not
    send."""
    real_payloads = {
        "state_changed": {"state": "initializing", "message": "Initializing run"},
        "run_started": {"url": "https://app.example.com", "provider": "Mock"},
        "browser_opened": {"headless": True, "adapter": "Direct Playwright"},
        "navigated": {"url": "https://app.example.com/records"},
        "page_observed": {"url": "https://app.example.com", "title": "Records", "elements": 5},
        "action_planned": {"action": "click", "element_id": "el_005", "reason": "Open registration"},
        "action_started": {"action": "click", "element_id": "el_005"},
        "action_executed": {"action": "click", "element_id": "el_005", "success": True},
        "safety_decision": REAL_SAFETY_ALLOW,
        "run_stopping": {"reason": "all_safe_candidates_exhausted"},
        "tests_proposed": {"count": 3},
    }
    for event, payload in real_payloads.items():
        text = summarize(event, payload)
        assert "unknown" not in text.lower(), f"{event}: {text}"
        assert "no reason given" not in text, f"{event}: {text}"


def test_a_state_change_uses_its_own_payload_keys():
    """It read `status` while the payload carries `state`/`message`, so the most
    frequent event in a run rendered as "Run state: unknown" hundreds of times."""
    assert summarize("state_changed", {"state": "initializing", "message": "Initializing run"}) == "Initializing run"
    assert summarize("state_changed", {"state": "exploring"}) == "exploring"


def test_an_unknown_event_degrades_to_readable_rather_than_empty():
    assert summarize("some_new_event", {}) == "some new event"


def test_every_summary_is_a_non_empty_string_for_an_empty_payload():
    """Summaries are built from payloads that may be missing any key; none may
    raise or come back blank."""
    from app.reporting.activity_log import _EVENT_PHASES

    for event in _EVENT_PHASES:
        text = summarize(event, {})
        assert isinstance(text, str) and text.strip(), event


# ===========================================================================
# D -- reading it back, including while the run is still going
# ===========================================================================


def test_records_are_read_back_in_order(tmp_path: Path):
    log = ActivityLog("run_1", root=tmp_path)
    for i in range(5):
        log.record("state_changed", {"state": f"s{i}"})

    records = ActivityLog.read("run_1", root=tmp_path)
    assert [r["seq"] for r in records] == [1, 2, 3, 4, 5]


def test_since_seq_returns_only_what_the_caller_has_not_seen(tmp_path: Path):
    """This is what lets an operator reload the page mid-run and still have the
    whole history without refetching it all."""
    log = ActivityLog("run_1", root=tmp_path)
    for i in range(5):
        log.record("state_changed", {"state": f"s{i}"})

    assert [r["seq"] for r in ActivityLog.read("run_1", root=tmp_path, since_seq=3)] == [4, 5]


def test_reads_are_capped(tmp_path: Path):
    log = ActivityLog("run_1", root=tmp_path)
    for _ in range(20):
        log.record("state_changed", {})

    assert len(ActivityLog.read("run_1", root=tmp_path, limit=5)) == 5


def test_an_absurd_limit_is_clamped_not_honoured(tmp_path: Path):
    log = ActivityLog("run_1", root=tmp_path)
    log.record("state_changed", {})
    # Does not raise, and cannot be used to ask for an unbounded response.
    assert len(ActivityLog.read("run_1", root=tmp_path, limit=MAX_READ_LIMIT * 100)) == 1


def test_reading_can_be_restricted_to_one_phase(tmp_path: Path):
    log = ActivityLog("run_1", root=tmp_path)
    log.record("page_observed", {})
    log.record("action_planned", {})
    log.record("page_observed", {})

    records = ActivityLog.read("run_1", root=tmp_path, phase="observation")
    assert len(records) == 2
    assert all(r["phase"] == "observation" for r in records)


def test_reading_a_run_with_no_log_yet_returns_nothing_rather_than_failing(tmp_path: Path):
    assert ActivityLog.read("never_ran", root=tmp_path) == []


def test_a_torn_final_line_is_skipped_not_treated_as_corruption(tmp_path: Path):
    """A read that races the writer sees a partial last line. That is normal
    while a run is in progress, and must not lose the records before it."""
    log = ActivityLog("run_1", root=tmp_path)
    log.record("page_observed", {"url": "https://app.example.com"})
    with (tmp_path / ACTIVITY_LOG_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write('{"seq": 2, "event": "page_obs')

    records = ActivityLog.read("run_1", root=tmp_path)
    assert [r["seq"] for r in records] == [1]


def test_blank_lines_are_ignored(tmp_path: Path):
    log = ActivityLog("run_1", root=tmp_path)
    log.record("page_observed", {})
    with (tmp_path / ACTIVITY_LOG_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write("\n\n")

    assert len(ActivityLog.read("run_1", root=tmp_path)) == 1
