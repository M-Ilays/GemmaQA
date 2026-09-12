"""Dimensional coverage (Phase 11): separate, honest coverage axes instead of one
misleading percentage, and a single de-duplicated coverage implementation (the
legacy ReportBuilder calculation is removed — it now delegates to memory.coverage())."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.goals import ExplorationGoal  # noqa: E402
from app.agent.memory import RunMemory  # noqa: E402
from app.application.store import ApplicationStore  # noqa: E402
from app.reporting.report_builder import ReportBuilder  # noqa: E402
from app.schemas import FormDescriptor, FormField, PageState  # noqa: E402
from app.utils.ids import new_id  # noqa: E402

ROOT = "https://example.com"


def _page(url: str, forms: list | None = None) -> PageState:
    return PageState(page_id=new_id(), url=url, title="T", forms=forms or [], state_fingerprint=f"fp-{url}")


def _memory() -> RunMemory:
    memory = RunMemory(run_id=new_id(), start_url=f"{ROOT}/")
    memory.remaining_action_budget = 30
    return memory


def test_coverage_record_carries_named_dimensions():
    memory = _memory()
    memory.remember_page(_page(f"{ROOT}/inventory.html"), explored=True)
    cov = memory.coverage()
    names = {d.name for d in cov.dimensions}
    assert {
        "navigation",
        "module",
        "submodule",
        "form_inspection",
        "safe_form_execution",
        "workflow",
        "candidate",
        "goal",
        "page_state",
        "role",
    } <= names


def test_navigation_dimension_never_reports_complete_with_unknown_candidates():
    memory = _memory()
    page = _page(f"{ROOT}/inventory.html")
    memory.remember_page(page, explored=True)
    memory.app_store.register_candidate_url(f"{ROOT}/cart.html")
    cov = memory.coverage()
    nav = next(d for d in cov.dimensions if d.name == "navigation")
    assert nav.unknown >= 1
    assert nav.pct_complete < 100.0


def test_form_inspection_dimension_uses_real_lifecycle_not_always_true_flag():
    """Regression: AppForm.inspected is set True the instant a form is merely
    observed, so it was a useless, always-100% signal. lifecycle_state (only
    advanced by an actual INSPECT_FORM completion) is the truthful one."""
    memory = _memory()
    form = FormDescriptor(
        form_id="f1",
        fields=[FormField(element_id="el_a", label="A", field_type="text")],
        submit_element_id="el_submit",
    )
    memory.remember_page(_page(f"{ROOT}/signup", forms=[form]))
    cov = memory.coverage()
    form_dim = next(d for d in cov.dimensions if d.name == "form_inspection")
    assert form_dim.discovered == 1
    assert form_dim.inspected == 0, "must not report a freshly-observed form as inspected"

    memory.app_store.mark_form_inspected("f1")
    cov2 = memory.coverage()
    form_dim2 = next(d for d in cov2.dimensions if d.name == "form_inspection")
    assert form_dim2.inspected == 1


def test_goal_dimension_reflects_goal_statuses():
    memory = _memory()
    memory.goals = [
        ExplorationGoal(goal_id="a", goal_type="inspect_form", title="a", status="completed"),
        ExplorationGoal(goal_id="b", goal_type="inspect_form", title="b", status="blocked"),
        ExplorationGoal(goal_id="c", goal_type="inspect_form", title="c", status="proposed"),
    ]
    cov = memory.coverage()
    goal_dim = next(d for d in cov.dimensions if d.name == "goal")
    assert goal_dim.discovered == 3
    assert goal_dim.completed == 1
    assert goal_dim.blocked == 1
    assert goal_dim.unknown == 1


def test_safe_form_execution_dimension_counts_real_completions():
    memory = _memory()
    memory.safe_writes_completed = 2
    cov = memory.coverage()
    dim = next(d for d in cov.dimensions if d.name == "safe_form_execution")
    assert dim.completed == 2


def test_role_dimension_marked_unavailable_when_no_roles_observed():
    memory = _memory()
    cov = memory.coverage()
    role_dim = next(d for d in cov.dimensions if d.name == "role")
    assert role_dim.available is False


def test_report_builder_delegates_to_single_coverage_implementation():
    """The legacy duplicate calculation in ReportBuilder must be gone — both call
    sites must produce the exact same CoverageRecord."""
    memory = _memory()
    memory.remember_page(_page(f"{ROOT}/inventory.html"), explored=True)
    via_memory = memory.coverage()
    via_report_builder = ReportBuilder().compute_coverage(memory)
    assert via_memory.model_dump() == via_report_builder.model_dump()
