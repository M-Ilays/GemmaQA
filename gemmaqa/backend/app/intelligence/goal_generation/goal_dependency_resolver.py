"""Goal dependency resolution -- represents (never executes) prerequisite
relationships between goals, e.g. "verify a KPI" depends on "verify the
workflow that feeds it" depends on "verify the state transition it
produces" depends on "verify the permission that allows it". Every
dependency is derived directly from existing graph structure (has_step,
acts_on, to_state/from_state, generated_by, requires, performed_by) --
never guessed at.
"""

from __future__ import annotations

from app.intelligence.goal_generation.schemas import GoalDependency, InvestigationGoal

_OUTPUT_GOAL_TYPES = {"verify_kpi", "validate_dashboard_output", "validate_report", "validate_notification", "validate_aggregation_rule"}


class GoalDependencyResolver:
    def __init__(self, graph) -> None:
        self.graph = graph
        self.memory = graph.memory
        self.query = graph.query_engine

    def resolve(self, goals: list[InvestigationGoal], *, primary_node_goal_ids: dict[str, str]) -> list[GoalDependency]:
        goals_by_id = {g.goal_id: g for g in goals}
        deps: list[GoalDependency] = []
        seen: set[tuple[str, str]] = set()

        def add(goal_id: str, depends_on_id: str, reason: str) -> None:
            if goal_id == depends_on_id or depends_on_id not in goals_by_id or goal_id not in goals_by_id:
                return
            key = (goal_id, depends_on_id)
            if key in seen:
                return
            seen.add(key)
            deps.append(GoalDependency(dependency_id=f"dep:{goal_id}->{depends_on_id}", goal_id=goal_id, depends_on_goal_id=depends_on_id, reason=reason))

        for goal in goals:
            if goal.goal_type == "verify_workflow":
                self._workflow_dependencies(goal, add, primary_node_goal_ids)
            elif goal.goal_type == "validate_prerequisite":
                self._prerequisite_blocks_workflow(goal, add, primary_node_goal_ids)
            elif goal.goal_type in _OUTPUT_GOAL_TYPES:
                self._output_dependencies(goal, add, primary_node_goal_ids)
            elif goal.goal_type == "verify_dependency":
                self._verify_dependency_dependencies(goal, add, primary_node_goal_ids)
            elif goal.goal_type == "verify_state_transition":
                self._transition_dependencies(goal, add, primary_node_goal_ids)
            elif goal.goal_type == "verify_actor_capability":
                self._actor_capability_dependencies(goal, add, primary_node_goal_ids)

        return sorted(deps, key=lambda d: (d.goal_id, d.depends_on_goal_id))

    # -- verify_workflow depends on the states it transitions through and the
    #    permissions/operations its steps require -------------------------------

    def _workflow_dependencies(self, goal, add, primary_node_goal_ids: dict[str, str]) -> None:
        workflow_node_id = _primary_node(goal, "verify_workflow")
        if workflow_node_id is None:
            return
        for step in self.query.workflow_steps(workflow_node_id):
            for edge_id in self.memory.outgoing_edge_ids(step.node_id):
                edge = self.memory.edges.get(edge_id)
                if edge is None or edge.stale:
                    continue
                if edge.edge_type in {"from_state", "to_state"} and edge.target_node_id in primary_node_goal_ids:
                    add(goal.goal_id, primary_node_goal_ids[edge.target_node_id], f"Workflow '{goal.title}' transitions through a state that is not yet fully verified.")
                elif edge.edge_type == "performed_by" and edge.target_node_id in primary_node_goal_ids:
                    add(goal.goal_id, primary_node_goal_ids[edge.target_node_id], f"Workflow '{goal.title}' relies on an actor whose capability is not yet verified.")

    def _prerequisite_blocks_workflow(self, goal, add, primary_node_goal_ids: dict[str, str]) -> None:
        prereq_node_id = _primary_node(goal, "validate_prerequisite")
        if prereq_node_id is None:
            return
        for edge_id in self.memory.incoming_edge_ids(prereq_node_id):
            edge = self.memory.edges.get(edge_id)
            if edge is None or edge.stale or edge.edge_type != "requires":
                continue
            workflow_goal_id = primary_node_goal_ids.get(edge.source_node_id)
            if workflow_goal_id:
                add(workflow_goal_id, goal.goal_id, f"Workflow requires prerequisite '{goal.title}', which is not yet satisfied.")

    def _output_dependencies(self, goal, add, primary_node_goal_ids: dict[str, str]) -> None:
        for node_id in goal.supporting_graph_nodes:
            node = self.memory.nodes.get(node_id)
            if node is None or node.node_type not in {"derived_output", "counter", "badge", "chart", "report", "queue", "notification", "alert"}:
                continue
            for edge_id in self.memory.outgoing_edge_ids(node_id) | self.memory.incoming_edge_ids(node_id):
                edge = self.memory.edges.get(edge_id)
                if edge is None or edge.stale or edge.edge_type not in {"generated_by", "affects"}:
                    continue
                other_id = edge.target_node_id if edge.source_node_id == node_id else edge.source_node_id
                workflow_goal_id = primary_node_goal_ids.get(other_id)
                if workflow_goal_id:
                    add(goal.goal_id, workflow_goal_id, f"'{goal.title}' is produced by a workflow that is not yet fully verified.")

    def _verify_dependency_dependencies(self, goal, add, primary_node_goal_ids: dict[str, str]) -> None:
        if not goal.supporting_graph_nodes:
            return
        source_node_id = goal.supporting_graph_nodes[0]
        workflow_goal_id = primary_node_goal_ids.get(source_node_id)
        if workflow_goal_id:
            add(goal.goal_id, workflow_goal_id, f"'{goal.title}' relies on a workflow that is not yet fully verified.")

    def _transition_dependencies(self, goal, add, primary_node_goal_ids: dict[str, str]) -> None:
        state_node_id = _primary_node(goal, "verify_state_transition")
        if state_node_id is None:
            return
        for edge_id in self.memory.incoming_edge_ids(state_node_id):
            edge = self.memory.edges.get(edge_id)
            if edge is None or edge.stale or edge.edge_type not in {"from_state", "to_state"}:
                continue
            step_or_transition = self.memory.nodes.get(edge.source_node_id)
            if step_or_transition is None:
                continue
            for step_edge_id in self.memory.outgoing_edge_ids(step_or_transition.node_id):
                step_edge = self.memory.edges.get(step_edge_id)
                if step_edge is None or step_edge.stale:
                    continue
                if step_edge.edge_type == "performed_by" and step_edge.target_node_id in primary_node_goal_ids:
                    add(goal.goal_id, primary_node_goal_ids[step_edge.target_node_id], f"'{goal.title}' is caused by an actor whose capability is not yet verified.")

    def _actor_capability_dependencies(self, goal, add, primary_node_goal_ids: dict[str, str]) -> None:
        actor_node_id = _primary_node(goal, "verify_actor_capability")
        if actor_node_id is None or self.memory.nodes.get(actor_node_id) is None or self.memory.nodes[actor_node_id].node_type != "actor":
            return
        for edge_id in self.memory.outgoing_edge_ids(actor_node_id):
            edge = self.memory.edges.get(edge_id)
            if edge is None or edge.stale or edge.edge_type not in {"has_permission", "lacks_permission"}:
                continue
            permission_goal_id = primary_node_goal_ids.get(edge.target_node_id)
            if permission_goal_id:
                add(goal.goal_id, permission_goal_id, f"Actor capability '{goal.title}' depends on a permission that is not yet fully verified.")


def _primary_node(goal: InvestigationGoal, expected_prefix: str) -> str | None:
    prefix = f"{expected_prefix}:"
    if not goal.goal_id.startswith(prefix):
        return None
    node_id = goal.goal_id[len(prefix):]
    return node_id if node_id in goal.supporting_graph_nodes else (goal.supporting_graph_nodes[0] if goal.supporting_graph_nodes else None)
