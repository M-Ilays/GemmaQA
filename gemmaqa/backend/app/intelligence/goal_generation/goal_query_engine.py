"""Goal query API -- the only way callers read goal memory directly.
Mirrors `graph_query_engine.py`'s role for the Knowledge Graph: simple,
bounded, deterministic lookups, never a place for new reasoning.
"""

from __future__ import annotations

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term
from app.intelligence.goal_generation.goal_memory import GoalMemory
from app.intelligence.goal_generation.schemas import GoalGroup, GoalStatistics, InvestigationGoal

HIGH_PRIORITY_THRESHOLD = 0.6


class GoalQueryEngine:
    def __init__(self, memory: GoalMemory) -> None:
        self.memory = memory

    def goal_by_id(self, goal_id: str) -> InvestigationGoal | None:
        return self.memory.goals.get(goal_id)

    def all_goals(self) -> list[InvestigationGoal]:
        return sorted(self.memory.goals.values(), key=lambda g: g.goal_id)

    def highest_priority_goals(self, limit: int = 20) -> list[InvestigationGoal]:
        return sorted(self.memory.goals.values(), key=lambda g: (-g.priority_score, g.goal_id))[:limit]

    def goals_for_actor(self, actor_term: str) -> list[InvestigationGoal]:
        target = normalize_term(actor_term)
        return sorted((g for g in self.memory.goals.values() if any(normalize_term(a) == target for a in g.required_actors)), key=lambda g: g.goal_id)

    def goals_for_entity(self, entity_term: str) -> list[InvestigationGoal]:
        target = normalize_term(entity_term)
        return sorted((g for g in self.memory.goals.values() if any(normalize_term(e) == target for e in g.required_entities)), key=lambda g: g.goal_id)

    def goals_for_workflow(self, workflow_term: str) -> list[InvestigationGoal]:
        target = normalize_term(workflow_term)
        return sorted((g for g in self.memory.goals.values() if any(normalize_term(w) == target for w in g.required_workflows)), key=lambda g: g.goal_id)

    def goals_for_output(self, output_term: str) -> list[InvestigationGoal]:
        target = normalize_term(output_term)
        return sorted((g for g in self.memory.goals.values() if any(normalize_term(o) == target for o in g.required_outputs)), key=lambda g: g.goal_id)

    def goals_for_gap(self, gap_id: str) -> list[InvestigationGoal]:
        return sorted((g for g in self.memory.goals.values() if gap_id in g.source_gap_ids or gap_id in g.blocking_gaps), key=lambda g: g.goal_id)

    def goals_for_contradiction(self, contradiction_id: str) -> list[InvestigationGoal]:
        return sorted((g for g in self.memory.goals.values() if contradiction_id in g.contradictions), key=lambda g: g.goal_id)

    def goals_by_type(self, goal_type: str) -> list[InvestigationGoal]:
        return sorted((g for g in self.memory.goals.values() if g.goal_type == goal_type), key=lambda g: g.goal_id)

    def blocked_goals(self) -> list[InvestigationGoal]:
        return sorted((g for g in self.memory.goals.values() if g.goal_status == "blocked"), key=lambda g: g.goal_id)

    def completed_goals(self) -> list[InvestigationGoal]:
        return sorted((g for g in self.memory.goals.values() if g.goal_status == "completed"), key=lambda g: g.goal_id)

    def pending_goals(self) -> list[InvestigationGoal]:
        return sorted((g for g in self.memory.goals.values() if g.goal_status == "pending"), key=lambda g: g.goal_id)

    def dismissed_goals(self) -> list[InvestigationGoal]:
        return sorted((g for g in self.memory.goals.values() if g.goal_status == "dismissed"), key=lambda g: g.goal_id)

    def groups_for_goal(self, goal_id: str) -> list[GoalGroup]:
        return sorted((g for g in self.memory.groups.values() if goal_id in g.goal_ids), key=lambda g: g.group_id)

    def all_groups(self) -> list[GoalGroup]:
        return sorted(self.memory.groups.values(), key=lambda g: g.group_id)

    def goal_statistics(self) -> GoalStatistics:
        goals = list(self.memory.goals.values())
        goals_by_type: dict[str, int] = {}
        goals_by_status: dict[str, int] = {}
        for g in goals:
            goals_by_type[g.goal_type] = goals_by_type.get(g.goal_type, 0) + 1
            goals_by_status[g.goal_status] = goals_by_status.get(g.goal_status, 0) + 1
        average_priority = (sum(g.priority_score for g in goals) / len(goals)) if goals else 0.0
        return GoalStatistics(
            total_goals=len(goals), goals_by_type=goals_by_type, goals_by_status=goals_by_status,
            average_priority=average_priority,
            high_priority_count=sum(1 for g in goals if g.priority_score >= HIGH_PRIORITY_THRESHOLD),
            blocked_count=goals_by_status.get("blocked", 0), completed_count=goals_by_status.get("completed", 0),
            dismissed_count=goals_by_status.get("dismissed", 0), group_count=len(self.memory.groups),
            dependency_count=len(self.memory.dependencies), conflict_count=len(self.memory.conflicts),
            graph_version=self.memory.last_graph_version,
        )
