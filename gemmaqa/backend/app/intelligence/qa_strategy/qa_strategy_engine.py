"""QAStrategyEngine -- the orchestrator.

Wires together candidate building, queue assignment, batch generation,
dependency/conflict projection, forecasting, and recommendation building
into one pipeline that converts a `ScenarioPlanningEngine`'s current
scenarios + a `GoalGenerationEngine` + an `ApplicationKnowledgeGraph` into
a `StrategyResult`. This is the ONLY class other GemmaQA code (`RunMemory`,
the controller) talks to.

`generate()` runs the full pipeline as a single idempotency-tracked pass,
exactly like `ScenarioPlanningEngine.generate()`: re-running it against an
UNCHANGED scenario set must leave every candidate's `created_at`/
`observation_count` untouched, and `strategy_version` must not increment
on a no-op pass.

This engine NEVER executes a browser action, never calls BrowserAdapter/
ActionExecutor, never switches actors, and never mutates application
state -- see `__init__.py` for the full scope statement.
"""

from __future__ import annotations

from app.intelligence.qa_strategy.schemas import (
    ExecutionOrdering,
    ExecutionPolicy,
    ExecutionStrategy,
    StrategyResult,
    StrategySummary,
)
from app.intelligence.qa_strategy.strategy_batch_builder import build_batches
from app.intelligence.qa_strategy.strategy_candidate_builder import build_candidate, refine_actions
from app.intelligence.qa_strategy.strategy_dependency_graph import build_conflicts, build_dependencies
from app.intelligence.qa_strategy.strategy_forecaster import build_forecasts
from app.intelligence.qa_strategy.strategy_memory import StrategyMemory
from app.intelligence.qa_strategy.strategy_query_engine import StrategyQueryEngine
from app.intelligence.qa_strategy.strategy_queue_builder import build_queues
from app.intelligence.qa_strategy.strategy_recommendation_builder import build_recommendations
from app.intelligence.qa_strategy.strategy_scoring import POLICY_WEIGHTS
from app.utils.exploration_trace import record as trace_record


class QAStrategyEngine:
    def __init__(self, memory: StrategyMemory | None = None) -> None:
        self.memory = memory or StrategyMemory()
        self.query_engine = StrategyQueryEngine(self.memory)

    def generate(
        self, scenario_engine, goal_engine, graph, *, policy_id: str = "balanced",
        weight_overrides: dict[str, float] | None = None,
    ) -> StrategyResult:
        self.memory.begin_pass()
        if policy_id not in POLICY_WEIGHTS and policy_id != "custom_weighted":
            policy_id = "balanced"

        scenarios = list(scenario_engine.query_engine.all_scenarios()) if scenario_engine is not None else []
        goals_by_id = {}
        if goal_engine is not None:
            goals_by_id = {g.goal_id: g for g in goal_engine.query_engine.all_goals()}

        max_degree = _max_node_degree(graph)

        scenario_dependencies = list(scenario_engine.memory.dependencies.values()) if scenario_engine is not None else []
        scenario_conflicts = list(scenario_engine.memory.conflicts.values()) if scenario_engine is not None else []
        blocking_impact_by_scenario = _blocking_impact_map(scenario_dependencies, total_scenarios=len(scenarios))

        candidates = []
        for scenario in sorted(scenarios, key=lambda s: s.scenario_id):
            goal = goals_by_id.get(scenario.goal_id)
            candidate = build_candidate(
                scenario, goal, graph, max_degree=max_degree, policy_id=policy_id, weight_overrides=weight_overrides,
                blocking_impact=blocking_impact_by_scenario.get(scenario.scenario_id, 0.0),
            )
            candidates.append(candidate)

        known_ids = {c.candidate_id for c in candidates}
        dependencies = build_dependencies(scenario_dependencies, known_ids)
        conflicts = build_conflicts(scenario_conflicts, known_ids)

        refine_actions(candidates, dependencies)

        queues = build_queues(candidates)
        batches = build_batches(candidates)
        recommendations = build_recommendations(candidates, policy_id=policy_id)
        graph_stats = graph.statistics() if graph is not None else None
        forecasts = build_forecasts(candidates, graph_stats)

        ordered = sorted(candidates, key=lambda c: (-c.priority_score, c.candidate_id))
        ordering = ExecutionOrdering(
            ordering_id="ordering:global", scope="global",
            ordered_candidate_ids=[c.candidate_id for c in ordered],
            rationale=f"Sorted by descending priority_score under policy '{policy_id}'; ties broken by candidate_id.",
        )

        still_present_ids: set[str] = set()
        for candidate in sorted(candidates, key=lambda c: c.candidate_id):
            stored = self.memory.upsert_candidate(candidate)
            still_present_ids.add(stored.candidate_id)
        self.memory.remove_candidates_not_in(still_present_ids)
        self.memory.replace_queues(queues)
        self.memory.replace_batches(batches)
        self.memory.replace_dependencies(dependencies)
        self.memory.replace_conflicts(conflicts)
        self.memory.replace_recommendations(recommendations)
        self.memory.replace_forecasts(forecasts)
        self.memory.set_ordering(ordering)

        self.memory.last_graph_version = graph.memory.graph_version if graph is not None else 0
        self.memory.last_scenario_plan_version = getattr(scenario_engine.memory, "scenario_plan_version", 0) if scenario_engine is not None else 0

        policy = ExecutionPolicy(policy_id=policy_id, description=_policy_description(policy_id), weight_overrides=weight_overrides or {})
        strategy = ExecutionStrategy(
            strategy_id=f"strategy:{self.memory.strategy_version + 1}", policy=policy,
            queue_ids=[q.queue_id for q in queues], batch_ids=[b.batch_id for b in batches], ordering_id=ordering.ordering_id,
        )
        self.memory.set_strategy(strategy)

        version_bumped = self.memory.end_pass()

        statistics = self.query_engine.strategy_statistics()
        top_candidates = ordered[:10]
        summary = StrategySummary(
            summary_id=f"summary:{self.memory.strategy_version}",
            top_candidate_ids=[c.candidate_id for c in top_candidates],
            total_candidates=len(candidates),
            narrative=_build_narrative(statistics, policy_id),
            policy_used=policy_id,
        )

        result = StrategyResult(
            result_id=f"strategy-result:{self.memory.strategy_version}", strategy=strategy, candidates=self.query_engine.all_candidates(),
            queues=self.query_engine.all_queues(), batches=self.query_engine.all_batches(),
            dependencies=list(self.memory.dependencies.values()), conflicts=list(self.memory.conflicts.values()),
            recommendations=list(self.memory.recommendations.values()), forecasts=list(self.memory.forecasts.values()),
            ordering=ordering, statistics=statistics, summary=summary,
            strategy_version=self.memory.strategy_version, graph_version=self.memory.last_graph_version,
        )
        trace_record(
            "qa_strategy.generation", strategy_version=self.memory.strategy_version, version_bumped=version_bumped,
            total_candidates=statistics.total_candidates, policy_used=policy_id,
            immediate_count=statistics.candidates_by_queue.get("immediate", 0),
            blocked_count=statistics.candidates_by_queue.get("blocked", 0),
            deferred_count=statistics.candidates_by_queue.get("deferred", 0),
            batch_count=statistics.batch_count, conflict_count=statistics.conflict_count,
        )
        return result

    # -- thin pass-throughs --------------------------------------------------

    def statistics(self):
        return self.query_engine.strategy_statistics()


