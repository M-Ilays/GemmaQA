"""Planning — Gemma action selection with deterministic exploration priorities."""

from __future__ import annotations

from typing import Any

from app.agent.auth_strategy import AuthenticationStrategy
from app.agent.cleanup_planner import plan_cleanup_action, plan_cleanup_navigation
from app.agent.frontier import (
    FrontierBuilder,
    FrontierCandidate,
    element_display_label,
    pending_cleanup_blocks_logout,
    this_run_owns_temporary_records,
)
from app.agent.form_workflow import GenericFormWorkflow
from app.agent.goals import (
    TERMINAL_GOAL_STATUSES,
    ExplorationGoal,
    refresh_goal_status,
    refresh_prerequisites,
    select_active_goal,
    sync_gap_goals,
    sync_goals,
)
from app.agent.memory import RunMemory, action_signature
from app.agent.priority_engine import PriorityDecision, PriorityEngine, apply_advisory_order
from app.agent.test_data import values_for_field
from app.gemma.base import ActionGenerationRequest, GemmaProvider
from app.schemas import (
    ActionCategory,
    ActionType,
    BrowserAction,
    InteractiveElement,
    PageState,
    RiskLevel,
)
from app.utils.auth_trace import trace as auth_trace
from app.utils.exploration_trace import record as exploration_trace
from app.utils.logging import get_logger

logger = get_logger("agent.planner")

# Hint lists and scoring for navigation candidates now live in app.agent.frontier —
# FrontierBuilder is the single source of every candidate type. This module only
# scores/dispatches what the frontier already generated.


def _objective_context(context: dict[str, Any]) -> dict[str, Any]:
    """The operator objective as ranking context, with absence stated explicitly.

    Every model call that could be steered by the objective gets the same
    representation, so "no objective" reads identically whether the model is
    ranking goals, breaking a candidate tie, or selecting an action.
    """
    objective = context.get("testing_objective")
    if objective is None:
        return {"testing_objective": None, "objective_status": "not_provided_by_operator"}
    return {"testing_objective": str(objective)}


def screenshot_budget_exhausted(context: dict[str, Any] | None) -> bool:
    """True when the run has already taken as many screenshots as policy allows.

    Missing `max_screenshots` (legacy/test callers) is treated as "no budget
    known" so existing tests keep planning screenshots. The live loop always
    supplies both counts from EvidenceCollector + policy.
    """
    if not context:
        return False
    max_shots = context.get("max_screenshots")
    if max_shots is None:
        return False
    try:
        return int(context.get("screenshots_taken") or 0) >= int(max_shots)
    except (TypeError, ValueError):
        return False


