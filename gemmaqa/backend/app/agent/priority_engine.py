"""Evidence-first exploration priority engine.

FrontierBuilder (app/agent/frontier.py) is the single source of exploration
candidates; this module is the single place that DECIDES which validated
candidate to attempt next, from generic value/risk factors — never from
application-specific names. It never generates a candidate of its own and
never talks to the browser; it only scores, filters, and orders what
FrontierBuilder already built.

Pipeline (`PriorityEngine.build_decision`):
1. Remove stale candidates (state_fingerprint no longer matches the current page).
2. Score every remaining candidate as a transparent sum of named positive and
   negative factors (see POSITIVE_WEIGHTS/NEGATIVE_WEIGHTS below).
3. Reject anything that fails a hard safety constraint (destructive/financial),
   regardless of how well it scored — this is IN ADDITION to, never a
   replacement for, SafetyPolicy/ActionValidator at execution time.
4. Sort by score, descending, with a fully deterministic tie-break
   (candidate_id, ascending) so equal-score candidates always resolve the
   same way.
5. Identify the "near-tied" top group — candidates within TIE_EPSILON of the
   leader. Planner may optionally consult Gemma to re-rank ONLY this group
   (advisory only: it can reorder within the tie, never elevate a candidate
   above it or introduce one that isn't already there).

Every score is kept as a list of named `ScoreFactor` objects (name, raw value,
weight, contribution) specifically so the resulting decision can be traced
and explained — see `PriorityDecision`/`docs` for the exploration_trace shape
this feeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from app.agent.frontier import FrontierCandidate
from app.schemas import PageState

if TYPE_CHECKING:
    from app.agent.goals import ExplorationGoal
    from app.agent.memory import RunMemory

# ---------------------------------------------------------------------------
# Named, transparent factor weights. Every number here is a tuning knob, not
# a hidden heuristic — the trace always names exactly which factors fired and
# by how much, for every scored candidate.
# ---------------------------------------------------------------------------

POSITIVE_WEIGHTS: dict[str, float] = {
    # An in-flight safe workflow (checkout, login, a generic form fill) must
    # dominate everything else — abandoning it to explore something unrelated
    # wastes the steps already spent and can leave the app in a half-finished
    # state. This is what makes "Continue Checkout" always outrank "open
    # Settings" — not a hardcoded priority tier, an actual scoring factor.
    "active_workflow_continuity": 100.0,
    # The Autonomous Investigation Engine's current scenario step is the
    # single strongest signal that can ever fire — stronger even than
    # active_workflow_continuity, since a scenario step IS the reason the
    # run is taking any action at all right now. Zero for every candidate in
    # standard (non-autonomous) mode — see
    # app.agent.planner._apply_investigation_alignment. At weight 300, a
    # required-control match (investigation_alignment=1.0 -> +300) can never
    # be outscored by the sum of every other positive factor below, even in
    # the pathological worst case where all of them fire simultaneously
    # (100+40+8+20+12+18+22+16+14+6+8+18 = 282 < 300).
    "investigation_alignment": 300.0,
    # A candidate that belongs to a goal currently gated on a prerequisite
    # (app.agent.prerequisites) — working it moves a blocked goal forward.
    "prerequisite_resolution_value": 40.0,
    "explicit_evidence_strength": 8.0,
    "expected_information_gain": 20.0,
    "business_relevance": 12.0,
    "coverage_gap_value": 18.0,
    "navigation_centrality": 22.0,
    "current_module_completion_value": 16.0,
    "novelty": 14.0,
    "reversibility": 6.0,
    "confidence": 8.0,
    # Read from FrontierCandidate.workflow_value (frontier.py's own
    # _canonical_score_hints already computes 0.8 for
    # start_form_workflow/continue_form_workflow/inspect_form/open_table_row
    # candidates) — previously computed but NEVER read by this scorer, which
    # is exactly why local form/table interaction candidates structurally
    # could never compete with navigation_centrality. This is the direct,
    # minimal fix for that gap: a real weight, not a new factor.
    "workflow_value": 18.0,
}

NEGATIVE_WEIGHTS: dict[str, float] = {
    # Destructive/financial risk is scored punitively AND hard-rejected below
    # — the weight matters only for the trace explaining *why* it would have
    # lost even without the hard rejection.
    "destructive_risk": 500.0,
    "financial_risk": 500.0,
    "external_domain_penalty": 25.0,
    "social_link_penalty": 35.0,
    "legal_link_deferral": 20.0,
    "repetition_penalty": 20.0,
    # Navigating away from a page that still carries unworked, actionable form
    # work. `active_workflow_continuity` above only fires once a workflow is
    # IN FLIGHT — between "the form was inspected" and "the workflow started"
    # there was no continuity signal at all, so a Cancel/Back button's
    # `navigation_centrality` (22) outscored `workflow_value` (18) and won.
    #
    # Observed live, twice in the same run: after inspecting a create form the
    # agent clicked Cancel and re-opened the same form two actions later; and
    # after clicking "Edit Contact" it immediately clicked Cancel, abandoning
    # the update it had spent the whole run getting to. Weighted above
    # navigation_centrality so reaching a form and then leaving it is never the
    # highest-scoring move — while staying a penalty rather than a hard
    # rejection, so a genuine dead end can still be escaped.
    "flow_abandonment_penalty": 34.0,
    "stale_state_penalty": 15.0,
    "low_information_history": 12.0,
    "failed_attempt_penalty": 30.0,
    # Not one of the task's named examples, but needed for real correctness:
    # logging out abandons the authenticated session an entire run may depend
    # on. It must remain a real, dispatchable last resort (never hard-
    # rejected like destructive/financial risk — if truly nothing else is
    # left, it must still surface) while never competing with an ordinary
    # candidate on equal footing. Regression-tested: without this, "Logout"
    # scored the same as any other reversible, never-attempted navigation
    # control and could beat a genuine safe_test_data_create candidate.
    "session_disruption_penalty": 60.0,
    # A gentle, NON-blocking deprioritisation for candidates unrelated to the
    # currently active investigation scenario step (FrontierCandidate.
    # off_scenario, set only while a scenario is active). Deliberately much
    # smaller than investigation_alignment's weight: navigation must stay
    # explorable (never "completely disable navigation"), just less
    # attractive than the step actually in progress.
    "off_scenario_penalty": 15.0,
}

# Generic, structural hint lists — universal platform/legal vocabulary, never
# one target application's business vocabulary (matches the style already
# used for NAV_HINTS/FILTER_HINTS/etc. in app.agent.frontier).
SOCIAL_DOMAIN_HINTS = (
    "facebook",
    "twitter",
    "x.com",
    "instagram",
    "linkedin",
    "youtube",
    "tiktok",
    "pinterest",
    "reddit",
    "snapchat",
    "whatsapp",
)
LEGAL_LINK_HINTS = (
    "privacy policy",
    "privacy notice",
    "terms of service",
    "terms of use",
    "terms and conditions",
    "cookie policy",
    "legal notice",
)

# candidate_types that represent continuing an already-in-flight safe
# workflow — the strongest possible positive signal.
ACTIVE_WORKFLOW_CANDIDATE_TYPES = frozenset(
    {"continue_active_workflow", "continue_form_workflow", "authenticate_with_credentials", "create_test_account"}
)


# Candidate types that represent form work available on the current page.
# Deliberately the same vocabulary the frontier uses, so "there is form work
# here" cannot drift from "the frontier is offering form work here".
_FORM_WORK_CANDIDATE_TYPES = frozenset(
    {"start_form_workflow", "continue_form_workflow", "inspect_form"}
)

# Candidate types that take the run OFF the current page. A form is only
# abandoned by leaving; interacting with something else on the same page still
# leaves the form reachable on the next iteration.
_PAGE_LEAVING_CANDIDATE_TYPES = frozenset(
    {"navigation_control", "open_url", "open_navigation_item", "verify_internal_link", "open_record_row"}
)


def _leaves_current_page(cand: FrontierCandidate) -> bool:
    if cand.semantic_operation in {"logout", "navigate"}:
        return True
    if cand.candidate_type in _PAGE_LEAVING_CANDIDATE_TYPES:
        return True
    # Cancel/Back-style controls: the frontier already recognises these as
    # flow-abandoning (see frontier.NAV_BUTTON_DEFER_HINTS) and marks them by
    # deferring their priority; treat that same recognition as leaving.
    label = (cand.actual_label or "").strip().lower()
    return any(hint in label for hint in ("cancel", "back to", "go back", "return to"))


def _is_social_link(cand: FrontierCandidate) -> bool:
    haystack = " ".join(filter(None, [cand.actual_label, cand.url, cand.target_url])).lower()
    return any(hint in haystack for hint in SOCIAL_DOMAIN_HINTS)


def _is_legal_link(cand: FrontierCandidate) -> bool:
    haystack = (cand.actual_label or "").lower()
    return cand.semantic_type == "legal_region" or any(hint in haystack for hint in LEGAL_LINK_HINTS)


@dataclass
class ScoreFactor:
    name: str
    value: float
    weight: float
    contribution: float
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 4),
            "weight": self.weight,
            "contribution": round(self.contribution, 4),
            "note": self.note,
        }


@dataclass
class CandidateScore:
    candidate_id: str
    candidate_type: str
    total_score: float
    positive_factors: list[ScoreFactor] = field(default_factory=list)
    negative_factors: list[ScoreFactor] = field(default_factory=list)
    safety_rejected: bool = False
    rejection_reason: str | None = None

    def top_positive_factor(self) -> ScoreFactor | None:
        fired = [f for f in self.positive_factors if f.contribution > 0]
        return max(fired, key=lambda f: f.contribution, default=None)

    def top_negative_factor(self) -> ScoreFactor | None:
        fired = [f for f in self.negative_factors if f.contribution < 0]
        return min(fired, key=lambda f: f.contribution, default=None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_type": self.candidate_type,
            "total_score": round(self.total_score, 4),
            "positive_factors": [f.to_dict() for f in self.positive_factors],
            "negative_factors": [f.to_dict() for f in self.negative_factors],
            "safety_rejected": self.safety_rejected,
            "rejection_reason": self.rejection_reason,
        }


@dataclass
class PriorityDecision:
    """Everything the priority engine decided for one planning iteration —
    the full, explainable basis for whichever candidate Planner ultimately
    dispatches (see Planner._record_priority_trace)."""

    ordered_candidates: list[FrontierCandidate]
    scores_by_id: dict[str, CandidateScore]
    rejected: list[dict[str, Any]]
    near_tied_top: list[FrontierCandidate]
    active_goal_id: str | None = None
    active_goal_type: str | None = None
    gemma_advisory_applied: bool = False
    gemma_advisory_order: list[str] = field(default_factory=list)

    def selection_reason(self, candidate_id: str) -> str:
        score = self.scores_by_id.get(candidate_id)
        if score is None:
            return "no score computed"
        top = score.top_positive_factor()
        parts = []
        if top is not None:
            parts.append(f"{top.name}={top.contribution:.1f}")
        worst = score.top_negative_factor()
        if worst is not None:
            parts.append(f"despite {worst.name}={worst.contribution:.1f}")
        basis = "; ".join(parts) if parts else "no positive factors fired"
        return f"highest composite score ({score.total_score:.1f}): {basis}"


class PriorityEngine:
    """Deterministic candidate scoring/selection. No LLM call anywhere in this
    class — see Planner for the OPTIONAL, advisory Gemma tie-break applied to
    `PriorityDecision.near_tied_top` only."""

    # Candidates within this many points of the leader are considered a
    # genuine tie eligible for (optional) LLM tie-breaking.
    TIE_EPSILON = 3.0
    MAX_NEAR_TIED = 5

    def build_decision(
        self,
        candidates: list[FrontierCandidate],
        *,
        memory: "RunMemory | None",
        page: PageState,
        active_goal: "ExplorationGoal | None" = None,
    ) -> PriorityDecision:
        live, stale_rejected = self._filter_stale(candidates, page.state_fingerprint)
        current_module_id = self._current_module_id(memory, page)
        # Is there form work on this page that the frontier is offering to do?
        # Computed once from the candidate set rather than from the page, so
        # "actionable" means exactly "something is available to act on" and no
        # separate eligibility rule can drift away from the frontier's own.
        unworked_form_present = any(
            c.candidate_type in _FORM_WORK_CANDIDATE_TYPES for c in live
        )

        scores: dict[str, CandidateScore] = {}
        safety_rejected: list[dict[str, Any]] = []
        survivors: list[FrontierCandidate] = []
        for cand in live:
            score = self._score(
                cand,
                memory=memory,
                active_goal=active_goal,
                current_module_id=current_module_id,
                unworked_form_present=unworked_form_present,
            )
            scores[cand.candidate_id] = score
            if score.safety_rejected:
                safety_rejected.append(
                    {
                        "candidate_id": cand.candidate_id,
                        "candidate_type": cand.candidate_type,
                        "reason": score.rejection_reason,
                    }
                )
                continue
            survivors.append(cand)

        survivors.sort(key=lambda c: (-scores[c.candidate_id].total_score, c.candidate_id))
        near_tied = self._near_tied_top(survivors, scores)

        return PriorityDecision(
            ordered_candidates=survivors,
            scores_by_id=scores,
            rejected=[*stale_rejected, *safety_rejected],
            near_tied_top=near_tied,
            active_goal_id=active_goal.goal_id if active_goal is not None else None,
            active_goal_type=active_goal.goal_type if active_goal is not None else None,
        )

    # -- stale filtering ----------------------------------------------------

    @staticmethod
    def _filter_stale(
        candidates: list[FrontierCandidate], current_fingerprint: str | None
    ) -> tuple[list[FrontierCandidate], list[dict[str, Any]]]:
        live: list[FrontierCandidate] = []
        rejected: list[dict[str, Any]] = []
        for cand in candidates:
            fp = cand.state_fingerprint or cand.source_state_fingerprint
            if fp and current_fingerprint and fp != current_fingerprint:
                rejected.append(
                    {
                        "candidate_id": cand.candidate_id,
                        "candidate_type": cand.candidate_type,
                        "reason": f"stale: candidate state_fingerprint {fp!r} != current {current_fingerprint!r}",
                    }
                )
                continue
            live.append(cand)
        return live, rejected

    # -- module context -------------------------------------------------

    @staticmethod
    def _current_module_id(memory: "RunMemory | None", page: PageState) -> str | None:
        app_store = getattr(memory, "app_store", None) if memory is not None else None
        if app_store is None:
            return None
        try:
            app_page = app_store.model.page_by_url(page.url)
        except Exception:
            return None
        return app_page.module_id if app_page is not None else None

    # -- scoring --------------------------------------------------------

    def _score(
        self,
        cand: FrontierCandidate,
        *,
        memory: "RunMemory | None",
        active_goal: "ExplorationGoal | None",
        current_module_id: str | None,
        unworked_form_present: bool = False,
    ) -> CandidateScore:
        positive: list[ScoreFactor] = []
        negative: list[ScoreFactor] = []

        def pos(name: str, raw_value: float, note: str = "") -> None:
            value = max(0.0, min(1.0, float(raw_value)))
            weight = POSITIVE_WEIGHTS[name]
            positive.append(ScoreFactor(name=name, value=value, weight=weight, contribution=value * weight, note=note))

        def neg(name: str, raw_value: float, note: str = "") -> None:
            value = max(0.0, min(1.0, float(raw_value)))
            weight = NEGATIVE_WEIGHTS[name]
            negative.append(
                ScoreFactor(name=name, value=value, weight=weight, contribution=-(value * weight), note=note)
            )

        # --- positive factors ---
        pos("investigation_alignment", cand.investigation_alignment, "matches the active investigation step" if cand.investigation_alignment else "")
        pos("workflow_value", cand.workflow_value)
        if cand.candidate_type in ACTIVE_WORKFLOW_CANDIDATE_TYPES:
            pos("active_workflow_continuity", 1.0, "candidate continues an in-flight safe workflow")
        elif cand.candidate_type == "open_registration":
            pos("active_workflow_continuity", 0.6, "opens the path toward an in-flight authentication goal")
        else:
            pos("active_workflow_continuity", 0.0)

        # Deliberately narrow: this rewards actually resolving a prerequisite
        # gate (app.agent.prerequisites), never bare "belongs to whichever
        # goal happens to be active" — a generic exploration goal with no
        # prerequisites (e.g. "discover this nav region") carries no special
        # claim over an unrelated, equally-legitimate candidate (e.g. an
        # un-inspected form) just because it was selected as active first.
        # Regression-tested: conflating the two let a same-page "Cancel" link
        # (owned by an active discover_navigation_region goal) starve out
        # inspect_form/start_form_workflow candidates on a checkout page.
        in_goal = bool(active_goal is not None and cand.candidate_id in active_goal.candidate_ids)
        if in_goal and active_goal.prerequisites:
            pos("prerequisite_resolution_value", 1.0, f"unblocks goal {active_goal.goal_id} ({active_goal.goal_type})")
        else:
            pos("prerequisite_resolution_value", 0.0)

        pos("explicit_evidence_strength", len(cand.evidence or []) / 3.0, f"{len(cand.evidence or [])} evidence item(s)")
        pos("expected_information_gain", cand.expected_information_gain)
        pos("business_relevance", cand.business_relevance)
        pos("coverage_gap_value", cand.coverage_value)
        pos("navigation_centrality", cand.navigation_centrality)

        if current_module_id and cand.module_id:
            if cand.module_id == current_module_id:
                pos("current_module_completion_value", 1.0, "same module as the current page")
            else:
                pos("current_module_completion_value", 0.3, "a different module than the current page")
        else:
            pos("current_module_completion_value", 0.0)

        pos("novelty", cand.novelty)
        pos("reversibility", 1.0 if cand.reversibility == "reversible" else 0.0)
        pos("confidence", cand.confidence)

        # --- negative factors ---
        neg("destructive_risk", 1.0 if cand.destructive_risk else 0.0)
        neg("financial_risk", 1.0 if cand.safety_class == "financial" else 0.0)
        neg("external_domain_penalty", cand.external_navigation_cost)
        neg("social_link_penalty", 1.0 if _is_social_link(cand) else 0.0, "matches a social-platform hint")
        neg("legal_link_deferral", 1.0 if _is_legal_link(cand) else 0.0, "matches a legal-page hint")
        neg("repetition_penalty", cand.repetition_penalty)
        neg(
            "stale_state_penalty",
            1.0 if cand.previous_failure_reason else 0.0,
            cand.previous_failure_reason or "",
        )
        low_info = (
            cand.already_attempted_count > 0
            and cand.novelty == 0.0
            and cand.expected_information_gain <= 0.5
        )
        neg("low_information_history", 1.0 if low_info else 0.0)
        failed_before = bool(cand.previous_failure_reason) or (
            bool(cand.element_id) and memory is not None and memory.element_failed_too_often(cand.element_id)
        )
        neg("failed_attempt_penalty", 1.0 if failed_before else 0.0)
        abandons_flow = unworked_form_present and _leaves_current_page(cand)
        neg(
            "flow_abandonment_penalty",
            1.0 if abandons_flow else 0.0,
            "leaves a page with unworked form work available" if abandons_flow else "",
        )
        neg(
            "session_disruption_penalty",
            1.0 if cand.semantic_operation == "logout" else 0.0,
            "abandons the authenticated session; last resort only" if cand.semantic_operation == "logout" else "",
        )
        neg(
            "off_scenario_penalty",
            1.0 if cand.off_scenario else 0.0,
            "unrelated to the active investigation scenario step" if cand.off_scenario else "",
        )

        total = sum(f.contribution for f in positive) + sum(f.contribution for f in negative)

        safety_rejected = bool(cand.destructive_risk) or cand.safety_class in {"destructive", "financial"}
        rejection_reason = None
        if safety_rejected:
            rejection_reason = (
                f"safety: candidate_type={cand.candidate_type!r} is classified "
                f"{cand.safety_class!r} — excluded from priority selection regardless of score"
            )

        return CandidateScore(
            candidate_id=cand.candidate_id,
            candidate_type=cand.candidate_type,
            total_score=total,
            positive_factors=positive,
            negative_factors=negative,
            safety_rejected=safety_rejected,
            rejection_reason=rejection_reason,
        )

    # -- near-tie detection for the optional Gemma advisory step ---------

    def _near_tied_top(
        self, ordered: list[FrontierCandidate], scores: dict[str, CandidateScore]
    ) -> list[FrontierCandidate]:
        if not ordered:
            return []
        top_score = scores[ordered[0].candidate_id].total_score
        group: list[FrontierCandidate] = []
        for cand in ordered[: self.MAX_NEAR_TIED]:
            if top_score - scores[cand.candidate_id].total_score <= self.TIE_EPSILON:
                group.append(cand)
            else:
                break
        return group


def apply_advisory_order(decision: PriorityDecision, ranked_candidate_ids: list[str]) -> PriorityDecision:
    """Reorder ONLY `decision.near_tied_top` according to `ranked_candidate_ids`
    (already validated by the caller against the near-tied set — never an
    invented id, never a candidate outside the tie). Everything outside the
    near-tied prefix keeps its deterministic score order untouched — Gemma's
    opinion can break a genuine tie, never outrank a clearly-better candidate."""
    near_ids = {c.candidate_id for c in decision.near_tied_top}
    valid_order = [cid for cid in ranked_candidate_ids if cid in near_ids]
    if len(valid_order) < 2:
        return decision

    by_id = {c.candidate_id: c for c in decision.ordered_candidates}
    remaining_near = [cid for cid in near_ids if cid not in valid_order]
    new_prefix_ids = [*valid_order, *remaining_near]
    new_prefix = [by_id[cid] for cid in new_prefix_ids if cid in by_id]
    rest = [c for c in decision.ordered_candidates if c.candidate_id not in near_ids]
    return replace(
        decision,
        ordered_candidates=[*new_prefix, *rest],
        gemma_advisory_applied=True,
        gemma_advisory_order=valid_order,
    )
