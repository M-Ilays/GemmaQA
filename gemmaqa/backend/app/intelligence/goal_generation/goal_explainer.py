"""Builds a human-readable `GoalExplanation` for one goal -- the "why"
trail a future Scenario Planning Engine (or a human reviewer) can read
without re-deriving it from the raw graph. Also determines
`recommended_goal_type`: which OTHER goal type should be tackled first
when this goal is blocked by an unresolved dependency, or this goal's own
type when nothing blocks direct investigation.
"""

from __future__ import annotations

from app.intelligence.goal_generation.schemas import GoalDependency, GoalExplanation, InvestigationGoal

MAX_SUMMARY_EVIDENCE_LINES = 5
MAX_DEPENDENCY_NAMES = 3


def explain(
    goal: InvestigationGoal,
    *,
    dependencies_by_goal: dict[str, list[GoalDependency]],
    goals_by_id: dict[str, InvestigationGoal],
) -> tuple[GoalExplanation, str]:
    summary = [ev.description for ev in goal.supporting_evidence[:MAX_SUMMARY_EVIDENCE_LINES] if ev.description]
    if not summary:
        summary = [goal.description or f"'{goal.title}' has not yet been confirmed."]

    unmet = [d for d in dependencies_by_goal.get(goal.goal_id, []) if d.depends_on_goal_id in goals_by_id and goals_by_id[d.depends_on_goal_id].goal_status not in {"completed", "dismissed"}]

    if unmet:
        blocker_titles = [goals_by_id[d.depends_on_goal_id].title for d in unmet[:MAX_DEPENDENCY_NAMES]]
        summary.append(f"This investigation depends on {len(unmet)} unresolved goal(s): {', '.join(blocker_titles)}.")
        top_blocker = goals_by_id[unmet[0].depends_on_goal_id]
        recommended_investigation = f"Investigate '{top_blocker.title}' before '{goal.title}'."
        recommended_goal_type = top_blocker.goal_type
    else:
        recommended_investigation = f"Investigate '{goal.title}' directly -- no unresolved prerequisite goals block it."
        recommended_goal_type = goal.goal_type

    summary.append(f"Confidence: {round(goal.confidence, 2)}.")

    explanation = GoalExplanation(
        explanation_id=f"explanation:{goal.goal_id}", goal_id=goal.goal_id, summary=summary,
        confidence=goal.confidence, recommended_investigation=recommended_investigation,
    )
    return explanation, recommended_goal_type