def _blocking_impact_map(scenario_dependencies: list, *, total_scenarios: int) -> dict[str, float]:
    """How many OTHER scenarios are unblocked once this one runs, as a
    fraction of every other scenario -- reuses Scenario Planning's own
    already-computed `ScenarioDependency` records rather than rediscovering
    dependency structure. A scenario nothing depends on scores 0.0; a
    scenario every other scenario depends on approaches 1.0."""
    denominator = max(1, total_scenarios - 1)
    counts: dict[str, int] = {}
    for dep in scenario_dependencies:
        if not dep.blocking:
            continue
        counts[dep.required_scenario_id] = counts.get(dep.required_scenario_id, 0) + 1
    return {scenario_id: min(1.0, count / denominator) for scenario_id, count in counts.items()}


def _max_node_degree(graph) -> int:
    if graph is None:
        return 1
    memory = graph.memory
    if not memory.nodes:
        return 1
    degrees = [len(memory.outgoing_edge_ids(n)) + len(memory.incoming_edge_ids(n)) for n in memory.nodes]
    return max(degrees) if degrees and max(degrees) > 0 else 1


def _policy_description(policy_id: str) -> str:
    return {
        "balanced": "Balances all twelve signals evenly across value, risk, coverage, and speed.",
        "fastest_first": "Prioritises low-complexity, quick-to-run scenarios.",
        "highest_value_first": "Prioritises scenarios tied to the highest business value.",
        "lowest_risk_first": "Prioritises read-only and low-risk scenarios ahead of mutating ones.",
        "read_only_first": "Runs every read-only scenario before any mutating scenario.",
        "coverage_first": "Prioritises scenarios that close the most graph coverage gaps.",
        "confidence_first": "Prioritises scenarios that most increase confidence in existing knowledge.",
        "business_critical_first": "Prioritises high business value combined with high risk reduction.",
        "dependency_first": "Prioritises scenarios that unblock the most other scenarios.",
        "custom_weighted": "Caller-supplied weight overrides applied on top of the balanced policy.",
    }.get(policy_id, "")


def _build_narrative(statistics, policy_id: str) -> str:
    return (
        f"Strategy pass under policy '{policy_id}': {statistics.total_candidates} candidates, "
        f"{statistics.candidates_by_action.get('execute', 0)} recommended to execute, "
        f"{statistics.candidates_by_action.get('defer', 0)} deferred, "
        f"{statistics.candidates_by_action.get('block', 0)} blocked, "
        f"{statistics.candidates_by_action.get('skip', 0)} skipped, "
        f"across {statistics.batch_count} batches."
    )
