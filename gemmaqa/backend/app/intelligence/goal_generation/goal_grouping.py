"""Goal grouping -- clusters goals along the dimensions the task asks for
(entity, workflow, actor, business process, output, module, graph region,
contradiction, gap) so a caller can see "how much do we understand about
X" at a glance, not just a flat goal list.

A `business_process` group is a connected cluster of {workflow,
workflow_step, entity} nodes only -- a pragmatic, deterministic
operationalisation of "business process" from graph structure (the
Knowledge Graph has no dedicated business-process node type of its own).
A `graph_region` group is any connected component from the Knowledge
Graph's own `connected_components()` OTHER than the largest one --
grouping the largest component would just duplicate every other grouping
dimension.
"""

from __future__ import annotations

from app.intelligence.goal_generation.schemas import GoalGroup, InvestigationGoal

_BUSINESS_PROCESS_NODE_TYPES = {"workflow", "workflow_step", "entity"}
_OUTPUT_NODE_TYPES = {"derived_output", "counter", "badge", "chart", "report", "queue", "notification", "alert"}


def build_groups(goals: list[InvestigationGoal], *, graph) -> list[GoalGroup]:
    memory = graph.memory
    groups: list[GoalGroup] = []

    groups.extend(_group_by_node_type(goals, memory, node_type="entity", group_type="entity"))
    groups.extend(_group_by_node_type(goals, memory, node_type="workflow", group_type="workflow"))
    groups.extend(_group_by_node_type(goals, memory, node_type="actor", group_type="actor"))
    groups.extend(_group_by_node_type(goals, memory, node_type=_OUTPUT_NODE_TYPES, group_type="output"))
    groups.extend(_group_by_module(goals, memory))
    groups.extend(_group_by_business_process(goals, memory, graph))
    groups.extend(_group_by_graph_region(goals, memory, graph))
    groups.extend(_group_by_contradiction(goals))
    groups.extend(_group_by_gap_type(goals, memory))

    return sorted(groups, key=lambda g: g.group_id)


def _finalize(group_type: str, group_key: str, title: str, member_goals: list[InvestigationGoal]) -> GoalGroup:
    importance = sum(g.priority_score for g in member_goals) / len(member_goals)
    coverage = sum(g.confidence for g in member_goals) / len(member_goals)
    risk = sum(g.risk_score for g in member_goals) / len(member_goals)
    return GoalGroup(
        group_id=f"group:{group_type}:{group_key}", group_type=group_type, group_key=group_key, title=title,
        goal_ids=sorted(g.goal_id for g in member_goals), importance=importance, coverage=coverage, risk=risk,
        goal_count=len(member_goals),
    )


def _group_by_node_type(goals: list[InvestigationGoal], memory, *, node_type, group_type: str) -> list[GoalGroup]:
    allowed = {node_type} if isinstance(node_type, str) else set(node_type)
    by_node: dict[str, list[InvestigationGoal]] = {}
    for goal in goals:
        for node_id in goal.supporting_graph_nodes:
            node = memory.nodes.get(node_id)
            if node is not None and node.node_type in allowed:
                by_node.setdefault(node_id, []).append(goal)
    result = []
    for node_id in sorted(by_node.keys()):
        node = memory.nodes[node_id]
        result.append(_finalize(group_type, node_id, f"{group_type.capitalize()}: {node.canonical_name}", by_node[node_id]))
    return result


def _group_by_module(goals: list[InvestigationGoal], memory) -> list[GoalGroup]:
    node_to_page: dict[str, str] = {}
    for edge in memory.edges.values():
        if edge.stale or edge.edge_type != "appears_on":
            continue
        page = memory.nodes.get(edge.target_node_id)
        if page is not None and page.node_type == "page":
            node_to_page[edge.source_node_id] = edge.target_node_id

    by_page: dict[str, list[InvestigationGoal]] = {}
    for goal in goals:
        pages = {node_to_page[n] for n in goal.supporting_graph_nodes if n in node_to_page}
        for page_id in pages:
            by_page.setdefault(page_id, []).append(goal)

    result = []
    for page_id in sorted(by_page.keys()):
        page = memory.nodes.get(page_id)
        title = f"Module: {page.canonical_name}" if page is not None else f"Module: {page_id}"
        result.append(_finalize("module", page_id, title, by_page[page_id]))
    return result


def _group_by_business_process(goals: list[InvestigationGoal], memory, graph) -> list[GoalGroup]:
    adjacency: dict[str, set[str]] = {}
    for edge in memory.edges.values():
        if edge.stale:
            continue
        src, tgt = memory.nodes.get(edge.source_node_id), memory.nodes.get(edge.target_node_id)
        if src is None or tgt is None or src.node_type not in _BUSINESS_PROCESS_NODE_TYPES or tgt.node_type not in _BUSINESS_PROCESS_NODE_TYPES:
            continue
        adjacency.setdefault(edge.source_node_id, set()).add(edge.target_node_id)
        adjacency.setdefault(edge.target_node_id, set()).add(edge.source_node_id)

    seen: set[str] = set()
    components: list[set[str]] = []
    for node_id in sorted(adjacency.keys()):
        if node_id in seen:
            continue
        stack, component = [node_id], set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(adjacency.get(current, set()) - component)
        seen |= component
        components.append(component)

    result = []
    for idx, component in enumerate(sorted(components, key=lambda c: sorted(c)[0] if c else "")):
        member_goals = [g for g in goals if set(g.supporting_graph_nodes) & component]
        if not member_goals:
            continue
        result.append(_finalize("business_process", str(idx), f"Business process cluster {idx + 1} ({len(component)} node(s))", member_goals))
    return result


def _group_by_graph_region(goals: list[InvestigationGoal], memory, graph) -> list[GoalGroup]:
    components = graph.gap_analyzer.connected_components()
    if len(components) <= 1:
        return []
    largest = max(components, key=len)
    result = []
    for idx, component in enumerate(sorted((c for c in components if c is not largest), key=lambda c: sorted(c)[0] if c else "")):
        member_goals = [g for g in goals if set(g.supporting_graph_nodes) & component]
        if not member_goals:
            continue
        result.append(_finalize("graph_region", str(idx), f"Disconnected graph region {idx + 1} ({len(component)} node(s))", member_goals))
    return result


def _group_by_contradiction(goals: list[InvestigationGoal]) -> list[GoalGroup]:
    by_contradiction: dict[str, list[InvestigationGoal]] = {}
    for goal in goals:
        for c_id in goal.contradictions:
            by_contradiction.setdefault(c_id, []).append(goal)
    return [_finalize("contradiction", c_id, f"Contradiction {c_id}", member_goals) for c_id, member_goals in sorted(by_contradiction.items())]


def _group_by_gap_type(goals: list[InvestigationGoal], memory) -> list[GoalGroup]:
    by_gap_type: dict[str, list[InvestigationGoal]] = {}
    for goal in goals:
        gap_types = {memory.gaps[g_id].gap_type for g_id in goal.source_gap_ids if g_id in memory.gaps}
        for gap_type in gap_types:
            by_gap_type.setdefault(gap_type, []).append(goal)
    return [_finalize("gap", gap_type, f"Gap type: {gap_type}", member_goals) for gap_type, member_goals in sorted(by_gap_type.items())]
