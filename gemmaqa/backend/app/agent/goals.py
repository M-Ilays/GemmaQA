"""Exploration goals — the planner selects a goal first, then the best frontier
candidate that serves it, instead of picking directly from a flat candidate list.

Goals are derived deterministically from the unified frontier (app.agent.frontier);
this module does not generate candidates of its own. Completion is decided from the
live status of the candidates a goal tracks, never from free-text labels or reasons.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.agent.prerequisites import evaluate_prerequisites
from app.application.gaps import compute_gaps
from app.utils.ids import new_id

if TYPE_CHECKING:
    from app.agent.frontier import FrontierCandidate
    from app.agent.memory import RunMemory

GOAL_TYPES = frozenset(
    {
        "discover_navigation_region",
        "inspect_module",
        "inspect_submodule",
        "inspect_page",
        "inspect_form",
        "inspect_table",
        "continue_workflow",
        "unlock_application_state",
        "verify_authenticated_area",
        "revisit_partial_region",
        "investigate_candidate_url",
        "return_to_navigation_hub",
    }
)

GOAL_STATUSES = frozenset({"proposed", "active", "completed", "blocked", "deferred", "abandoned"})

TERMINAL_GOAL_STATUSES = frozenset({"completed", "abandoned"})

# Which goal_type each frontier candidate_type exists to serve. Candidate types with no
# entry here (none currently) simply aren't goal-tracked yet.
CANDIDATE_GOAL_TYPE: dict[str, str] = {
    "navigation_control": "discover_navigation_region",
    "open_url": "investigate_candidate_url",
    "inspect_form": "inspect_form",
    "inspect_table": "inspect_table",
    "safe_test_data_create": "unlock_application_state",
    "continue_active_workflow": "continue_workflow",
    "authenticate_with_credentials": "verify_authenticated_area",
    "create_test_account": "verify_authenticated_area",
    "open_registration": "verify_authenticated_area",
    "start_form_workflow": "continue_workflow",
    "continue_form_workflow": "continue_workflow",
}


@dataclass
class ExplorationGoal:
    """A first-class exploration objective the planner can select, pursue across
    several actions, and mark complete/blocked based on structured evidence."""

    goal_id: str
    goal_type: str
    title: str
    description: str = ""

    # Exact (goal_type, anchor) identity this goal is tracked and merged by — see
    # _goal_key(). Distinct from module_id/page_id/workflow_id below, which are
    # structural linkage for reporting/UI, not the matching key: an inspect_form goal
    # is anchored by form identity even though it also records the module/page it
    # lives on.
    anchor: str = ""

    module_id: str | None = None
    page_id: str | None = None
    workflow_id: str | None = None

    status: str = "proposed"
    priority: int = 100
    confidence: float = 0.5

    # Optional LLM-suggested rank (app.agent.prompts.GOAL_RANKING, set by
    # Planner.next_action via gemma.rank_goals) — used ONLY as a tie-break among
    # goals that already share the same deterministic `priority`; it can never
    # override the deterministic priority hierarchy itself. None means "not yet
    # ranked" and sorts last among ties, never first.
    llm_rank: int | None = None

    evidence: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)

    completion_criteria: str = ""
    blocked_reason: str | None = None

    created_at_iteration: int = 0
    completed_at_iteration: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "goal_type": self.goal_type,
            "title": self.title,
            "description": self.description,
            "anchor": self.anchor,
            "module_id": self.module_id,
            "page_id": self.page_id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "priority": self.priority,
            "confidence": self.confidence,
            "llm_rank": self.llm_rank,
            "evidence": list(self.evidence),
            "prerequisites": list(self.prerequisites),
            "candidate_ids": list(self.candidate_ids),
            "completion_criteria": self.completion_criteria,
            "blocked_reason": self.blocked_reason,
            "created_at_iteration": self.created_at_iteration,
            "completed_at_iteration": self.completed_at_iteration,
        }


def _anchor_for(cand: "FrontierCandidate") -> str:
    """The identity a goal is tracked by. Must be specific to what the candidate
    actually targets — e.g. two different forms in the same module must get two
    different goals, not collapse into one because they share a module_id."""
    if cand.candidate_type in {"inspect_form"}:
        return cand.form_id or cand.candidate_id
    if cand.candidate_type == "inspect_table":
        return cand.element_id or cand.candidate_id
    if cand.candidate_type == "open_url":
        return cand.url or cand.target_url or cand.candidate_id
    if cand.candidate_type == "continue_active_workflow":
        return cand.workflow_id or cand.candidate_id
    if cand.candidate_type in {"start_form_workflow", "continue_form_workflow"}:
        # Anchored by form_id (not workflow_id) so the goal created when the
        # workflow STARTS (no workflow_id assigned yet) is the same goal found again
        # once it's CONTINUING (now has a workflow_id) — one goal for the whole
        # form-completion lifecycle, not two.
        return cand.form_id or cand.candidate_id
    if cand.candidate_type in {
        "authenticate_with_credentials",
        "create_test_account",
        "open_registration",
    }:
        return "authentication"
    # navigation_control / safe_test_data_create: grouped at module (or page, if the
    # module isn't known yet) granularity — many elements on one page legitimately
    # serve the same "explore this region" objective.
    return cand.module_id or cand.source_page_id or cand.candidate_type


def _goal_key(goal_type: str, anchor: str) -> str:
    return f"{goal_type}:{anchor}"


def _prerequisites_for(goal_type: str, cand: "FrontierCandidate") -> list[str]:
    """Deterministic prerequisites for a freshly-created goal — see
    app.agent.prerequisites for the rule definitions. Most goal types have none;
    a checkout-purpose form workflow requires auth and at least one prior
    successful safe-write (e.g. a cart item), mirroring the spec's canonical
    "checkout requires auth + >= 1 cart item" example."""
    if goal_type == "continue_workflow" and (cand.metadata or {}).get("form_purpose") == "checkout_information":
        return ["authenticated", "has_completed_safe_write"]
    return []


def _title_for(goal_type: str, cand: "FrontierCandidate") -> str:
    label = cand.actual_label or cand.candidate_type
    titles = {
        "discover_navigation_region": f"Explore navigation region: {label}",
        "investigate_candidate_url": f"Investigate unvisited URL: {cand.url or cand.target_url}",
        "inspect_form": f"Inspect form: {label}",
        "inspect_table": f"Inspect table: {label}",
        "unlock_application_state": f"Create safe test data via: {label}",
        "continue_workflow": "Continue in-progress workflow",
        "verify_authenticated_area": "Resolve authentication and verify authenticated area",
    }
    return titles.get(goal_type, f"{goal_type}: {label}")


def sync_goals(
    candidates: list["FrontierCandidate"],
    memory: "RunMemory",
    *,
    iteration: int,
) -> list[ExplorationGoal]:
    """Create or refresh goal proposals from the current frontier.

    Idempotent: existing goals are matched by (goal_type, anchor) and just get their
    candidate_ids refreshed; a brand-new anchor gets a new proposed goal. Terminal
    (completed/abandoned) goals are left untouched even if matching candidates reappear
    — a completed goal does not get silently reopened.
    """
    by_key: dict[str, ExplorationGoal] = {_goal_key(g.goal_type, g.anchor): g for g in memory.goals}

    groups: dict[str, list["FrontierCandidate"]] = defaultdict(list)
    anchor_of: dict[str, str] = {}
    goal_type_of: dict[str, str] = {}
    for cand in candidates:
        if cand.status in {"exhausted", "blocked"}:
            continue
        goal_type = CANDIDATE_GOAL_TYPE.get(cand.candidate_type)
        if goal_type is None:
            continue
        anchor = _anchor_for(cand)
        key = _goal_key(goal_type, anchor)
        groups[key].append(cand)
        anchor_of[key] = anchor
        goal_type_of[key] = goal_type

    for key, cands in groups.items():
        existing = by_key.get(key)
        if existing is not None:
            if existing.status in TERMINAL_GOAL_STATUSES:
                continue
            existing.candidate_ids = [c.candidate_id for c in cands]
            existing.priority = min(existing.priority, min(c.priority for c in cands))
            continue
        goal_type = goal_type_of[key]
        head = cands[0]
        goal = ExplorationGoal(
            goal_id=new_id(),
            goal_type=goal_type,
            title=_title_for(goal_type, head),
            description=head.reason or "",
            anchor=anchor_of[key],
            module_id=head.module_id,
            page_id=head.source_page_id,
            workflow_id=head.workflow_id,
            status="proposed",
            priority=min(c.priority for c in cands),
            confidence=0.6,
            candidate_ids=[c.candidate_id for c in cands],
            prerequisites=_prerequisites_for(goal_type, head),
            completion_criteria=(
                f"All frontier candidates anchored to this {goal_type} target are "
                "attempted, exhausted, or blocked."
            ),
            created_at_iteration=iteration,
        )
        memory.goals.append(goal)
        by_key[key] = goal

    return memory.goals


def sync_gap_goals(memory: "RunMemory", *, iteration: int) -> list[ExplorationGoal]:
    """Turn ApplicationStore-derived gaps (app.application.gaps) into deferred goal
    proposals — this is what makes the store an active input to planning rather than
    a write-only record. A gap's (goal_type, anchor) key uses the exact same scheme
    sync_goals() uses for frontier candidates, so once a live candidate for the same
    target is generated (e.g. we navigate back to the page it lives on), the two
    merge into one goal instead of duplicating — see candidate_ids population there.
    """
    gaps = compute_gaps(getattr(memory, "app_store", None))
    memory.gaps = gaps

    by_key: dict[str, ExplorationGoal] = {_goal_key(g.goal_type, g.anchor): g for g in memory.goals}
    for gap in gaps:
        anchor = gap.goal_anchor
        if not anchor:
            continue
        key = _goal_key(gap.recommended_goal_type, anchor)
        existing = by_key.get(key)
        if existing is not None:
            if existing.status in TERMINAL_GOAL_STATUSES:
                continue
            if gap.reason not in existing.evidence:
                existing.evidence.append(gap.reason)
            continue
        goal = ExplorationGoal(
            goal_id=new_id(),
            goal_type=gap.recommended_goal_type,
            title=f"Close gap ({gap.gap_type}): {gap.reason}",
            description=gap.reason,
            anchor=anchor,
            status="deferred",
            # Below any goal a live candidate already backs (see NAV_PRIORITY_FLOOR-
            # style reasoning in frontier.py) until sync_goals() attaches real
            # candidates and refines this down via existing.priority = min(...).
            priority=200,
            confidence=gap.confidence,
            evidence=[gap.reason],
            candidate_ids=[],
            completion_criteria=gap.completion_condition,
            created_at_iteration=iteration,
        )
        memory.goals.append(goal)
        by_key[key] = goal
    return memory.goals


def refresh_prerequisites(memory: "RunMemory") -> None:
    """Block goals whose prerequisites aren't (yet) satisfied instead of letting
    their candidates fail or silently vanish, and reactivate them the moment the
    prerequisite becomes true. Must run before refresh_goal_status/select_active_goal
    each iteration so a freshly-blocked goal isn't also evaluated for completion this
    same pass, and a freshly-reactivated one is immediately selectable again."""
    for goal in memory.goals:
        if goal.status in TERMINAL_GOAL_STATUSES or not goal.prerequisites:
            continue
        satisfied, missing = evaluate_prerequisites(goal.prerequisites, memory)
        if not satisfied:
            if goal.status != "blocked":
                goal.status = "blocked"
                goal.blocked_reason = f"Unsatisfied prerequisite(s): {', '.join(missing)}"
                goal.evidence.append(goal.blocked_reason)
        elif goal.status == "blocked":
            goal.status = "deferred"
            goal.blocked_reason = None
            goal.evidence.append("Prerequisites satisfied — reactivated.")


def refresh_goal_status(
    candidates_by_id: dict[str, "FrontierCandidate"],
    memory: "RunMemory",
    *,
    iteration: int,
) -> None:
    """Mark goals completed/blocked once every candidate they track has reached a
    terminal state. Completion is decided purely from candidate status, never from
    free-text reasons."""
    for goal in memory.goals:
        if goal.status in TERMINAL_GOAL_STATUSES:
            continue
        if goal.status == "blocked" and goal.prerequisites:
            # Prerequisite-blocked (refresh_prerequisites) — leave it alone here;
            # it's reactivated the moment its prerequisites become true, not based
            # on candidate terminal-state, which would otherwise misread "we're not
            # currently on the right page" as "this goal is done".
            continue
        if not goal.candidate_ids:
            # Never backed by a real candidate yet — e.g. a gap-created deferred goal
            # awaiting the right page to come back into the frontier. Nothing to
            # evaluate; sync_goals() will populate candidate_ids once a matching
            # frontier candidate appears, via the same (goal_type, anchor) key. If it
            # was picked as a last-resort "active" goal anyway, demote it back rather
            # than let it squat as active forever with nothing dispatchable.
            if goal.status == "active":
                goal.status = "deferred"
            continue
        live = [candidates_by_id[cid] for cid in goal.candidate_ids if cid in candidates_by_id]
        if not live:
            goal.status = "completed"
            goal.completed_at_iteration = iteration
            goal.evidence.append("No further frontier candidates are generated for this target.")
            continue
        if all(c.status in {"exhausted", "blocked"} for c in live):
            any_attempted = any(c.status == "exhausted" for c in live)
            goal.status = "completed" if any_attempted else "blocked"
            if goal.status == "blocked":
                goal.blocked_reason = "All tracked candidates remain blocked by safety policy."
            goal.completed_at_iteration = iteration
            goal.evidence.append(
                f"All {len(live)} tracked candidate(s) reached a terminal state "
                f"({'exhausted' if any_attempted else 'blocked'})."
            )
        else:
            goal.candidate_ids = [c.candidate_id for c in live]


def select_active_goal(memory: "RunMemory") -> ExplorationGoal | None:
    """Pick the goal the planner should pursue this iteration: the highest-priority
    already-active goal, or promote the highest-priority proposed/deferred one.

    Goals already backed by real frontier candidates are preferred over ones a gap
    merely proposed but that haven't been reached yet — activating an empty goal
    doesn't restrict dispatch (candidates_for_goal falls through to the full frontier)
    but would otherwise squat as "the" active goal and starve other proposals from
    ever getting a turn.
    """
    # llm_rank is a tie-break ONLY: it can never move a goal ahead of one with a
    # strictly better (lower) priority — None (not yet ranked) sorts last among ties.
    def _key(g: ExplorationGoal) -> tuple[int, int, str]:
        return (g.priority, g.llm_rank if g.llm_rank is not None else 999_999, g.goal_id)

    active = [g for g in memory.goals if g.status == "active"]
    if active:
        return min(active, key=_key)
    pending = [g for g in memory.goals if g.status in {"proposed", "deferred"}]
    if not pending:
        return None
    backed = [g for g in pending if g.candidate_ids]
    pool = backed or pending
    best = min(pool, key=_key)
    best.status = "active"
    return best


def candidates_for_goal(
    goal: ExplorationGoal, frontier: list["FrontierCandidate"]
) -> list["FrontierCandidate"]:
    """Live, still-available frontier candidates this goal tracks, in frontier order."""
    ids = set(goal.candidate_ids)
    return [c for c in frontier if c.candidate_id in ids and c.status not in {"exhausted", "blocked"}]
