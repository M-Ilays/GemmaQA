"""Workflow Discovery Engine — the orchestrator.

Unlike Entity/Actor discovery (which classify ONE observation), workflow
discovery fundamentally needs a BEFORE/AFTER pair plus the action executed
between them — state transitions and step observation are meaningless
without knowing what changed and what caused it. `observe()` is therefore
called once per EXECUTED ACTION (not once per raw observation), from the
controller's post-action hook where both snapshots and the action are
available.

Deterministic, read-only over its inputs, never touches the browser, never
calls an LLM — same discipline as every other Perception/Intelligence layer
this package sits beside.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.intelligence.workflow_discovery.state_transition_detector import StateTransitionDetector
from app.intelligence.workflow_discovery.workflow_candidate_builder import WorkflowCandidateBuilder
from app.intelligence.workflow_discovery.workflow_gap_analyzer import WorkflowGapAnalyzer
from app.intelligence.workflow_discovery.workflow_reconstructor import WorkflowReconstructor
from app.intelligence.workflow_discovery.workflow_registry import WorkflowRegistry
from app.intelligence.workflow_discovery.workflow_relationship_builder import WorkflowRelationshipBuilder
from app.intelligence.workflow_discovery.workflow_step_extractor import WorkflowStepExtractor
from app.utils.exploration_trace import record as trace_record
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from app.intelligence.actor_discovery import ActorRegistry
    from app.intelligence.entity_discovery import EntityRegistry
    from app.perception.models import CanonicalPageModel

logger = get_logger("intelligence.workflow_discovery")


class WorkflowDiscoveryEngine:
    def __init__(self, registry: WorkflowRegistry | None = None) -> None:
        self.registry = registry or WorkflowRegistry()
        self.candidate_builder = WorkflowCandidateBuilder()
        self.step_extractor = WorkflowStepExtractor()
        self.transition_detector = StateTransitionDetector()
        self.relationship_builder = WorkflowRelationshipBuilder()
        self.reconstructor = WorkflowReconstructor()
        self.gap_analyzer = WorkflowGapAnalyzer()

    def observe(
        self,
        *,
        before_model: "CanonicalPageModel",
        after_model: "CanonicalPageModel",
        executed_element_id: str | None,
        action_succeeded: bool,
        iteration: int = 0,
        authenticated: bool = False,
        current_actor_term: str | None = None,
        primary_entity_term: str | None = None,
        entity_registry: "EntityRegistry | None" = None,
        actor_registry: "ActorRegistry | None" = None,
        analyze_gaps: bool = True,
    ) -> dict[str, Any]:
        transitions = self.transition_detector.detect(before_model, after_model)

        # Candidates are built from BOTH snapshots, not just the after-action
        # page: the control the action actually targeted lives on the BEFORE
        # page and routinely no longer exists after a redirect/re-render (the
        # common "submit -> navigate to a bare list page" case) — building
        # only from `after_model` silently lost every such step. Newly
        # revealed affordances on the after-page (e.g. an Approve button that
        # only appears once status changes) are still picked up from there.
        candidates = self._merged_candidates(
            before_model, after_model, entity_registry=entity_registry,
            primary_entity_term=primary_entity_term, current_actor_term=current_actor_term,
        )
        steps = self.step_extractor.extract(
            candidates, page_id=after_model.url, page_state_fingerprint=after_model.state_fingerprint or "",
            sequence_hint=iteration, executed_element_id=executed_element_id,
            executed_action_succeeded=action_succeeded, transitions=transitions,
        )
        handoffs = self.relationship_builder.build_handoffs(
            after_model, candidates, current_actor_term=current_actor_term, actor_registry=actor_registry
        )
        prerequisites = self.relationship_builder.build_prerequisites(
            after_model, candidates, authenticated=authenticated, current_actor_term=current_actor_term,
            actor_registry=actor_registry, handoffs=handoffs,
        )
        branches = self.relationship_builder.build_branches(after_model, candidates)

        recon_summary = self.reconstructor.reconstruct(
            self.registry, steps=steps, transitions=transitions, handoffs=handoffs,
            prerequisites=prerequisites, branches=branches, page_url=after_model.url, iteration=iteration,
            primary_entity_term=primary_entity_term,
        )
        if actor_registry is not None:
            self._populate_actor_known_workflows(recon_summary["touched_workflow_ids"], actor_registry)

        gaps = []
        if analyze_gaps and (entity_registry is not None or actor_registry is not None):
            gaps = self.gap_analyzer.analyze(self.registry, entity_registry, actor_registry)

        summary = self._summarize(after_model, steps, transitions, handoffs, branches, prerequisites, recon_summary, gaps)
        self._trace(summary)
        return summary

    def _populate_actor_known_workflows(self, touched_workflow_ids: list[str], actor_registry: "ActorRegistry") -> None:
        """`ActorRecord.known_workflows` was declared in the Actor Discovery
        Engine's schema but never populated anywhere — this is the writer."""
        for workflow_id in touched_workflow_ids:
            descriptor = next((w for w in self.registry.records.values() if w.workflow_id == workflow_id), None)
            if descriptor is None:
                continue
            for participation in descriptor.actors:
                actor = actor_registry.get(participation.actor_id)
                if actor is not None and descriptor.canonical_name not in actor.known_workflows:
                    actor.known_workflows.append(descriptor.canonical_name)

    def _merged_candidates(
        self, before_model, after_model, *, entity_registry, primary_entity_term, current_actor_term
    ) -> list:
        before_candidates = self.candidate_builder.build(
            before_model, entity_registry=entity_registry, primary_entity_term=primary_entity_term,
            current_actor_term=current_actor_term,
        )
        after_candidates = self.candidate_builder.build(
            after_model, entity_registry=entity_registry, primary_entity_term=primary_entity_term,
            current_actor_term=current_actor_term,
        )
        seen: set[tuple[str, str | None]] = set()
        merged = []
        for candidate in [*before_candidates, *after_candidates]:
            key = (candidate.candidate_type, candidate.evidence.element_id)
            if key in seen:
                continue
            seen.add(key)
            merged.append(candidate)
        return merged

    # -- tracing --------------------------------------------------------------

    def _summarize(self, model, steps, transitions, handoffs, branches, prerequisites, recon_summary, gaps) -> dict[str, Any]:
        touched = [
            self.registry.get(wid) or next((w for w in self.registry.records.values() if w.workflow_id == wid), None)
            for wid in recon_summary["touched_workflow_ids"]
        ]
        touched = [w for w in touched if w is not None]
        return {
            "url": model.url,
            "state_fingerprint": model.state_fingerprint,
            "candidates_discovered": len(steps),
            "steps_added": [
                {"step_id": s.step_id, "semantic_action": s.semantic_action, "status": s.status, "confidence": round(s.confidence, 3)}
                for s in steps
            ],
            "transitions_detected": [
                {"kind": sig.kind, "description": sig.description, "confidence": sig.confidence} for sig in transitions.signals
            ],
            "handoffs_detected": [
                {"to_actor_hint": h.to_actor_hint, "confidence": h.confidence} for h in handoffs
            ],
            "branches_found": [b.branch_type for b in branches],
            "prerequisites_attached": [p.type for p in prerequisites],
            "touched_workflows": [w.to_summary_dict() for w in touched],
            "gaps_generated": [g.gap_type for g in gaps],
            "registry_totals": {
                "total": len(self.registry.records),
                "known": len(self.registry.known_workflows()),
                "high_confidence": len(self.registry.high_confidence_workflows()),
                "cross_role": len(self.registry.cross_role_workflows()),
                "gap_count": len(self.registry.gaps),
            },
        }

    @staticmethod
    def _trace(summary: dict[str, Any]) -> None:
        trace_record("workflow_discovery.observation", **summary)
