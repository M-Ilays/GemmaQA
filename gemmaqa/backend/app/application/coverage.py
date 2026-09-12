"""Coverage calculations from the canonical ApplicationModel."""

from __future__ import annotations

from typing import Any

from app.application.models import (
    ApplicationModel,
    ExplorationStatus,
    ScenarioExecutionStatus,
)
from app.schemas import CoverageDimension, CoverageRecord


_EXECUTED = {
    ScenarioExecutionStatus.PASSED,
    ScenarioExecutionStatus.FAILED,
    ScenarioExecutionStatus.BLOCKED,
    ScenarioExecutionStatus.SKIPPED,
    ScenarioExecutionStatus.EXECUTING,
    ScenarioExecutionStatus.INCONCLUSIVE,
    ScenarioExecutionStatus.CONTRADICTED,
}


def compute_coverage(
    model: ApplicationModel,
    *,
    actions_taken: int = 0,
    action_budget: int = 0,
    tables_discovered: int = 0,
    tables_inspected: int = 0,
    workflows_identified: int = 0,
    bugs_found: int = 0,
    suspected_issues: int = 0,
    observations: int = 0,
    tests_generated: int | None = None,
    tests_executed: int | None = None,
    passed: int | None = None,
    failed: int | None = None,
    goals: list[dict[str, Any]] | None = None,
    gaps: list[dict[str, Any]] | None = None,
    safe_writes_completed: int = 0,
    active_form_workflow_state: str | None = None,
    frontier_candidate_count: int = 0,
    attempted_candidate_count: int = 0,
    distinct_state_count: int = 0,
    roles_observed: int | None = None,
    crud_hypotheses: list[Any] | None = None,
    investigation_statistics: Any | None = None,
    goals_generated: int = 0,
    scenario_statistics: Any | None = None,
    collections_discovered: int = 0,
    collections_inspected: int = 0,
    local_controls_discovered: int = 0,
    local_controls_exercised: int = 0,
    cleanup_counts: dict[str, int] | None = None,
) -> CoverageRecord:
    """
    Page metrics come only from unique canonical Page records.

    pages_discovered = len(model.pages)
    pages_explored   = pages marked EXPLORED
    observed_coverage = pages_explored / pages_discovered  (0 if none)

    Link-only candidate_urls are listed in unexplored_areas and must not
    appear in pages_discovered or the observed_coverage denominator.
    """
    # Single source: same helper as Page Inventory
    pages = canonical_pages(model)
    discovered = len(pages)
    explored = sum(1 for p in pages if p.exploration_status == ExplorationStatus.EXPLORED)


    forms = model.forms
    forms_discovered = len(forms)
    # AppForm.inspected is set True the instant a form is merely observed (DOM-
    # scraped), so it's always true and useless as a coverage signal — see
    # app.agent.form_workflow / ApplicationStore.mark_form_inspected. lifecycle_state
    # tracks whether an INSPECT_FORM action actually ran, and is the truthful count.
    forms_inspected = sum(1 for f in forms if f.lifecycle_state not in {"discovered", ""})
    forms_tested = sum(1 for f in forms if f.tested)

    scenarios = model.scenarios
    generated = tests_generated if tests_generated is not None else len(scenarios)
    executed = (
        tests_executed
        if tests_executed is not None
        else sum(1 for s in scenarios if s.execution_status in _EXECUTED)
    )
    passed_n = (
        passed
        if passed is not None
        else sum(1 for s in scenarios if s.execution_status == ScenarioExecutionStatus.PASSED)
    )
    failed_n = (
        failed
        if failed is not None
        else sum(1 for s in scenarios if s.execution_status == ScenarioExecutionStatus.FAILED)
    )

    nav_discovered = len(model.navigation_edges)
    nav_used = sum(1 for e in model.navigation_edges if e.occurrence_count > 0)

    hyps = list(crud_hypotheses or [])
    crud_discovered_n = len(hyps)
    crud_executed_n = sum(1 for h in hyps if getattr(h, "status", "") in {"supported", "executed", "verified"})
    crud_verified_n = sum(1 for h in hyps if getattr(h, "status", "") == "verified")

    assertions_evaluated_n = assertions_supported_n = assertions_contradicted_n = assertions_inconclusive_n = 0
    if investigation_statistics is not None:
        assertions_evaluated_n = getattr(investigation_statistics, "total_assertions_evaluated", 0)
        assertions_supported_n = getattr(investigation_statistics, "supported_assertion_count", 0)
        assertions_contradicted_n = getattr(investigation_statistics, "contradicted_assertion_count", 0)
        assertions_inconclusive_n = getattr(investigation_statistics, "inconclusive_assertion_count", 0)

    def pct(n: int, d: int) -> float:
        if d <= 0:
            return 0.0
        return round(min(100.0, (n / d) * 100.0), 1)

    # Per-operation CRUD breakdown -- "update" reads crud_discovery's
    # internal "edit" operation; there is no separate "read" operation there,
    # so CRUD read coverage instead reads collections_discovered/inspected.
    def _crud_count(operation: str, *, executed_only: bool) -> int:
        matches = [h for h in hyps if getattr(h, "operation", "") == operation]
        if not executed_only:
            return len(matches)
        return sum(1 for h in matches if getattr(h, "status", "") in {"supported", "executed", "verified"})

    crud_create_discovered = _crud_count("create", executed_only=False)
    crud_create_executed = _crud_count("create", executed_only=True)
    crud_update_discovered = _crud_count("edit", executed_only=False)
    crud_update_executed = _crud_count("edit", executed_only=True)
    crud_delete_discovered = _crud_count("delete", executed_only=False)
    crud_delete_executed = _crud_count("delete", executed_only=True)
    cleanup_succeeded = int((cleanup_counts or {}).get("succeeded") or 0)
    cleanup_deleted = int((cleanup_counts or {}).get("deleted") or 0)
    if cleanup_succeeded or cleanup_deleted:
        # `deleted` means the control was clicked and the record left the
        # detail page; `absence_verified` is the stronger list-row check.
        # Run b6509a34 deleted the contact then logged out before absence
        # was confirmed, so crud_delete_executed stayed 0.
        crud_delete_executed = max(crud_delete_executed, cleanup_succeeded + cleanup_deleted)

    # Goal Generation / Scenario Planning pipeline counts -- distinct from
    # tests_generated/tests_executed and the legacy "goal" dimension below,
    # which both read the OLDER goal/AppTestScenario mechanisms.
    scenarios_generated_n = getattr(scenario_statistics, "total_scenarios", 0) if scenario_statistics else 0
    scenarios_by_status = dict(getattr(scenario_statistics, "scenarios_by_status", None) or {})
    scenarios_by_feasibility = dict(getattr(scenario_statistics, "scenarios_by_feasibility", None) or {})
    _PRE_EXECUTION_STATUSES = {
        "draft", "feasible", "conditionally_feasible", "blocked", "incomplete",
        "superseded", "stale", "selected", "rejected", "queued",
    }
    scenarios_executable_n = sum(
        n for status, n in scenarios_by_feasibility.items() if status in {"feasible", "conditionally_feasible"}
    )
    scenarios_executed_n = sum(n for status, n in scenarios_by_status.items() if status not in _PRE_EXECUTION_STATUSES)
    scenarios_passed_n = scenarios_by_status.get("passed", 0) + scenarios_by_status.get("completed", 0)
    scenarios_failed_n = scenarios_by_status.get("failed", 0) + scenarios_by_status.get("contradicted", 0)
    scenarios_blocked_n = scenarios_by_status.get("blocked", 0)

    cleanup = cleanup_counts or {}

    candidates = list(dict.fromkeys(model.candidate_urls))
    unexplored = [
        *candidates,
        *[
            p.canonical_url
            for p in pages
            if p.exploration_status
            not in {ExplorationStatus.EXPLORED, ExplorationStatus.BLOCKED}
        ],
    ]
    observed_pct = pct(explored, discovered)

    notes = [
        "Observed exploratory coverage only — not total application coverage.",
        "These metrics do not imply complete application coverage.",
        f"Unique canonical same-origin pages: {discovered}.",
        f"Unvisited same-origin candidates (not counted as pages): {len(candidates)}.",
        f"External references excluded from coverage: {len(model.external_references)}.",
        "Pages visited/discovered is an EXPLORATION metric, not test coverage — "
        "see the separate generated/executed scenario, CRUD-operation, and "
        "verification figures below for what was actually tested.",
        "CRUD read coverage reads collections_discovered/collections_inspected "
        "(record-collections actually reasoned about), since crud_discovery tracks "
        "only create/edit/delete operations, not a distinct 'read' operation.",
        "Local-control coverage (buttons/inputs/toggles with no href) is a separate "
        "axis from page-navigation coverage above; a page can be fully navigated to "
        "while most of its local controls remain unexercised.",
    ]

    dimensions = _build_dimensions(
        model=model,
        pages=pages,
        discovered=discovered,
        explored=explored,
        candidates=candidates,
        forms_discovered=forms_discovered,
        forms_inspected=forms_inspected,
        goals=goals or [],
        gaps=gaps or [],
        safe_writes_completed=safe_writes_completed,
        active_form_workflow_state=active_form_workflow_state,
        frontier_candidate_count=frontier_candidate_count,
        attempted_candidate_count=attempted_candidate_count,
        distinct_state_count=distinct_state_count,
        roles_observed=roles_observed,
        crud_discovered=crud_discovered_n,
        crud_executed=crud_executed_n,
        assertions_evaluated=assertions_evaluated_n,
        assertions_supported=assertions_supported_n,
        assertions_contradicted=assertions_contradicted_n,
        assertions_inconclusive=assertions_inconclusive_n,
        collections_discovered=collections_discovered,
        collections_inspected=collections_inspected,
        local_controls_discovered=local_controls_discovered,
        local_controls_exercised=local_controls_exercised,
        scenarios_generated=scenarios_generated_n,
        scenarios_executed=scenarios_executed_n,
        scenarios_blocked=scenarios_blocked_n,
        cleanup_counts=cleanup,
        pct=pct,
    )

    return CoverageRecord(
        run_id=model.run_id,
        pages_discovered=discovered,
        pages_explored=explored,
        navigation_items_discovered=nav_discovered,
        navigation_items_used=nav_used,
        forms_discovered=forms_discovered,
        forms_inspected=forms_inspected,
        forms_tested=forms_tested,
        tables_discovered=tables_discovered,
        tables_inspected=tables_inspected,
        workflows_identified=workflows_identified,
        dimensions=dimensions,
        tests_generated=generated,
        tests_executed=executed,
        passed=passed_n,
        failed=failed_n,
        crud_operations_discovered=crud_discovered_n,
        crud_operations_executed=crud_executed_n,
        crud_operations_verified=crud_verified_n,
        crud_create_discovered=crud_create_discovered,
        crud_create_executed=crud_create_executed,
        crud_read_discovered=collections_discovered,
        crud_read_executed=collections_inspected,
        crud_update_discovered=crud_update_discovered,
        crud_update_executed=crud_update_executed,
        crud_delete_discovered=crud_delete_discovered,
        crud_delete_executed=crud_delete_executed,
        assertions_evaluated=assertions_evaluated_n,
        assertions_supported=assertions_supported_n,
        assertions_contradicted=assertions_contradicted_n,
        assertions_inconclusive=assertions_inconclusive_n,
        collections_discovered=collections_discovered,
        collections_inspected=collections_inspected,
        local_controls_discovered=local_controls_discovered,
        local_controls_exercised=local_controls_exercised,
        goals_generated=goals_generated,
        scenarios_generated=scenarios_generated_n,
        scenarios_executable=scenarios_executable_n,
        scenarios_executed=scenarios_executed_n,
        scenarios_passed=scenarios_passed_n,
        scenarios_failed=scenarios_failed_n,
        scenarios_blocked=scenarios_blocked_n,
        cleanup_pending=cleanup.get("pending", 0),
        cleanup_succeeded=cleanup.get("succeeded", 0),
        cleanup_failed=cleanup.get("failed", 0),
        cleanup_manual_required=cleanup.get("manual_required", 0),
        bugs_found=bugs_found,
        suspected_issues=suspected_issues,
        observations=observations,
        action_budget_used=actions_taken,
        action_budget_total=action_budget,
        observed_coverage_pct=observed_pct,
        explored_coverage_pct=observed_pct,
        executed_coverage_pct=pct(executed, max(generated, 1)) if generated else 0.0,
        local_control_coverage_pct=pct(local_controls_exercised, local_controls_discovered),
        collection_coverage_pct=pct(collections_inspected, collections_discovered),
        scenario_execution_coverage_pct=pct(scenarios_executed_n, max(scenarios_generated_n, 1)) if scenarios_generated_n else 0.0,
        cleanup_coverage_pct=pct(
            cleanup.get("succeeded", 0),
            max(cleanup.get("pending", 0) + cleanup.get("succeeded", 0) + cleanup.get("manual_required", 0), 1),
        ),
        coverage_notes=notes,
        unexplored_areas=unexplored[:20],
        disclaimer=(
            "Coverage percentages reflect observed exploratory activity only and do not claim "
            "complete application coverage. Exploration coverage (pages visited/discovered) is "
            "NOT test coverage: see generated vs. executed scenario counts, CRUD-operation "
            "discovered/executed/verified counts, and assertion verification counts for what was "
            "actually tested and confirmed."
        ),
    )


