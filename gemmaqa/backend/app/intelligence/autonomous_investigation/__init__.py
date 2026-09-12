"""Autonomous Investigation Engine (app.intelligence.autonomous_investigation).

The execution brain: consumes the QA Strategy Engine's execution queues,
selects the next executable scenario, drives it step-by-step through the
EXISTING runtime `Planner` -> `SafetyValidator` -> `ActionExecutor` ->
`BrowserAdapter` pipeline (never a raw Playwright call, never a new
selector-matching heuristic), verifies assertions against genuinely
observed evidence, updates the Knowledge Graph (via the SAME registry/sync
pipeline every other action already triggers), and closes the loop by
re-running Goal Generation.

It does NOT replace:

  - The runtime `Planner` -- every browser-driving step delegates to
    `Planner.plan_by_priority`, unmodified.
  - `SafetyValidator`/`ActionValidator` -- every action this engine
    produces still passes through the controller's existing per-action
    safety gate; this package's own `investigation_safety_gate.py` is an
    ADDITIONAL scenario-level check, never a substitute.
  - `BrowserAdapter`/`ActionExecutor` -- neither is imported here at all.
  - The Knowledge Graph, Goal Generation, Scenario Planning, or QA
    Strategy engines -- all are consumed read-only (plus one explicit,
    narrow write-back: re-invoking `GoalGenerationEngine.generate()` after
    the graph has already been updated by the normal pipeline, never a
    direct graph mutation).

This engine never mutates application state itself, never switches actor
sessions directly (that remains `AuthenticationStrategy`'s job, reached
via `Planner.plan_by_priority`), and never invents a goal or scenario
record of its own.
"""

from app.intelligence.autonomous_investigation.autonomous_investigation_engine import AutonomousInvestigationEngine
from app.intelligence.autonomous_investigation.schemas import InvestigationResult

__all__ = ["AutonomousInvestigationEngine", "InvestigationResult"]
