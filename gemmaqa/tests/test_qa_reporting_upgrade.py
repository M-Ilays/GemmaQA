"""Senior QA Reporting Architect upgrade: the five newer intelligence
engines (Knowledge Graph, Goal Generation, Scenario Planning, QA Strategy,
Autonomous Investigation), CRUD/collection/form-lifecycle/cleanup coverage,
blocked/unexecuted-scenario reasons, capability disclosure, and stop-reason
reporting — all surfaced through app.reporting.report_builder.ReportBuilder.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.memory import RunMemory  # noqa: E402
from app.agent.temporary_record_registry import TemporaryRecordRegistry  # noqa: E402
from app.intelligence.autonomous_investigation.autonomous_investigation_engine import (  # noqa: E402
    AutonomousInvestigationEngine,
)
from app.intelligence.autonomous_investigation.schemas import (  # noqa: E402
    AssertionResult,
    InvestigationResult,
    VerificationResult,
)
from app.intelligence.crud_discovery.crud_discovery_engine import CRUDDiscoveryEngine  # noqa: E402
from app.intelligence.crud_discovery.schemas import CRUDWorkflowHypothesis  # noqa: E402
from app.intelligence.goal_generation.goal_generation_engine import GoalGenerationEngine  # noqa: E402
from app.intelligence.knowledge_graph.knowledge_graph import ApplicationKnowledgeGraph  # noqa: E402
from app.intelligence.qa_strategy.qa_strategy_engine import QAStrategyEngine  # noqa: E402
from app.intelligence.qa_strategy.schemas import ExecutionCandidate  # noqa: E402
from app.intelligence.scenario_planning.schemas import InvestigationScenario  # noqa: E402
from app.intelligence.scenario_planning.scenario_planning_engine import ScenarioPlanningEngine  # noqa: E402
from app.reporting.blocked_reasons import BLOCKED_REASONS, classify_scenario_blocked_reason  # noqa: E402
from app.reporting.report_builder import REPORT_SECTION_ORDER, ReportBuilder  # noqa: E402


def _bare_memory(run_id: str = "run-1", *, provider_type: str = "mock") -> RunMemory:
    return RunMemory(run_id=run_id, start_url="https://example.test/", provider_type=provider_type)


def _memory_with_full_pipeline() -> RunMemory:
    """One scenario blocked (missing test data), one scenario passed with a
    supported assertion -- exercises every new engine's real data path, not
    just the empty-engine default shape."""
    memory = _bare_memory(provider_type="openai_compatible")

    memory.knowledge_graph = ApplicationKnowledgeGraph()
    memory.goal_engine = GoalGenerationEngine()

    memory.scenario_engine = ScenarioPlanningEngine()
    blocked_scenario = InvestigationScenario(
        scenario_id="sc1", goal_id="g1", scenario_type="workflow_verification",
        title="Create a record", status="blocked", feasibility_status="blocked",
    )
    passed_scenario = InvestigationScenario(
        scenario_id="sc2", goal_id="g1", scenario_type="workflow_verification",
        title="Verify a record", status="passed", feasibility_status="feasible",
    )
    memory.scenario_engine.memory.scenarios["sc1"] = blocked_scenario
    memory.scenario_engine.memory.scenarios["sc2"] = passed_scenario

    memory.strategy_engine = QAStrategyEngine()
    blocked_candidate = ExecutionCandidate(
        candidate_id="cand1", scenario_id="sc1", goal_id="g1", scenario_type="workflow_verification",
        queue_type="blocked", recommended_action="block", blocking_reasons=["missing test data"],
    )
    memory.strategy_engine.memory.candidates["cand1"] = blocked_candidate

    memory.investigation_engine = AutonomousInvestigationEngine()
    memory.investigation_engine.memory.eligibility_by_candidate_id["cand1"] = {
        "status": "blocked_by_missing_test_data",
        "reason": "no valid test data available for this field",
    }
    verification = VerificationResult(
        verification_id="v1", investigation_id="inv1", scenario_id="sc2",
        assertion_results=[
            AssertionResult(
                assertion_result_id="a1", investigation_id="inv1", subject_id="row1",
                operator="equals", outcome="supported", observed_value="X", expected_value="X",
                confidence=0.9, explanation="matched",
            )
        ],
        supported_count=1, overall_outcome="supported",
    )
    result = InvestigationResult(
        investigation_id="inv1", candidate_id="cand1", scenario_id="sc2",
        outcome="completed", state="completed", verification=verification,
    )
    memory.investigation_engine.memory.results["inv1"] = result
    memory.investigation_engine.memory.history_order.append("inv1")

    memory.crud_registry = CRUDDiscoveryEngine().registry
    memory.crud_registry.upsert(
        CRUDWorkflowHypothesis(operation="create", entity_hypothesis="employee", status="verified", collection_id="grid1")
    )
    memory.known_collection_ids.add("grid1")

    memory.temporary_record_registry = TemporaryRecordRegistry(run_id=memory.run_id)
    entry = memory.temporary_record_registry.register_created(
        record_type="employee", generated_identity="Alex Rivera", collection_element_id="grid1",
    )
    memory.temporary_record_registry.mark_verified(entry.temporary_record_id, evidence=["list_row match"])

    return memory


# ---------------------------------------------------------------------------
# 1. New engine data appears in reports
# ---------------------------------------------------------------------------


def test_new_engine_data_appears_in_report():
    memory = _memory_with_full_pipeline()
    report = ReportBuilder().build(memory)

    assert report.knowledge_graph_summary["available"] is True
    assert report.generated_goals["available"] is True
    assert report.scenario_planning_summary["available"] is True
    assert report.scenario_planning_summary["total_scenarios"] == 2
    assert report.qa_strategy_summary["available"] is True
    assert report.qa_strategy_summary["total_candidates"] == 1
    assert report.autonomous_investigation_summary["available"] is True
    assert report.autonomous_investigation_summary["total_investigations"] == 1

    for title in (
        "Knowledge Graph Summary", "Generated Goals", "Scenario Planning Summary",
        "QA Strategy Summary", "Autonomous Investigation Summary",
    ):
        assert title in REPORT_SECTION_ORDER
        assert title in report.sections_markdown
        assert report.sections_markdown[title].strip()


def test_engine_sections_degrade_cleanly_when_engine_absent():
    memory = _bare_memory()
    report = ReportBuilder().build(memory)
    assert report.knowledge_graph_summary == {"available": False, "note": "No Knowledge Graph attached this run."}
    assert report.generated_goals["available"] is False
    assert report.scenario_planning_summary["available"] is False
    assert report.qa_strategy_summary["available"] is False
    assert report.autonomous_investigation_summary["available"] is False
    # Every section still renders text, never a crash / missing key
    assert "Not available this run" in report.sections_markdown["Generated Goals"] or report.sections_markdown["Generated Goals"]


# ---------------------------------------------------------------------------
# 2. Blocked reasons appear
# ---------------------------------------------------------------------------


def test_blocked_scenario_carries_a_named_reason_not_bare_not_run():
    memory = _memory_with_full_pipeline()
    report = ReportBuilder().build(memory)

    assert len(report.blocked_scenarios) >= 1
    for row in report.blocked_scenarios:
        assert row["reason"] in BLOCKED_REASONS
        assert row["reason"] != ""
    # sc1 is blocked; sc2 passed and must not appear in blocked/unexecuted
    ids = {r["scenario_id"] for r in report.blocked_scenarios}
    assert "sc1" in ids
    assert "sc2" not in ids


def test_unexecuted_scenarios_is_a_superset_of_blocked_and_all_have_reasons():
    memory = _memory_with_full_pipeline()
    report = ReportBuilder().build(memory)
    blocked_ids = {r["scenario_id"] for r in report.blocked_scenarios}
    unexecuted_ids = {r["scenario_id"] for r in report.unexecuted_scenarios}
    assert blocked_ids.issubset(unexecuted_ids)
    for row in report.unexecuted_scenarios:
        assert row["reason"] in BLOCKED_REASONS


def test_classify_scenario_blocked_reason_maps_eligibility_statuses():
    assert classify_scenario_blocked_reason(
        eligibility_status="blocked_by_missing_actor", autonomous_investigation_enabled=True, provider_capable=True,
    ) == "missing_actor"
    assert classify_scenario_blocked_reason(
        eligibility_status="blocked_by_configuration", autonomous_investigation_enabled=True, provider_capable=True,
    ) == "controlled_writes_disabled"
    assert classify_scenario_blocked_reason(
        eligibility_status="blocked_by_safety_policy", autonomous_investigation_enabled=True, provider_capable=True,
    ) == "unsafe"
    assert classify_scenario_blocked_reason(
        eligibility_status=None, autonomous_investigation_enabled=False, provider_capable=True,
    ) == "autonomous_mode_disabled"
    assert classify_scenario_blocked_reason(
        eligibility_status=None, autonomous_investigation_enabled=True, provider_capable=False,
    ) == "provider_incapable"


def test_legacy_not_run_scenarios_get_a_reason_too():
    from app.schemas import TestExecution, TestScenario

    memory = _bare_memory()
    memory.remaining_action_budget = 0
    for i in range(3):
        memory.scenarios.append(TestScenario(test_id=f"t{i}", title=f"Scenario {i}"))
        memory.executions.append(TestExecution(execution_id=f"e{i}", test_id=f"t{i}", run_id=memory.run_id, status="not_run"))

    report = ReportBuilder().build(memory)
    legacy_rows = [r for r in report.unexecuted_scenarios if r.get("source") == "legacy_tester"]
    assert len(legacy_rows) == 3
    for row in legacy_rows:
        assert row["reason"] == "budget_exhausted"


# ---------------------------------------------------------------------------
# 3. Autonomous mode status appears
# ---------------------------------------------------------------------------


def test_autonomous_mode_status_appears_in_capability_disclosure():
    disabled = ReportBuilder().build(_bare_memory())
    assert disabled.capability_disclosure["autonomous_investigation_enabled"] is False
    assert "disabled" in disabled.sections_markdown["Configuration and Capability Disclosure"].lower()

    memory = _memory_with_full_pipeline()
    enabled = ReportBuilder().build(memory)
    assert enabled.capability_disclosure["autonomous_investigation_enabled"] is True
    assert "enabled" in enabled.sections_markdown["Configuration and Capability Disclosure"].lower()


# ---------------------------------------------------------------------------
# 4. Mock capability status appears
# ---------------------------------------------------------------------------


def test_mock_provider_gets_prominent_exploration_only_notice():
    memory = _bare_memory(provider_type="mock")
    report = ReportBuilder().build(memory)
    assert report.capability_disclosure["provider_capability_mode"] == "exploration_only"
    assert report.capability_disclosure["mock_exploration_only_notice"]
    assert "exploration-only" in report.capability_disclosure["mock_exploration_only_notice"].lower()
    assert "exploration-only" in report.sections_markdown["Configuration and Capability Disclosure"].lower()


def test_non_mock_provider_has_no_exploration_only_notice():
    # build_runtime_info() (and therefore report.runtime["capability_mode"])
    # reflects the SERVER's globally configured provider, not RunMemory.provider_type
    # (a pre-existing architectural fact, not something this upgrade changes) --
    # so _capability_disclosure is tested directly here, isolated from whatever
    # provider this sandbox happens to have configured.
    memory = _bare_memory(provider_type="openai_compatible")
    runtime = {
        "provider": "Gemma via OpenAI-compatible API",
        "model": "gemma-3-27b",
        "capability_mode": "real_model_reasoning",
        "capability_mode_explanation": "",
        "browser_adapter": "Direct Playwright",
        "enable_autonomous_investigation": False,
    }
    disclosure = ReportBuilder()._capability_disclosure(memory, runtime)
    assert disclosure["mock_exploration_only_notice"] == ""
    assert disclosure["provider_capability_mode"] == "real_model_reasoning"


# ---------------------------------------------------------------------------
# 5. CRUD coverage is calculated correctly
# ---------------------------------------------------------------------------


def test_crud_coverage_breaks_down_by_operation():
    memory = _memory_with_full_pipeline()
    report = ReportBuilder().build(memory)
    cov = report.coverage
    assert cov.crud_create_discovered == 1
    assert cov.crud_create_executed == 1  # status="verified" counts as executed
    assert cov.crud_update_discovered == 0
    assert cov.crud_delete_discovered == 0
    # Read coverage reads collection discovery/inspection, not a "read" CRUD op
    assert cov.crud_read_discovered == cov.collections_discovered
    assert cov.crud_read_executed == cov.collections_inspected
    assert report.crud_workflow_coverage["create"]["discovered"] == 1
    assert report.crud_workflow_coverage["create"]["executed"] == 1


def test_crud_coverage_zero_when_no_hypotheses():
    memory = _bare_memory()
    report = ReportBuilder().build(memory)
    cov = report.coverage
    assert cov.crud_create_discovered == 0
    assert cov.crud_create_executed == 0
    assert cov.crud_update_discovered == 0
    assert cov.crud_delete_discovered == 0


# ---------------------------------------------------------------------------
# 6. Page exploration is not confused with execution coverage
# ---------------------------------------------------------------------------


def test_page_coverage_and_scenario_execution_coverage_are_independent_numbers():
    memory = _memory_with_full_pipeline()
    report = ReportBuilder().build(memory)
    cov = report.coverage
    # No pages were ever visited in this memory, but scenarios/CRUD data IS present —
    # proving execution-coverage metrics are not derived from / gated on page coverage.
    assert cov.pages_discovered == 0
    assert cov.pages_explored == 0
    assert cov.scenarios_generated == 2
    assert cov.crud_create_discovered == 1
    assert cov.observed_coverage_pct == 0.0
    # The disclaimer text explicitly says visited/discovered pages is not test coverage
    assert "not test coverage" in cov.disclaimer.lower() or any(
        "not test coverage" in note.lower() for note in cov.coverage_notes
    )


def test_coverage_notes_explicitly_separate_exploration_from_testing():
    memory = _bare_memory()
    report = ReportBuilder().build(memory)
    notes_blob = " ".join(report.coverage.coverage_notes).lower()
    assert "exploration metric" in notes_blob or "not test coverage" in notes_blob


# ---------------------------------------------------------------------------
# 7. Stop reason appears
# ---------------------------------------------------------------------------


def test_stop_reason_appears_in_stop_summary_and_section():
    memory = _bare_memory()
    memory.stop_reason = "budget_exceeded"
    report = ReportBuilder().build(memory)
    assert report.stop_summary["stop_reason"] == "budget_exceeded"
    assert "budget_exceeded" in report.sections_markdown["Stop Reason"]
    assert "Stop Reason" in REPORT_SECTION_ORDER


def test_stop_summary_answers_all_required_questions():
    memory = _memory_with_full_pipeline()
    memory.stop_reason = "max_actions_reached"
    report = ReportBuilder().build(memory)
    summary = report.stop_summary
    assert summary["stop_reason"] == "max_actions_reached"
    assert "last_meaningful_operation" in summary
    assert "untested_summary" in summary
    assert "what_prevented_execution" in summary
    assert "recommended_configuration_changes" in summary
    assert "temporary_records_left_behind" in summary


# ---------------------------------------------------------------------------
# 8. Temporary record cleanup appears
# ---------------------------------------------------------------------------


def test_temporary_records_and_cleanup_status_appear():
    memory = _memory_with_full_pipeline()
    report = ReportBuilder().build(memory)
    assert report.temporary_records["available"] is True
    assert report.temporary_records["total_records"] == 1
    assert "Temporary Records" in report.sections_markdown
    assert "1" in report.sections_markdown["Temporary Records"]

    assert report.cleanup_status["pending"] == 1  # verified but not yet cleaned up
    assert "Cleanup Status" in report.sections_markdown
    assert "Cleanup Status" in REPORT_SECTION_ORDER


def test_manual_cleanup_instructions_surface_when_cleanup_exhausted():
    from app.agent.temporary_record_registry import MAX_CLEANUP_ATTEMPTS

    memory = _bare_memory()
    memory.temporary_record_registry = TemporaryRecordRegistry(run_id=memory.run_id)
    entry = memory.temporary_record_registry.register_created(record_type="employee", generated_identity="Alex Rivera")
    memory.temporary_record_registry.mark_verified(entry.temporary_record_id, evidence=["e"])
    for _ in range(MAX_CLEANUP_ATTEMPTS):
        memory.temporary_record_registry.mark_cleanup_failed(entry.temporary_record_id, error="delete control not found")

    report = ReportBuilder().build(memory)
    assert report.cleanup_status["manual_required"] == 1
    assert report.cleanup_status["manual_cleanup_instructions"]
    assert "Alex Rivera" in report.sections_markdown["Cleanup Status"]


# ---------------------------------------------------------------------------
# 9. Report remains backward compatible
# ---------------------------------------------------------------------------


def test_legacy_report_fields_still_populate():
    memory = _bare_memory()
    report = ReportBuilder().build(memory)
    # Every pre-existing top-level field must still exist and be well-formed
    assert report.run_id == memory.run_id
    assert isinstance(report.page_inventory, list)
    assert isinstance(report.forms_inventory, list)
    assert isinstance(report.test_scenarios, list)
    assert isinstance(report.test_executions, list)
    assert report.coverage is not None
    assert isinstance(report.runtime, dict)
    assert report.runtime.get("provider") == "Mock"
    for legacy_section in (
        "Executive Summary", "Run Environment", "Product Overview", "Page Inventory",
        "Forms Inventory", "Tables Inventory", "Test Scenarios", "Test Execution Results",
        "Coverage Summary", "Known Limitations",
    ):
        assert legacy_section in report.sections_markdown


def test_final_report_json_round_trips_with_new_fields():
    memory = _memory_with_full_pipeline()
    report = ReportBuilder().build(memory)
    dumped = report.model_dump(mode="json")
    for key in (
        "knowledge_graph_summary", "generated_goals", "scenario_planning_summary",
        "qa_strategy_summary", "autonomous_investigation_summary", "crud_workflow_coverage",
        "form_lifecycle_summary", "collection_coverage", "executed_assertions",
        "blocked_scenarios", "unexecuted_scenarios", "temporary_records", "cleanup_status",
        "stop_summary", "capability_disclosure",
    ):
        assert key in dumped

    from app.schemas import FinalReport

    reloaded = FinalReport.model_validate(dumped)
    assert reloaded.run_id == report.run_id
    assert reloaded.blocked_scenarios == report.blocked_scenarios


def test_coverage_record_new_fields_default_safely_for_old_callers():
    from app.schemas import CoverageRecord

    cov = CoverageRecord(run_id="x")
    assert cov.collections_discovered == 0
    assert cov.scenarios_generated == 0
    assert cov.cleanup_pending == 0
    assert cov.crud_create_discovered == 0