def _build_dimensions(
    *,
    model: ApplicationModel,
    pages: list,
    discovered: int,
    explored: int,
    candidates: list[str],
    forms_discovered: int,
    forms_inspected: int,
    goals: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    safe_writes_completed: int,
    active_form_workflow_state: str | None,
    frontier_candidate_count: int,
    attempted_candidate_count: int,
    distinct_state_count: int,
    roles_observed: int | None,
    crud_discovered: int = 0,
    crud_executed: int = 0,
    assertions_evaluated: int = 0,
    assertions_supported: int = 0,
    assertions_contradicted: int = 0,
    assertions_inconclusive: int = 0,
    collections_discovered: int = 0,
    collections_inspected: int = 0,
    local_controls_discovered: int = 0,
    local_controls_exercised: int = 0,
    scenarios_generated: int = 0,
    scenarios_executed: int = 0,
    scenarios_blocked: int = 0,
    cleanup_counts: dict[str, int] | None = None,
    pct,
) -> list[CoverageDimension]:
    """Separate, honest coverage axes — see CoverageDimension. A dimension with
    unknown > 0 (a same-origin region known to exist but never reached) must never
    round up to 100% complete."""
    blocked_pages = sum(1 for p in pages if p.exploration_status == ExplorationStatus.BLOCKED)
    dims = [
        CoverageDimension(
            name="navigation",
            discovered=discovered + len(candidates),
            observed=discovered,
            completed=explored,
            blocked=blocked_pages,
            unknown=len(candidates),
            pct_complete=pct(explored, discovered + len(candidates)),
            notes="unknown = same-origin candidate URLs discovered via links but never visited.",
        ),
    ]

    modules = [m for m in model.modules if m.canonical_key != "authentication" or m.page_ids]
    top_level = [m for m in modules if not m.parent_module_id]
    submodules = [m for m in modules if m.parent_module_id]

    def _module_completed(m) -> bool:
        mod_pages = [p for p in pages if p.id in m.page_ids]
        return bool(mod_pages) and all(p.exploration_status == ExplorationStatus.EXPLORED for p in mod_pages)

    dims.append(
        CoverageDimension(
            name="module",
            discovered=len(top_level),
            observed=sum(1 for m in top_level if m.page_ids),
            completed=sum(1 for m in top_level if _module_completed(m)),
            unknown=sum(1 for m in top_level if not m.page_ids),
            pct_complete=pct(sum(1 for m in top_level if _module_completed(m)), max(len(top_level), 1)),
        )
    )
    dims.append(
        CoverageDimension(
            name="submodule",
            discovered=len(submodules),
            observed=sum(1 for m in submodules if m.page_ids),
            completed=sum(1 for m in submodules if _module_completed(m)),
            unknown=sum(1 for m in submodules if not m.page_ids),
            pct_complete=pct(sum(1 for m in submodules if _module_completed(m)), max(len(submodules), 1))
            if submodules
            else 0.0,
            available=bool(submodules),
            notes="" if submodules else "No submodule hierarchy has been inferred yet this run.",
        )
    )
    dims.append(
        CoverageDimension(
            name="form_inspection",
            discovered=forms_discovered,
            inspected=forms_inspected,
            completed=forms_inspected,
            unknown=max(forms_discovered - forms_inspected, 0),
            pct_complete=pct(forms_inspected, forms_discovered),
        )
    )

    active_in_progress = active_form_workflow_state not in (None, "verified", "failed", "blocked", "skipped")
    safe_form_attempted = safe_writes_completed + (1 if active_in_progress else 0)
    dims.append(
        CoverageDimension(
            name="safe_form_execution",
            discovered=max(safe_form_attempted, safe_writes_completed),
            attempted=safe_form_attempted,
            completed=safe_writes_completed,
            blocked=1 if active_form_workflow_state == "blocked" else 0,
            pct_complete=pct(safe_writes_completed, max(safe_form_attempted, 1)),
            notes="Counts safe test-data-creation / form-workflow submissions actually observed this run.",
        )
    )

    workflow_goals = [g for g in goals if g.get("goal_type") == "continue_workflow"]
    dims.append(
        CoverageDimension(
            name="workflow",
            discovered=len(workflow_goals),
            completed=sum(1 for g in workflow_goals if g.get("status") == "completed"),
            blocked=sum(1 for g in workflow_goals if g.get("status") == "blocked"),
            unknown=sum(1 for g in workflow_goals if g.get("status") in {"proposed", "deferred"}),
            pct_complete=pct(
                sum(1 for g in workflow_goals if g.get("status") == "completed"), max(len(workflow_goals), 1)
            ),
        )
    )

    dims.append(
        CoverageDimension(
            name="candidate",
            discovered=frontier_candidate_count,
            attempted=attempted_candidate_count,
            pct_complete=pct(attempted_candidate_count, max(frontier_candidate_count, 1)),
            notes="A per-iteration snapshot (candidates are regenerated each iteration, not stored historically).",
        )
    )

    dims.append(
        CoverageDimension(
            name="goal",
            discovered=len(goals),
            completed=sum(1 for g in goals if g.get("status") == "completed"),
            blocked=sum(1 for g in goals if g.get("status") == "blocked"),
            unknown=sum(1 for g in goals if g.get("status") in {"proposed", "deferred"}),
            pct_complete=pct(sum(1 for g in goals if g.get("status") == "completed"), max(len(goals), 1)),
            notes=f"{len(gaps)} open ApplicationStore-derived gap(s) currently feed this goal set.",
        )
    )

    dims.append(
        CoverageDimension(
            name="page_state",
            discovered=distinct_state_count or discovered,
            observed=distinct_state_count or discovered,
            pct_complete=0.0,
            notes=(
                "Distinct observed page states (URL + fingerprint, including menu/modal/tab/"
                "auth/data-state variations) — always >= page count, never collapsed to it."
            ),
        )
    )

    dims.append(
        CoverageDimension(
            name="role",
            discovered=roles_observed or 0,
            available=roles_observed is not None,
            notes="" if roles_observed is not None else "No role/permission observations recorded this run.",
        )
    )

    dims.append(
        CoverageDimension(
            name="crud_operation",
            discovered=crud_discovered,
            attempted=crud_executed,
            completed=crud_executed,
            unknown=max(crud_discovered - crud_executed, 0),
            pct_complete=pct(crud_executed, max(crud_discovered, 1)) if crud_discovered else 0.0,
            available=crud_discovered > 0,
            notes=(
                "Create/edit/delete hypotheses from app.intelligence.crud_discovery. "
                "'completed' requires before/after corroboration (a create/edit-shaped form "
                "appearing, or a delete's confirmation/row-count decrease) — never a bare "
                "Add/Edit/Delete control existing."
                if crud_discovered
                else "No record-collection CRUD surfaces detected this run."
            ),
        )
    )

    dims.append(
        CoverageDimension(
            name="verification",
            discovered=assertions_evaluated,
            completed=assertions_supported,
            blocked=assertions_contradicted,
            unknown=assertions_inconclusive,
            pct_complete=pct(assertions_supported, max(assertions_evaluated, 1)) if assertions_evaluated else 0.0,
            available=assertions_evaluated > 0,
            notes=(
                "Autonomous Investigation assertion outcomes (supported/contradicted/"
                "inconclusive) — a step can execute successfully yet still verify "
                "inconclusively; that is reported here, never silently rounded into 'passed'."
                if assertions_evaluated
                else "No autonomous-investigation assertions were evaluated this run."
            ),
        )
    )

    dims.append(
        CoverageDimension(
            name="collection",
            discovered=collections_discovered,
            inspected=collections_inspected,
            completed=collections_inspected,
            unknown=max(collections_discovered - collections_inspected, 0),
            pct_complete=pct(collections_inspected, max(collections_discovered, 1)) if collections_discovered else 0.0,
            available=collections_discovered > 0,
            notes=(
                "Universal record-collection (grid/table/card-list) surfaces from "
                "app.perception.models.RecordCollection. 'inspected' requires GemmaQA to have "
                "actually reasoned about the collection's CRUD surface, never merely rendering it."
                if collections_discovered
                else "No record-collection surfaces detected this run."
            ),
        )
    )

    dims.append(
        CoverageDimension(
            name="local_control",
            discovered=local_controls_discovered,
            attempted=local_controls_exercised,
            completed=local_controls_exercised,
            unknown=max(local_controls_discovered - local_controls_exercised, 0),
            pct_complete=pct(local_controls_exercised, max(local_controls_discovered, 1)) if local_controls_discovered else 0.0,
            available=local_controls_discovered > 0,
            notes=(
                "Non-navigating interactive controls (buttons/inputs/toggles with no href) on "
                "visited pages — a separate axis from page-navigation coverage; a fully-navigated "
                "page can still have most of its local controls unexercised."
            ),
        )
    )

    dims.append(
        CoverageDimension(
            name="scenario_execution",
            discovered=scenarios_generated,
            attempted=scenarios_executed,
            completed=scenarios_executed,
            blocked=scenarios_blocked,
            unknown=max(scenarios_generated - scenarios_executed - scenarios_blocked, 0),
            pct_complete=pct(scenarios_executed, max(scenarios_generated, 1)) if scenarios_generated else 0.0,
            available=scenarios_generated > 0,
            notes=(
                "app.intelligence.scenario_planning scenarios (Autonomous Investigation pipeline) "
                "— distinct from the legacy 'candidate'/tests_generated mechanisms above."
                if scenarios_generated
                else "No Scenario Planning scenarios were generated this run "
                "(Autonomous Investigation may be disabled)."
            ),
        )
    )

    cleanup = cleanup_counts or {}
    cleanup_total = cleanup.get("pending", 0) + cleanup.get("succeeded", 0) + cleanup.get("manual_required", 0)
    dims.append(
        CoverageDimension(
            name="cleanup",
            discovered=cleanup_total,
            attempted=cleanup_total - cleanup.get("pending", 0),
            completed=cleanup.get("succeeded", 0),
            blocked=cleanup.get("manual_required", 0),
            unknown=cleanup.get("pending", 0),
            pct_complete=pct(cleanup.get("succeeded", 0), max(cleanup_total, 1)) if cleanup_total else 0.0,
            available=cleanup_total > 0,
            notes=(
                "Temporary Record Registry cleanup outcomes (app.agent.temporary_record_registry) "
                "— records GemmaQA itself created this run, never pre-existing application data."
                if cleanup_total
                else "No temporary test records were created this run."
            ),
        )
    )

    return dims


def canonical_pages(model: ApplicationModel) -> list:
    """Same page set used by coverage and Page Inventory (deduped by canonical_url)."""
    seen: dict[str, object] = {}
    for p in model.pages:
        if p.visit_count <= 0:
            continue
        # Last write wins for metadata; identity is canonical_url
        seen[p.canonical_url] = p
    return list(seen.values())
