"""Scenario Planning Engine (app.intelligence.scenario_planning).

Converts an `InvestigationGoal` (from Goal Generation) into one or more
declarative, browser-independent `InvestigationScenario` records: semantic
investigation plans describing what must be prepared, which actor is
required, which entity/workflow/state is involved, what must be observed
and compared, and what evidence would be sufficient.

This engine answers ONLY "how could this goal be investigated?" It does
NOT decide what to investigate next (Goal Generation's job), and it does
NOT implement:

  - Execution of any browser action (BrowserAdapter/ActionExecutor are
    never invoked)
  - Actor-session switching (represented declaratively via a
    `switch_actor` step type, never performed)
  - Selecting the single globally-best scenario (a future QA Strategy
    Engine's job)
  - Autonomous Investigation
  - Replacing or renaming the existing runtime `Planner`

It never rediscovers application knowledge -- every scenario traces back
to an `InvestigationGoal` and the `ApplicationKnowledgeGraph` context
around it. It never mutates the goal, the graph, or any upstream
registry; it is a read-only consumer.
"""

from app.intelligence.scenario_planning.scenario_planning_engine import ScenarioPlanningEngine
from app.intelligence.scenario_planning.schemas import InvestigationScenario, ScenarioPlanningResult

__all__ = ["ScenarioPlanningEngine", "InvestigationScenario", "ScenarioPlanningResult"]
