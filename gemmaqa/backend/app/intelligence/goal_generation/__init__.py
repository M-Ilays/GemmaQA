"""Goal Generation Engine (app.intelligence.goal_generation).

The bridge between the Application Knowledge Graph and a future Scenario
Planning Engine: it converts existing graph knowledge (gaps, contradictions,
low-confidence relationships, inferred edges, unverified nodes) into
prioritised, evidence-backed, explainable `InvestigationGoal` records.

This engine answers ONLY "what should GemmaQA investigate next?" It does
NOT decide how to investigate anything, and it does NOT implement:

  - Scenario Planning (turning a goal into a concrete sequence of actions)
  - Browser execution of any kind
  - Actor switching
  - QA Strategy selection
  - Autonomous Investigation

It never rediscovers application knowledge -- every goal traces back to an
`ApplicationKnowledgeGraph` node, edge, gap, contradiction, consistency
issue, or unresolved reference. It never mutates the Knowledge Graph or any
upstream registry; it is a read-only consumer.
"""

from app.intelligence.goal_generation.goal_generation_engine import GoalGenerationEngine
from app.intelligence.goal_generation.schemas import GoalGenerationResult, InvestigationGoal

__all__ = ["GoalGenerationEngine", "GoalGenerationResult", "InvestigationGoal"]