class Planner:
    """Ask Gemma for the next action, with deterministic priority fallbacks."""

    def __init__(self, gemma: GemmaProvider) -> None:
        self.gemma = gemma
        self.priority_engine = PriorityEngine()

    async def next_action(
        self,
        page_state: PageState,
        previous_actions: list[dict[str, Any]],
        unexplored: list[str],
        context: dict[str, Any],
        memory: RunMemory | None = None,
    ) -> BrowserAction:
        auth_trace(
            "planner.next_action.enter",
            url=page_state.url,
            has_auth_strategy=bool(memory and memory.auth_strategy),
            authenticated=bool(memory and memory.auth_strategy and memory.auth_strategy.authenticated),
        )

        # rank_goals is only a tie-break among competing exploration goals.
        # Skip it when the next action is already decided (in-flight workflow)
        # or when we are still on the login/signup path — auth/frontier picks
        # Sign up without Gemma, and the call was ~60–80s of dead wait.
        skip_rank_goals = (
            self._workflow_has_pending_step(memory)
            or self._auth_path_undecided(memory)
            or pending_cleanup_blocks_logout(memory)
            or self._leftover_cleanup_exhausted(memory)
            or this_run_owns_temporary_records(memory)
            # Local Gemma 4B times out rank_goals at ~60s (OrangeHRM run
            # 4b25765f). Same skip as analyze_page / generate_test_scenarios.
            or getattr(self.gemma, "name", "") == "openai_compatible"
        )

        if memory is not None and not skip_rank_goals:
            # LLM goal-ranking (Phase 10): advisory tie-break only — see
            # ExplorationGoal.llm_rank and select_active_goal. Applied to whatever
            # goals already exist from the previous iteration's sync_goals (this
            # iteration's sync happens inside _select_auth_candidate below); a
            # brand-new goal just simply isn't ranked yet until the next pass.
            # Never touches dispatch/execution — pure re-prioritization of goal
            # objects already fully validated and produced by the deterministic
            # frontier/goal system.
            rankable = [g for g in memory.goals if g.status not in TERMINAL_GOAL_STATUSES]
            if len(rankable) >= 2:
                try:
                    ranked_ids = await self.gemma.rank_goals(
                        [g.to_dict() for g in rankable],
                        gaps=[gap.to_dict() for gap in memory.gaps],
                        context=_objective_context(context),
                    )
                except Exception as exc:
                    logger.warning("rank_goals failed (%s); no ranking applied this iteration", type(exc).__name__)
                    ranked_ids = []
                by_goal_id = {g.goal_id: g for g in rankable}
                for idx, gid in enumerate(ranked_ids):
                    goal = by_goal_id.get(gid)
                    if goal is not None:
                        goal.llm_rank = idx

        if memory and memory.auth_strategy:
            auth: AuthenticationStrategy = memory.auth_strategy
            cont = auth.next_workflow_action()
            auth_trace(
                "planner.next_workflow_action",
                result="continue" if cont is not None else "none",
                active_workflow=auth.active_workflow.workflow_id if auth.active_workflow else None,
            )
            if cont is not None:
                cont.metadata = {
                    **(cont.metadata or {}),
                    "decision": "continue_authentication_workflow",
                    "candidate_id": (
                        f"continue_{auth.active_workflow.workflow_id}"
                        if auth.active_workflow
                        else "continue_auth"
                    ),
                }
                return cont
            auth_action = await self._select_with_priority_engine(page_state, memory, context)
            auth_trace(
                "planner.select_frontier_candidate.result",
                selected=bool(auth_action is not None),
                action=auth_action.action.value if auth_action is not None else None,
            )
            if auth_action is not None:
                return self._avoid_exhausted_screenshot(
                    auth_action, page_state, memory, context
                )
            cleanup_action = self.next_cleanup_action(page_state, memory, context)
            if cleanup_action is not None:
                return cleanup_action
            if pending_cleanup_blocks_logout(memory) and self._cleanup_allowed(memory, context):
                registry = getattr(memory, "temporary_record_registry", None)
                still_pending = bool(
                    registry is not None and registry.records_pending_cleanup()
                )
                if still_pending:
                    return self._finish(
                        "Exploration complete; leftover test records will be cleaned up",
                        code="exploration_complete",
                    )
                return self._finish(
                    "Exploration complete",
                    code="exploration_complete",
                )
            if self._leftover_cleanup_exhausted(memory):
                return self._finish(
                    "Exploration complete; leftover test records require manual cleanup",
                    code="exploration_complete",
                )
            if memory.auth_strategy.authenticated:
                return self._finish(
                    "No remaining safe candidates",
                    code="all_safe_candidates_exhausted",
                )

        if getattr(self.gemma, "name", "") == "openai_compatible" and memory is not None:
            return self._avoid_exhausted_screenshot(
                self.plan_by_priority(page_state, memory, context),
                page_state,
                memory,
                context,
            )

        frontier_dicts: list[dict[str, Any]] = []
        if memory is not None:
            frontier_dicts = [c.to_dict() for c in self._build_full_frontier(page_state, memory, context)]

        request = ActionGenerationRequest(
            page_state=page_state,
            memory=(memory.memory_snapshot() if memory else context.get("memory") or {}),
            recent_actions=previous_actions,
            visited_states=list(
                context.get("visited_states")
                or (list(memory.page_fingerprints) if memory else [])
            ),
            unexplored=unexplored,
            # The operator's objective, verbatim when supplied. The default is a
            # DESCRIPTION OF DEFAULT BEHAVIOUR, not a pretend instruction, and it
            # is paired with testing_objective_provided=False so the prompt can
            # say plainly that no objective was given.
            testing_objective=str(
                context.get("testing_objective")
                or "Explore safely, cover navigation/forms, discover defects"
            ),
            testing_objective_provided=context.get("testing_objective") is not None,
            remaining_action_budget=int(
                context.get("remaining_action_budget")
                or (memory.remaining_action_budget if memory else 20)
            ),
            remaining_page_budget=int(
                context.get("remaining_page_budget")
                or (memory.remaining_page_budget if memory else 10)
            ),
            safe_mode=bool(context.get("safe_mode", True)),
            authorized_domain=str(context.get("authorized_domain") or ""),
            screenshot_path=context.get("screenshot_path") or page_state.screenshot_path,
            frontier_candidates=frontier_dicts,
            run_id=str(context.get("run_id") or ""),
            # Canonical perception and the live memory reach the provider here —
            # the two inputs the audit found were computed every iteration and
            # then never shown to the model (J-4, J-8).
            canonical_page_model=context.get("canonical_page_model")
            or (getattr(memory, "canonical_page_model", None) if memory else None),
            run_memory=memory,
        )

        action = await self.gemma.generate_action(request)
        meta = action.metadata or {}
        if meta.get("ai_failure"):
            logger.warning(
                "AI failure path engaged fallback=%s exhausted=%s reason=%s",
                meta.get("fallback"),
                meta.get("provider_exhausted"),
                (action.reason or "")[:160],
            )
            if meta.get("provider_exhausted"):
                if memory and not meta.get("provider_exhausted_fatal"):
                    alt = self.plan_by_priority(page_state, memory, context)
                    if alt.action != ActionType.FINISH:
                        return self._avoid_exhausted_screenshot(
                            alt, page_state, memory, context
                        )
                return self._avoid_exhausted_screenshot(
                    action, page_state, memory, context
                )

        if memory and self._is_repetition(action, page_state, memory):
            logger.info("Gemma action repeated signature — using priority planner")
            action = self.plan_by_priority(page_state, memory, context)

        if memory and memory.element_failed_too_often(action.element_id):
            logger.info("Element failed too often — using priority planner")
            action = self.plan_by_priority(page_state, memory, context)

        if memory and action.action == ActionType.FINISH:
            alt = self.plan_by_priority(page_state, memory, context)
            if alt.action != ActionType.FINISH:
                logger.info(
                    "Overriding premature FINISH with deterministic action: %s",
                    alt.action.value,
                )
                return self._avoid_exhausted_screenshot(
                    alt, page_state, memory, context
                )

        action = self._avoid_exhausted_screenshot(action, page_state, memory, context)
        logger.info("Planned action: %s (%s)", action.action.value, (action.reason or "")[:120])
        return action

    @staticmethod
    def _workflow_has_pending_step(memory: RunMemory | None) -> bool:
        """True when the next planner action is already the next workflow step.

        AuthWorkflow.next_action() is a peek (does not consume). Form-workflow
        next_action() consumes a step, so we only inspect state here.
        """
        if memory is None:
            return False
        auth = memory.auth_strategy
        if auth is not None and auth.active_workflow is not None:
            if auth.active_workflow.next_action() is not None:
                return True
        wf = getattr(memory, "active_form_workflow", None)
        if wf is not None and getattr(wf, "state", None) in {
            "classified",
            "data_planned",
            "filling",
            "ready_to_submit",
        }:
            return True
        return False

    @staticmethod
    def _auth_path_undecided(memory: RunMemory | None) -> bool:
        """True while login/registration is still the job — no Gemma ranking needed."""
        if memory is None or memory.auth_strategy is None:
            return False
        return not bool(memory.auth_strategy.authenticated)

    def _build_full_frontier(
        self,
        page_state: PageState,
        memory: RunMemory,
        context: dict[str, Any],
    ) -> list[FrontierCandidate]:
        """Build the ONE canonical candidate set for this iteration — authentication,
        safe data-creation, form/table inspection, navigation, and candidate URLs all
        come from this single FrontierBuilder.build() call. Nothing downstream may
        derive a separate, competing candidate set from raw page elements."""
        auth: AuthenticationStrategy | None = memory.auth_strategy
        if auth is None:
            # No real AuthenticationStrategy wired up (e.g. a caller that only cares
            # about general navigation) — a throwaway default strategy still lets
            # FrontierBuilder produce form/table/navigation/URL candidates; it just
            # never has real credentials, so no login/registration candidate fires.
            auth = AuthenticationStrategy()
        return FrontierBuilder(auth).build(
            page_state,
            unexplored_urls=list(memory.unexplored_urls),
            allow_login=bool(context.get("allow_login", True)),
            allow_registration=bool(context.get("allow_test_account_creation", True)),
            allow_safe_test_data=bool(context.get("allow_safe_test_data_creation", False)),
            # Whether a form has actually been inspected is tracked per-form by
            # auth.form_lifecycle (DISCOVERED -> INSPECTED, updated by
            # note_form_inspected() once an INSPECT_FORM action really completes).
            # memory.known_form_ids only means "seen at least once" — remember_page()
            # adds a form to it the instant it's first observed, before this method is
            # even called, so passing that set here excluded every form from
            # inspect_form candidate generation the moment it appeared, permanently.
            inspected_form_ids=None,
            memory=memory,
        )

    # Candidate types that are already the strongest possible positive signal
    # (see priority_engine.ACTIVE_WORKFLOW_CANDIDATE_TYPES) — never penalised
    # as "off scenario" even while an investigation is active, since they ARE
    # how an in-flight auth/form workflow actually continues.
    _ALIGNMENT_EXEMPT_CANDIDATE_TYPES = frozenset(
        {"continue_active_workflow", "continue_form_workflow", "authenticate_with_credentials", "create_test_account"}
    )

    @staticmethod
    def _matches_any(haystack: str, needles: list[str]) -> bool:
        haystack = (haystack or "").lower()
        return any(n and n.lower() in haystack for n in needles if n)

    def _apply_investigation_alignment(
        self, frontier: list[FrontierCandidate], context: dict[str, Any]
    ) -> None:
        """Tags every frontier candidate with `investigation_alignment`/
        `off_scenario` from `context["investigation_hints"]` (populated only
        by `semantic_step_executor._hints_for_step` while
        AutonomousInvestigationEngine is driving a scenario step — absent in
        every standard-mode call, so this is a complete no-op there). This is
        the ONLY place a candidate's investigation-alignment is set;
        `FrontierBuilder` never computes it itself, so it stays exactly as
        neutral (`0.0`/`False`) as every other new field until this runs.

        Never invents a new candidate and never removes one — purely a
        post-build annotation pass consumed by `PriorityEngine._score()`.
        """
        hints = context.get("investigation_hints")
        if not hints:
            return
        required_ids = set(hints.get("required_control_ids") or [])
        operation = str(hints.get("intended_operation") or "")
        forbidden = list(hints.get("forbidden_alternatives") or [])
        is_cleanup = bool(hints.get("is_cleanup_step"))

        for cand in frontier:
            label_and_op = f"{cand.actual_label} {cand.semantic_operation}"
            if forbidden and self._matches_any(label_and_op, forbidden):
                continue  # never boosted; may still be picked, just not favoured
            if required_ids and (cand.element_id in required_ids or cand.form_id in required_ids):
                cand.investigation_alignment = 1.0
                continue
            if operation and self._matches_any(label_and_op, [operation]):
                cand.investigation_alignment = 0.5 if is_cleanup else 0.7
                continue
            if cand.candidate_type not in self._ALIGNMENT_EXEMPT_CANDIDATE_TYPES:
                cand.off_scenario = True

    def _select_auth_candidate(
        self,
        page_state: PageState,
        memory: RunMemory,
        context: dict[str, Any],
    ) -> BrowserAction | None:
        """Synchronous, fully deterministic candidate selection (PriorityEngine
        scoring, no Gemma call). Used by `plan_by_priority` (the "Gemma is
        stuck" fallback — consulting Gemma again there would defeat the
        purpose) and by every existing direct test call site. Planner's
        primary path (`next_action`) goes through `_select_with_priority_engine`
        instead, which additionally applies the OPTIONAL Gemma advisory tie-break."""
        decision, goal, auth = self._build_priority_decision(page_state, memory, context)
        return self._select_from_priority_decision(
            decision, goal, auth, page_state=page_state, memory=memory, context=context
        )

    async def _select_with_priority_engine(
        self,
        page_state: PageState,
        memory: RunMemory,
        context: dict[str, Any],
    ) -> BrowserAction | None:
        """The primary planning path. Builds the exact same deterministic
        `PriorityDecision` as `_select_auth_candidate`, but additionally
        consults Gemma — ADVISORY ONLY — to break a genuine near-tie among the
        top-scored candidates (see
        app.agent.priority_engine.PriorityDecision.near_tied_top). Gemma can
        never elevate a candidate outside that tied group, never override a
        hard safety rejection, and never invent a candidate — see
        `_apply_gemma_tie_break`/`apply_advisory_order`."""
        decision, goal, auth = self._build_priority_decision(page_state, memory, context)
        if len(decision.near_tied_top) >= 2 and not self._auth_path_undecided(memory):
            decision = await self._apply_gemma_tie_break(decision, context)
        return self._select_from_priority_decision(
            decision, goal, auth, page_state=page_state, memory=memory, context=context
        )

    def _build_priority_decision(
        self,
        page_state: PageState,
        memory: RunMemory,
        context: dict[str, Any],
    ) -> tuple[PriorityDecision, ExplorationGoal | None, AuthenticationStrategy | None]:
        """Build the full frontier, sync the goal system against it exactly as
        before, then hand the whole candidate set to `PriorityEngine` for
        stale-removal, transparent scoring, and safety filtering. Returns the
        decision plus the active goal/auth strategy the dispatch step needs."""
        auth: AuthenticationStrategy | None = memory.auth_strategy
        frontier = self._build_full_frontier(page_state, memory, context)
        self._apply_investigation_alignment(frontier, context)
        memory.frontier_candidate_count = len(frontier)
        auth_trace(
            "select_auth_candidate.frontier_built",
            candidates=[
                (c.candidate_id, c.candidate_type, c.priority, c.status, c.actual_label, c.semantic_operation)
                for c in frontier
            ],
        )

        iteration = len(memory.actions)
        by_id = {c.candidate_id: c for c in frontier}
        sync_goals(frontier, memory, iteration=iteration)
        sync_gap_goals(memory, iteration=iteration)
        refresh_prerequisites(memory)
        refresh_goal_status(by_id, memory, iteration=iteration)
        goal = select_active_goal(memory)

        decision = self.priority_engine.build_decision(frontier, memory=memory, page=page_state, active_goal=goal)
        return decision, goal, auth

    async def _apply_gemma_tie_break(
        self, decision: PriorityDecision, context: dict[str, Any]
    ) -> PriorityDecision:
        """Consult Gemma only about the near-tied top group, never the full
        frontier. Any failure, empty, or invalid response simply keeps the
        deterministic score order — this step can only ever narrow/reorder an
        already-decided-safe, already-decided-close set, never change what's
        eligible in the first place."""
        near = decision.near_tied_top
        if len(near) < 2:
            return decision
        summaries = [
            {
                "candidate_id": c.candidate_id,
                "candidate_type": c.candidate_type,
                "label": c.actual_label,
                "total_score": decision.scores_by_id[c.candidate_id].total_score,
                "reason": decision.selection_reason(c.candidate_id),
            }
            for c in near
        ]
        try:
            ranked_ids = await self.gemma.rank_candidates(summaries, context=_objective_context(context))
        except Exception as exc:
            logger.warning("rank_candidates failed (%s); keeping deterministic order", type(exc).__name__)
            return decision
        if not ranked_ids:
            return decision
        return apply_advisory_order(decision, ranked_ids)

    def _select_from_priority_decision(
        self,
        decision: PriorityDecision,
        goal: ExplorationGoal | None,
        auth: AuthenticationStrategy | None,
        *,
        page_state: PageState,
        memory: RunMemory,
        context: dict[str, Any],
    ) -> BrowserAction | None:
        """Walk `decision.ordered_candidates` (already stale-filtered, scored,
        safety-filtered, and sorted) and dispatch the first one that actually
        converts to a real `BrowserAction` — exactly the same "try next on
        None" behavior as before, just over the PriorityEngine's order instead
        of a bare `(priority, ...)` sort."""
        rejected: list[dict[str, Any]] = list(decision.rejected)
        for cand in decision.ordered_candidates:
            if cand.status in {"exhausted", "blocked"}:
                rejected.append(
                    {"candidate_id": cand.candidate_id, "candidate_type": cand.candidate_type, "reason": f"status={cand.status}"}
                )
                continue
            action = self._candidate_to_action(cand, page_state, memory, auth, context)
            if action is not None:
                if goal is not None and cand.candidate_id in goal.candidate_ids:
                    action.metadata = {**(action.metadata or {}), "goal_id": goal.goal_id}
                auth_trace(
                    "select_auth_candidate.dispatched",
                    candidate_id=cand.candidate_id,
                    candidate_type=cand.candidate_type,
                    label=cand.actual_label,
                    element_id=cand.element_id,
                    url=cand.url,
                )
                self._record_priority_trace(
                    decision, goal, page_state=page_state, memory=memory, selected=cand, action=action, rejected=rejected
                )
                return action
            rejected.append(
                {"candidate_id": cand.candidate_id, "candidate_type": cand.candidate_type, "reason": "dispatch_returned_none"}
            )
        auth_trace(
            "select_auth_candidate.exhausted",
            frontier_count=len(decision.ordered_candidates) + len(decision.rejected),
        )
        self._record_priority_trace(
            decision, goal, page_state=page_state, memory=memory, selected=None, action=None, rejected=rejected
        )
        return None

    @staticmethod
    def _record_priority_trace(
        decision: PriorityDecision,
        goal: ExplorationGoal | None,
        *,
        page_state: PageState,
        memory: RunMemory,
        selected: FrontierCandidate | None,
        action: BrowserAction | None,
        rejected: list[dict[str, Any]],
    ) -> None:
        """The explainable decision trace: every candidate's transparent score
        breakdown (positive factors, penalties), the active goal, whichever
        higher-risk/stale candidates were rejected and why, the one selected,
        and — derived directly from its own score breakdown, not a canned
        string — the reason it won."""
        exploration_trace(
            "iteration.plan",
            page_url=page_state.url,
            page_fingerprint=page_state.state_fingerprint,
            active_goal={"goal_id": goal.goal_id, "goal_type": goal.goal_type, "status": goal.status}
            if goal is not None
            else None,
            gap_count=len(memory.gaps),
            goal_counts={
                status: sum(1 for g in memory.goals if g.status == status)
                for status in {"proposed", "active", "completed", "blocked", "deferred", "abandoned"}
            },
            candidate_scores=[decision.scores_by_id[c.candidate_id].to_dict() for c in decision.ordered_candidates],
            candidates=[
                {"candidate_id": c.candidate_id, "candidate_type": c.candidate_type, "priority": c.priority, "status": c.status}
                for c in decision.ordered_candidates
            ],
            rejected_candidates=rejected,
            gemma_advisory_applied=decision.gemma_advisory_applied,
            gemma_advisory_order=decision.gemma_advisory_order,
            selected_candidate_id=selected.candidate_id if selected is not None else None,
            selected_action=action.action.value if action is not None else None,
            selection_reason=decision.selection_reason(selected.candidate_id) if selected is not None else None,
        )

    def _candidate_to_action(
        self,
        cand: FrontierCandidate,
        page_state: PageState,
        memory: RunMemory,
        auth: AuthenticationStrategy | None,
        context: dict[str, Any],
    ) -> BrowserAction | None:
        """Convert one frontier candidate into an executable BrowserAction, or None
        if this specific candidate can't actually proceed right now (the caller tries
        the next one — this is a per-candidate viability check, not a hard stop)."""
        if cand.candidate_type == "continue_active_workflow":
            return auth.next_workflow_action() if auth else None
        if cand.candidate_type == "authenticate_with_credentials" and auth:
            forms = [f for f in auth.detect_forms(page_state) if f.form_id == cand.form_id]
            form = forms[0] if forms else next(
                (f for f in auth.detect_forms(page_state) if f.kind == "login"), None
            )
            wf = auth.build_login_workflow(page_state, form) if form else None
            auth_trace(
                "select_auth_candidate.login",
                form_id=form.form_id if form else None,
                workflow_built=bool(wf),
                blocker=auth.blocker,
            )
            if not wf:
                return None
            action = auth.next_workflow_action()
            if action:
                action.metadata = {
                    **(action.metadata or {}),
                    "decision": "resolve_authentication_blocker",
                    "candidate_id": cand.candidate_id,
                    "confidence": 0.96,
                }
            return action
        if cand.candidate_type == "create_test_account" and auth:
            forms = [f for f in auth.detect_forms(page_state) if f.form_id == cand.form_id]
            form = forms[0] if forms else next(
                (f for f in auth.detect_forms(page_state) if f.kind == "registration"), None
            )
            wf = auth.build_registration_workflow(page_state, form) if form else None
            auth_trace(
                "select_auth_candidate.registration",
                form_id=form.form_id if form else None,
                workflow_built=bool(wf),
                step_count=len(wf.steps) if wf else 0,
            )
            if not wf:
                return None
            action = auth.next_workflow_action()
            if action:
                action.metadata = {
                    **(action.metadata or {}),
                    "decision": "resolve_authentication_blocker",
                    "candidate_id": cand.candidate_id,
                    "confidence": 0.96,
                }
            return action
        if cand.candidate_type == "open_registration" and cand.element_id:
            return BrowserAction(
                action=ActionType.CLICK,
                element_id=cand.element_id,
                reason=cand.reason,
                expected_result="Registration page opens.",
                risk=RiskLevel.LOW,
                category=ActionCategory.NAVIGATION_TEST,
                metadata={
                    "decision": "resolve_authentication_blocker",
                    "candidate_id": cand.candidate_id,
                    "action_label": "Open registration",
                },
            )
        if cand.candidate_type == "start_form_workflow" and cand.form_id:
            form = next((f for f in page_state.forms if f.form_id == cand.form_id), None)
            if form is None:
                return None
            heading = page_state.headings[0] if page_state.headings else ""
            wf = GenericFormWorkflow.start(
                form,
                page_url=page_state.url,
                heading=heading,
                interactive_elements=page_state.interactive_elements,
                run_id=memory.run_id,
            )
            if wf is None:
                return None
            memory.active_form_workflow = wf
            action = wf.next_action()
            if action is None:
                memory.active_form_workflow = None
                memory.note_form_workflow_finished(wf)
                if wf.state in {"failed", "blocked", "skipped"}:
                    memory.failed_form_workflow_counts[wf.form_id] += 1
                return None
            action.metadata = {**(action.metadata or {}), "candidate_id": cand.candidate_id}
            return action
        if cand.candidate_type == "continue_form_workflow":
            wf = memory.active_form_workflow
            if wf is None or wf.workflow_id != cand.workflow_id:
                return None
            action = wf.next_action()
            if action is None:
                # next_action() can force itself into a terminal state (e.g. its
                # internal step-budget safety net) without ever producing an action —
                # the normal post-action clearing in the controller never runs for
                # that case, so clear it here or a permanently "failed" workflow
                # object would keep blocking any new workflow from ever starting.
                if wf.state in {"verified", "failed", "blocked", "skipped"}:
                    memory.active_form_workflow = None
                    memory.note_form_workflow_finished(wf)
                if wf.state in {"failed", "blocked", "skipped"}:
                    memory.failed_form_workflow_counts[wf.form_id] += 1
                return None
            action.metadata = {**(action.metadata or {}), "candidate_id": cand.candidate_id}
            return action
        if cand.candidate_type == "safe_test_data_create" and cand.element_id:
            label = cand.actual_label or element_display_label(
                page_state, cand.element_id, fallback="Open creation form"
            )
            auth_trace(
                "select_auth_candidate.safe_test_data_create",
                element_id=cand.element_id,
                candidate_id=cand.candidate_id,
                label=label,
            )
            return BrowserAction(
                action=ActionType.CLICK,
                element_id=cand.element_id,
                reason=cand.reason,
                expected_result="Safe test-data creation workflow opens.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EXPLORATION,
                metadata={
                    "decision": "safe_test_data_creation",
                    "candidate_id": cand.candidate_id,
                    "risk_class": "safe_test_data_write",
                    "action_label": label,
                },
            )
        if cand.candidate_type == "inspect_form" and cand.form_id:
            return BrowserAction(
                action=ActionType.INSPECT_FORM,
                element_id=cand.form_id,
                reason=cand.reason,
                expected_result="Form fields recorded; positive path remains available.",
                risk=RiskLevel.LOW,
                category=ActionCategory.FORM_INSPECTION,
                metadata={"candidate_id": cand.candidate_id},
            )
        if cand.candidate_type == "inspect_table" and cand.element_id:
            return BrowserAction(
                action=ActionType.INSPECT_TABLE,
                element_id=cand.element_id,
                reason=cand.reason,
                expected_result="Table structure recorded.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EXPLORATION,
                metadata={"candidate_id": cand.candidate_id},
            )
        if cand.candidate_type == "open_url" and cand.url:
            domain = context.get("authorized_domain") or ""
            if domain and not memory.same_domain(cand.url, domain):
                return None
            if this_run_owns_temporary_records(memory) and "logout" in cand.url.lower():
                return None
            return BrowserAction(
                action=ActionType.OPEN_URL,
                url=cand.url,
                reason=cand.reason,
                expected_result="New page loads.",
                risk=RiskLevel.LOW,
                category=ActionCategory.NAVIGATION_TEST,
                metadata={
                    "candidate_id": cand.candidate_id,
                    "value_category": cand.url,
                    "action_label": "Open URL",
                },
            )
        if cand.candidate_type == "navigation_control" and cand.element_id:
            if this_run_owns_temporary_records(memory) and (
                cand.semantic_operation == "logout"
                or "logout" in (cand.actual_label or "").lower()
                or "sign out" in (cand.actual_label or "").lower()
            ):
                return None
            try:
                action_category = ActionCategory(cand.category)
            except ValueError:
                action_category = ActionCategory.EXPLORATION
            return BrowserAction(
                action=ActionType.CLICK,
                element_id=cand.element_id,
                reason=f"Click navigation: {cand.actual_label}",
                expected_result="UI navigates or reveals new content safely.",
                risk=RiskLevel.LOW,
                category=action_category,
                metadata={
                    "candidate_id": cand.candidate_id,
                    "action_label": str(cand.actual_label).strip(),
                },
            )
        # --- CanonicalPageModel-sourced candidate types ---------------------
        # Every one of these still dispatches through the existing, already-
        # validated ActionType/RiskLevel/ActionCategory vocabulary — only the
        # candidate_type (the semantic label used for scoring/tracing) is new.
        # None of this bypasses SafetyPolicy/ActionValidator, which run
        # unchanged on whatever BrowserAction is returned here.
        if cand.candidate_type == "select_tab" and cand.element_id:
            return BrowserAction(
                action=ActionType.OPEN_TAB,
                element_id=cand.element_id,
                reason=f"Select tab: {cand.actual_label}",
                expected_result="Tab panel content becomes visible.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EXPLORATION,
                metadata={"candidate_id": cand.candidate_id, "action_label": str(cand.actual_label).strip()},
            )
        if (
            cand.candidate_type
            in {
                "open_dropdown",
                "open_context_menu",
                "expand_navigation_region",
                "expand_accordion",
                "inspect_card",
                "open_table_row",
                # Opening a record from a collection — the route to that
                # record's own update/delete controls.
                "open_record_row",
                "verify_internal_link",
                "open_navigation_item",
                "open_dialog",
            }
            and cand.element_id
        ):
            try:
                action_category = ActionCategory(cand.category)
            except ValueError:
                action_category = ActionCategory.EXPLORATION
            return BrowserAction(
                action=ActionType.CLICK,
                element_id=cand.element_id,
                reason=f"{cand.candidate_type}: {cand.actual_label}",
                expected_result="UI reveals or navigates to new content safely.",
                risk=RiskLevel.LOW,
                category=action_category,
                metadata={
                    "candidate_id": cand.candidate_id,
                    "action_label": str(cand.actual_label).strip(),
                    "semantic_type": cand.semantic_type,
                },
            )
        if cand.candidate_type == "verify_external_link" and cand.element_id:
            # Never actually navigate off the authorized domain to "verify" an
            # external link — hover only confirms it exists without paying
            # external_navigation_cost.
            return BrowserAction(
                action=ActionType.HOVER,
                element_id=cand.element_id,
                reason=f"Verify external link without leaving the authorized domain: {cand.actual_label}",
                expected_result="Link target is confirmed present without navigating away.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EXPLORATION,
                metadata={"candidate_id": cand.candidate_id, "action_label": str(cand.actual_label).strip()},
            )
        if cand.candidate_type == "close_dialog" and cand.element_id:
            return BrowserAction(
                action=ActionType.PRESS,
                element_id=cand.element_id,
                key="Escape",
                reason=cand.reason,
                expected_result="Open dialog/modal closes.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EXPLORATION,
                metadata={"candidate_id": cand.candidate_id, "action_label": "Close dialog (Escape)"},
            )
        if cand.candidate_type in {"inspect_image", "inspect_document"} and cand.element_id:
            if screenshot_budget_exhausted(context):
                return None
            return BrowserAction(
                action=ActionType.TAKE_SCREENSHOT,
                element_id=cand.element_id,
                reason=cand.reason,
                expected_result="Image/document captured as evidence.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EVIDENCE_CAPTURE,
                metadata={"candidate_id": cand.candidate_id, "action_label": str(cand.actual_label).strip()},
            )
        if cand.candidate_type == "inspect_unknown_component" and cand.element_id:
            return BrowserAction(
                action=ActionType.HOVER,
                element_id=cand.element_id,
                reason=cand.reason,
                expected_result="Additional evidence (tooltip/hover state) may be revealed safely.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EXPLORATION,
                metadata={"candidate_id": cand.candidate_id, "action_label": "Inspect unknown component"},
            )
        return None

    def _is_repetition(
        self,
        action: BrowserAction,
        page_state: PageState,
        memory: RunMemory,
    ) -> bool:
        if action.action == ActionType.FINISH:
            return False
        if (action.metadata or {}).get("auth_write"):
            return False
        cat = action.category.value if action.category else ""
        value_cat = (action.metadata or {}).get("value_category")
        if cat == ActionCategory.NEGATIVE_TEST.value and value_cat == "duplicate_name":
            sig = action_signature(
                page_fingerprint=page_state.state_fingerprint,
                action_type=action.action.value,
                element_id=action.element_id,
                value_category=str(value_cat),
            )
            return memory.signature_counts.get(sig, 0) >= 2
        sig = action_signature(
            page_fingerprint=page_state.state_fingerprint,
            action_type=action.action.value,
            element_id=action.element_id,
            value_category=str(value_cat) if value_cat else None,
        )
        return memory.has_seen_signature(sig)

    def plan_by_priority(
        self,
        page_state: PageState,
        memory: RunMemory,
        context: dict[str, Any],
    ) -> BrowserAction:
        """Deterministic exploration/testing priorities when Gemma is stuck.

        Every candidate type (auth, safe data-creation, forms, tables, navigation
        links/buttons, candidate URLs) is generated by the single unified frontier
        (_select_auth_candidate → FrontierBuilder.build) and scored/dispatched from
        there — this method no longer derives its own separate candidate set."""
        frontier_action = self._select_auth_candidate(page_state, memory, context)
        if frontier_action is not None:
            return frontier_action

        if context.get("allow_controlled_writes") or context.get(
            "allow_safe_test_data_creation"
        ):
            if not context.get("safe_mode", True) or context.get(
                "allow_safe_test_data_creation"
            ):
                fill = self._safe_fill_candidate(page_state, memory)
                if fill:
                    return fill

        if memory.auth_strategy and not memory.auth_strategy.authenticated:
            blocker = memory.auth_strategy.unresolved_auth_blocker(
                page_state,
                allow_login=bool(context.get("allow_login", True)),
                allow_registration=bool(context.get("allow_test_account_creation", True)),
            )
            if blocker:
                memory.auth_blocker = blocker
                memory.auth_strategy.blocker = blocker
                return self._finish(blocker, code="unresolved_authentication")

        return self._finish(
            "No remaining same-origin navigation controls, uninspected forms, "
            "or unvisited application URLs",
            code="exploration_complete",
        )

    def _rank_elements(
        self,
        page_state: PageState,
        memory: RunMemory,
    ) -> list[tuple[InteractiveElement, str, ActionCategory]]:
        """Scoring utility only — delegates to FrontierBuilder's navigation-candidate
        generation (the single source of these candidates) and adapts the result back
        to the legacy (element, reason, category) shape some callers/tests still use.
        This method must not independently discover a different candidate set."""
        authenticated = bool(memory.auth_strategy and memory.auth_strategy.authenticated)
        candidates = FrontierBuilder._build_navigation_candidates(
            page_state, memory, authenticated=authenticated
        )
        by_id = {el.element_id: el for el in page_state.interactive_elements}
        ranked: list[tuple[InteractiveElement, str, ActionCategory]] = []
        for cand in sorted(candidates, key=lambda c: (c.priority, c.candidate_id)):
            el = by_id.get(cand.element_id)
            if el is None:
                continue
            try:
                category = ActionCategory(cand.category)
            except ValueError:
                category = ActionCategory.EXPLORATION
            ranked.append((el, cand.reason, category))
        return ranked

    def _safe_fill_candidate(
        self,
        page_state: PageState,
        memory: RunMemory,
    ) -> BrowserAction | None:
        for form in page_state.forms:
            for field in form.fields:
                if not field.element_id or field.disabled:
                    continue
                if (field.field_type or "").lower() == "password":
                    continue
                values = values_for_field(field.field_type, field.label)
                for val in values:
                    if val.category != "empty":
                        continue
                    sig = action_signature(
                        page_fingerprint=page_state.state_fingerprint,
                        action_type=ActionType.FILL.value,
                        element_id=field.element_id,
                        value_category=val.category,
                    )
                    if memory.has_seen_signature(sig):
                        continue
                    return BrowserAction(
                        action=ActionType.FILL,
                        element_id=field.element_id,
                        value=val.value,
                        reason=f"Safe empty-value check on {field.label or field.name}",
                        expected_result="Required validation should prevent submit if required.",
                        risk=RiskLevel.LOW,
                        category=ActionCategory.NEGATIVE_TEST,
                        metadata={
                            "value_category": val.category,
                            "risk_class": "safe_test_data_write",
                        },
                    )
        return None

    @staticmethod
    def _cleanup_allowed(memory: RunMemory | None, context: dict[str, Any]) -> bool:
        if context.get("allow_destructive_actions") is True:
            return True
        cfg = getattr(memory, "configuration", None) if memory is not None else None
        return bool(getattr(cfg, "allow_destructive_actions", False))

    @staticmethod
    def _leftover_cleanup_exhausted(memory: RunMemory | None) -> bool:
        """True after bounded delete retries failed (`manual_cleanup_required`).

        Those records are no longer `records_pending_cleanup()`, so the
        leftover-cleanup FINISH shortcut used to miss them and fall through
        to a 60–120s `generate_action` that still chose finish (run 1efb5323).
        """
        if memory is None:
            return False
        registry = getattr(memory, "temporary_record_registry", None)
        if registry is None:
            return False
        try:
            return bool(registry.records_requiring_manual_cleanup())
        except Exception:
            return False

    def next_cleanup_action(
        self,
        page_state: PageState,
        memory: RunMemory | None,
        context: dict[str, Any],
    ) -> BrowserAction | None:
        """Click Delete (or walk back to the record) while the session is still live.

        Cleanup used to wait until after FINISH. On Contact List that meant a
        60–120s Gemma call to say "nothing left", then a silent epilogue click
        the activity log never showed.
        """
        if memory is None or not pending_cleanup_blocks_logout(memory):
            return None
        if not self._cleanup_allowed(memory, context):
            return None
        registry = getattr(memory, "temporary_record_registry", None)
        if registry is None:
            return None
        pending = registry.records_pending_cleanup()
        # Create → list → update first. A verified-only record is not ready
        # for live delete (run 51b62f6c deleted before Edit Contact).
        ready = [e for e in pending if e.current_state in {"updated", "cleanup_failed"}]
        if not ready:
            return None
        entry = ready[0]
        action = plan_cleanup_action(
            entry, canonical_model=getattr(memory, "canonical_page_model", None)
        )
        if action is None:
            action = plan_cleanup_navigation(
                entry,
                canonical_model=getattr(memory, "canonical_page_model", None),
                current_url=page_state.url,
            )
        return action

    def _avoid_exhausted_screenshot(
        self,
        action: BrowserAction,
        page_state: PageState,
        memory: RunMemory | None,
        context: dict[str, Any],
    ) -> BrowserAction:
        """Never emit take_screenshot once the screenshot budget is gone.

        Pick the next real frontier candidate when one remains; otherwise finish
        so the live loop does not re-plan the same blocked screenshot.
        """
        if action.action != ActionType.TAKE_SCREENSHOT or not screenshot_budget_exhausted(
            context
        ):
            return action
        logger.info("Screenshot budget exhausted — not planning another take_screenshot")
        if memory is not None:
            alt = self.plan_by_priority(page_state, memory, context)
            if alt.action != ActionType.TAKE_SCREENSHOT:
                return alt
        return self._finish(
            "Screenshot budget exhausted and no other safe candidates remain",
            code="exploration_complete",
        )

    def _finish(self, reason: str, *, code: str = "exploration_complete") -> BrowserAction:
        """`code` is the canonical, truthful stop-reason vocabulary the controller
        surfaces as memory.stop_reason (see RunMemory.should_stop) — `reason` remains
        free text for logs/UI, but the code is what downstream logic and the final
        report must rely on, not string-matching the free text."""
        return BrowserAction(
            action=ActionType.FINISH,
            reason=reason,
            expected_result="Run completes and final report is generated.",
            risk=RiskLevel.LOW,
            category=ActionCategory.COMPLETION,
            metadata={"stop_reason_code": code},
        )
