"""CRUD Discovery Engine — the orchestrator.

Same calling discipline as `WorkflowDiscoveryEngine`: `observe()` is called
once per EXECUTED ACTION with a before/after `CanonicalPageModel` pair, from
the controller's post-action hook. Deterministic, read-only over its
inputs, never touches the browser, never calls an LLM, and — critically —
NEVER EXECUTES A BROWSER ACTION ITSELF: this package only produces
hypotheses about what CRUD surfaces exist and how confident that claim is;
turning a hypothesis into an actual runtime action remains the Autonomous
Investigation Engine's job (or a human's), exactly like every other
discovery engine in this codebase.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.crud_discovery.crud_candidate_builder import CRUDCandidateBuilder
from app.intelligence.crud_discovery.crud_registry import CRUDRegistry
from app.utils.exploration_trace import record as trace_record
from app.utils.logging import get_logger

logger = get_logger("intelligence.crud_discovery")


class CRUDDiscoveryEngine:
    def __init__(self, registry: CRUDRegistry | None = None) -> None:
        self.registry = registry or CRUDRegistry()
        self.candidate_builder = CRUDCandidateBuilder()

    def observe(
        self,
        *,
        before_model: Any,
        after_model: Any,
        executed_element_id: str | None,
        action_succeeded: bool,
        iteration: int = 0,
        current_actor_term: str | None = None,
    ) -> dict[str, Any]:
        entry_points = [
            *self.candidate_builder.build_entry_points(before_model, current_actor_term=current_actor_term),
            *self.candidate_builder.build_entry_points(after_model, current_actor_term=current_actor_term),
        ]
        fulfilled = self.candidate_builder.build_fulfilled(
            before_model, after_model,
            executed_element_id=executed_element_id, action_succeeded=action_succeeded,
            current_actor_term=current_actor_term,
        )

        upserted = []
        for hyp in [*entry_points, *fulfilled]:
            upserted.append(self.registry.upsert(hyp))

        summary = {
            "url": after_model.url,
            "entry_points_seen": len(entry_points),
            "fulfilled_this_iteration": [h.to_summary_dict() for h in fulfilled],
            "registry_totals": self.registry.snapshot().model_dump(),
        }
        trace_record("crud_discovery.observation", iteration=iteration, **{k: v for k, v in summary.items() if k != "registry_totals"})
        return summary
