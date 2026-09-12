"""QA Strategy Engine (app.intelligence.qa_strategy).

Consumes `InvestigationGoal`/`InvestigationScenario` records (from Goal
Generation and Scenario Planning) and decides WHICH scenarios should
execute, IN WHAT ORDER, and WHY. It is the strategic decision-making
layer between Scenario Planning and a future Autonomous Investigation
Engine.

This engine answers ONLY "which scenarios should run, in what order?" It
does NOT execute anything:

  - No browser action of any kind (BrowserAdapter/ActionExecutor are
    never invoked)
  - No actor-session switching
  - No mutation of application state
  - No replacing or modifying the existing runtime `Planner`

It never rediscovers application knowledge -- every recommendation traces
back to an `InvestigationGoal`, an `InvestigationScenario`, and the
`ApplicationKnowledgeGraph` context around them. It never mutates the
goal, the scenario, or the graph; it is a read-only consumer.
"""

from app.intelligence.qa_strategy.qa_strategy_engine import QAStrategyEngine
from app.intelligence.qa_strategy.schemas import StrategyResult

__all__ = ["QAStrategyEngine", "StrategyResult"]

