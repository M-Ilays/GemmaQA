"""Exploration gaps computed from the canonical ApplicationStore.

This is what makes ApplicationStore an active input to planning instead of a
write-only record: every iteration, the store's own model of what has and hasn't
been explored is turned into structured ExplorationGap objects, which feed
ExplorationGoal creation (app.agent.goals) alongside the goals already derived
directly from the live frontier. A gap is only useful if a goal derived from it can
eventually be resolved by a real frontier candidate — see GAP_TO_GOAL_TYPE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.application.models import ExplorationStatus

if TYPE_CHECKING:
    from app.application.store import ApplicationStore

GAP_TYPES = frozenset(
    {
        "unexplored_page",
        "unexplored_candidate_url",
        "unexplored_module",
        "unvisited_form",
        "low_interaction_module",
    }
)

# Which goal_type (app.agent.goals) a gap of this type should reinforce. Chosen so the
# resulting goal is always one a real frontier candidate can eventually satisfy —
# module/page-level gaps reinforce that module's discover_navigation_region goal
# (the same goal type/anchor navigation_control candidates already produce), so a gap
# discovered while standing on a different page still pulls the run back once the
# frontier for that module is generated again.
GAP_TO_GOAL_TYPE: dict[str, str] = {
    "unexplored_page": "discover_navigation_region",
    "unexplored_module": "discover_navigation_region",
    "low_interaction_module": "discover_navigation_region",
    "unvisited_form": "inspect_form",
    "unexplored_candidate_url": "investigate_candidate_url",
}


@dataclass
class ExplorationGap:
    gap_id: str
    gap_type: str
    related_id: str | None
    severity: str  # low | medium | high
    confidence: float
    reason: str
    recommended_goal_type: str
    completion_condition: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "gap_type": self.gap_type,
            "related_id": self.related_id,
            "severity": self.severity,
            "confidence": self.confidence,
            "reason": self.reason,
            "recommended_goal_type": self.recommended_goal_type,
            "completion_condition": self.completion_condition,
        }

    @property
    def goal_anchor(self) -> str | None:
        """The anchor a goal built from this gap must use to merge with any matching
        goal a live frontier candidate would separately produce for the same target."""
        return self.related_id


def compute_gaps(
    store: "ApplicationStore | None",
    *,
    low_interaction_visit_threshold: int = 1,
) -> list["ExplorationGap"]:
    if store is None:
        return []
    model = store.model
    gaps: list[ExplorationGap] = []

    for page in model.pages:
        if page.exploration_status == ExplorationStatus.DISCOVERED and page.module_id:
            gaps.append(
                ExplorationGap(
                    gap_id=f"gap_page_{page.id}",
                    gap_type="unexplored_page",
                    related_id=page.module_id,
                    severity="medium",
                    confidence=0.7,
                    reason=(
                        f"Page {page.canonical_url} was observed but no deliberate "
                        "action has been taken there yet."
                    ),
                    recommended_goal_type=GAP_TO_GOAL_TYPE["unexplored_page"],
                    completion_condition="Page exploration_status becomes explored or blocked.",
                )
            )

    for url in model.candidate_urls:
        gaps.append(
            ExplorationGap(
                gap_id=f"gap_url_{abs(hash(url)) % 10_000_000}",
                gap_type="unexplored_candidate_url",
                related_id=url,
                severity="low",
                confidence=0.6,
                reason=f"Candidate URL {url} has been observed but never visited.",
                recommended_goal_type=GAP_TO_GOAL_TYPE["unexplored_candidate_url"],
                completion_condition="URL appears in visited_urls.",
            )
        )

    for form in model.forms:
        # AppForm.inspected is set True the instant a form's DOM structure is merely
        # observed (ApplicationStore.upsert_page -> upsert_form(..., inspected=True)),
        # so it's always true and useless as a gap signal. lifecycle_state tracks
        # whether an actual INSPECT_FORM action has run against it
        # (mark_form_inspected(), called from the controller only on real completion)
        # and stays "discovered" until then — that's the real gap.
        if form.lifecycle_state in {"discovered", ""}:
            gaps.append(
                ExplorationGap(
                    gap_id=f"gap_form_{form.id}",
                    gap_type="unvisited_form",
                    related_id=form.form_name or form.id,
                    severity="medium",
                    confidence=0.75,
                    reason=(
                        f"Form '{form.form_name}' was discovered but has not been "
                        "structurally inspected."
                    ),
                    recommended_goal_type=GAP_TO_GOAL_TYPE["unvisited_form"],
                    completion_condition="AppForm.inspected becomes true.",
                )
            )

    for module in model.modules:
        if not module.page_ids:
            continue
        visit_total = sum(p.visit_count for p in model.pages if p.id in module.page_ids)
        if visit_total <= low_interaction_visit_threshold:
            gaps.append(
                ExplorationGap(
                    gap_id=f"gap_module_low_{module.id}",
                    gap_type="low_interaction_module",
                    related_id=module.id,
                    severity="low",
                    confidence=0.5,
                    reason=(
                        f"Module '{module.name}' has only {visit_total} recorded "
                        f"visit(s) across {len(module.page_ids)} page(s)."
                    ),
                    recommended_goal_type=GAP_TO_GOAL_TYPE["low_interaction_module"],
                    completion_condition="Module's aggregate page visit count increases.",
                )
            )

    return gaps
