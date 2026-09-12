"""Closes the loop back to Goal Generation -- WITHOUT this package ever
fabricating a goal itself. `InvestigationGoal` records are entirely derived
from the Knowledge Graph by `GoalGenerationEngine.generate()`; since every
step this engine executes flows through the controller's existing
observation/registry/graph-sync pipeline (see package `__init__.py`), the
graph already reflects whatever was learned by the time this runs. "Follow-
up goals" therefore means: re-run goal generation against the now-updated
graph, and report whichever goal ids are new since the investigation
started -- never inventing a goal record ourselves.
"""

from __future__ import annotations

from app.intelligence.autonomous_investigation.schemas import NextGoals


def goal_ids_snapshot(goal_engine) -> set[str]:
    if goal_engine is None:
        return set()
    return {g.goal_id for g in goal_engine.query_engine.all_goals()}


def regenerate_goals(goal_engine, graph, *, iteration: int) -> None:
    if goal_engine is not None and graph is not None:
        goal_engine.generate(graph, iteration=iteration)


def build_next_goals(investigation_id: str, before_ids: set[str], after_ids: set[str], *, triggered_by: str) -> NextGoals:
    new_ids = sorted(after_ids - before_ids)
    return NextGoals(
        next_goals_id=f"next-goals:{investigation_id}", investigation_id=investigation_id,
        new_goal_ids=new_ids, triggered_by=triggered_by,
    )
