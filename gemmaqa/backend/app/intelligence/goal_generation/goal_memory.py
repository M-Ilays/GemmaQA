"""Goal memory -- the raw store for `InvestigationGoal`/`GoalGroup`/
`GoalDependency`/`GoalConflict` records and their idempotent merge logic.
Mirrors `knowledge_graph_memory.py`'s role for the Knowledge Graph: this is
NOT a second application-memory system, it only holds the OUTPUT of goal
generation (derived, evidence-backed investigation goals), never
rediscovered application knowledge of its own.

Idempotency matters here exactly as it does for the graph: re-running
`generate()` against an UNCHANGED Knowledge Graph must leave every goal's
`created_at`/`observation_count` untouched -- only genuinely changed
content bumps them.
"""

from __future__ import annotations

from app.intelligence.goal_generation.schemas import GoalConflict, GoalDependency, GoalGroup, InvestigationGoal

_STABLE_STATUSES = {"completed", "dismissed", "superseded"}
_CONTENT_EXCLUDE = {"created_at", "updated_at", "observation_count", "goal_status"}


class GoalMemory:
    def __init__(self) -> None:
        self.goals: dict[str, InvestigationGoal] = {}
        self.groups: dict[str, GoalGroup] = {}
        self.dependencies: dict[str, GoalDependency] = {}
        self.conflicts: dict[str, GoalConflict] = {}
        self.generation_count: int = 0
        # The Knowledge Graph version the LAST `generate()` pass reasoned
        # over -- tracked here (not just on the transient
        # `GoalGenerationResult`) so `goal_statistics()`/`goal_engine.
        # statistics()` called independently, later, still report the
        # correct version rather than the schema's bare 0 default.
        self.last_graph_version: int = 0
        self.dirty_this_pass: bool = False
        self.added_goal_ids: list[str] = []
        self.updated_goal_ids: list[str] = []
        self.removed_goal_ids: list[str] = []

    def begin_pass(self) -> None:
        self.dirty_this_pass = False
        self.added_goal_ids = []
        self.updated_goal_ids = []
        self.removed_goal_ids = []

    def end_pass(self) -> bool:
        if self.dirty_this_pass:
            self.generation_count += 1
        return self.dirty_this_pass

    # -- goals --------------------------------------------------------------------

    def upsert_goal(self, goal: InvestigationGoal) -> InvestigationGoal:
        existing = self.goals.get(goal.goal_id)
        if existing is None:
            self.goals[goal.goal_id] = goal
            self.added_goal_ids.append(goal.goal_id)
            self.dirty_this_pass = True
            return goal

        # A status an external consumer (not implemented in this milestone)
        # has moved to a terminal state should never be silently reset back
        # to "pending"/"blocked" just because the same graph evidence is
        # still present on the next pass.
        if existing.goal_status in _STABLE_STATUSES:
            goal.goal_status = existing.goal_status

        if _content_differs(existing, goal):
            goal.created_at = existing.created_at
            goal.observation_count = existing.observation_count + 1
            self.goals[goal.goal_id] = goal
            self.updated_goal_ids.append(goal.goal_id)
            self.dirty_this_pass = True
            return goal
        return existing

    def remove_goals_not_in(self, still_present_ids: set[str]) -> None:
        """A goal's underlying evidence can disappear entirely between
        passes (e.g. a gap resolves and nothing else motivates the same
        subject) -- such goals are removed rather than left stale forever,
        since (unlike graph nodes/edges) a goal with zero remaining
        motivation carries no historical value of its own."""
        for goal_id in list(self.goals.keys()):
            if goal_id not in still_present_ids:
                del self.goals[goal_id]
                self.removed_goal_ids.append(goal_id)
                self.dirty_this_pass = True

    def mark_goal_status(self, goal_id: str, status: str) -> None:
        goal = self.goals.get(goal_id)
        if goal is not None and goal.goal_status != status:
            goal.goal_status = status
            self.dirty_this_pass = True

    # -- groups / dependencies / conflicts (recomputed each pass, keyed for
    #    stability -- replacing by deterministic id is itself idempotent) --------

    def replace_groups(self, groups: list[GoalGroup]) -> None:
        new_groups = {g.group_id: g for g in groups}
        if new_groups != self.groups:
            self.dirty_this_pass = True
        self.groups = new_groups

    def replace_dependencies(self, dependencies: list[GoalDependency]) -> None:
        new_deps = {d.dependency_id: d for d in dependencies}
        if set(new_deps.keys()) != set(self.dependencies.keys()):
            self.dirty_this_pass = True
        self.dependencies = new_deps

    def replace_conflicts(self, conflicts: list[GoalConflict]) -> None:
        new_conflicts = {c.conflict_id: c for c in conflicts}
        if set(new_conflicts.keys()) != set(self.conflicts.keys()):
            self.dirty_this_pass = True
        self.conflicts = new_conflicts

    def dependencies_for_goal(self, goal_id: str) -> list[GoalDependency]:
        return [d for d in self.dependencies.values() if d.goal_id == goal_id]


def _content_differs(existing: InvestigationGoal, incoming: InvestigationGoal) -> bool:
    a = existing.model_dump(exclude=_CONTENT_EXCLUDE)
    b = incoming.model_dump(exclude=_CONTENT_EXCLUDE)
    a["goal_status"] = existing.goal_status
    b["goal_status"] = incoming.goal_status
    return a != b
