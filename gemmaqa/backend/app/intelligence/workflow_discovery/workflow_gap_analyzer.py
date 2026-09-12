"""Workflow gap analysis — cross-references the full Workflow/Entity/Actor
Registries to surface what's still unknown or unverified. Runs over
ACCUMULATED state (unlike the per-observation relationship builder), so it
can see things like "this entity has a known status but no workflow ever
showed how it got there" that no single page observation could reveal.

Never executes an investigation goal — only names it
(`recommended_investigation_goal`), per the task's explicit scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.intelligence.workflow_discovery.schemas import WorkflowEvidence, WorkflowGap
from app.intelligence.workflow_discovery.workflow_registry import WorkflowRegistry

if TYPE_CHECKING:
    from app.intelligence.actor_discovery import ActorRegistry
    from app.intelligence.entity_discovery import EntityRegistry


class WorkflowGapAnalyzer:
    def analyze(
        self, registry: WorkflowRegistry, entity_registry: "EntityRegistry | None", actor_registry: "ActorRegistry | None"
    ) -> list[WorkflowGap]:
        gaps: list[WorkflowGap] = []
        gaps.extend(self._workflow_structural_gaps(registry))
        if entity_registry is not None:
            gaps.extend(self._entity_gaps(registry, entity_registry))
        if actor_registry is not None:
            gaps.extend(self._actor_gaps(registry, actor_registry))
        for gap in gaps:
            registry.add_gap(gap)
        return gaps

    # -- gaps derivable from the workflow registry alone ----------------------

    @staticmethod
    def _workflow_structural_gaps(registry: WorkflowRegistry) -> list[WorkflowGap]:
        gaps: list[WorkflowGap] = []
        for workflow in registry.all_workflows():
            if workflow.steps and not any(p.role_in_workflow == "initiator" for p in workflow.actors):
                gaps.append(
                    WorkflowGap(
                        workflow_id=workflow.workflow_id, gap_type="unknown_initiating_actor",
                        description=f"{workflow.canonical_name}: no actor identified as the initiator",
                        related_step_ids=[s.step_id for s in workflow.steps], confidence=0.4,
                        exploration_value=0.6, recommended_investigation_goal="observe who performs the first step of this workflow",
                    )
                )
            if not any(s.semantic_action == "create" for s in workflow.steps) and workflow.entities:
                gaps.append(
                    WorkflowGap(
                        workflow_id=workflow.workflow_id, gap_type="missing_creation_path",
                        description=f"{workflow.canonical_name}: entity participates but no creation step observed",
                        related_entity_ids=[p.entity_id for p in workflow.entities], confidence=0.4,
                        exploration_value=0.7, recommended_investigation_goal="find and exercise the creation entry point for this entity",
                    )
                )
            if workflow.known_entry_points and not workflow.known_exit_points:
                gaps.append(
                    WorkflowGap(
                        workflow_id=workflow.workflow_id, gap_type="missing_completion_path",
                        description=f"{workflow.canonical_name}: has an entry point but no observed completion",
                        confidence=0.4, exploration_value=0.7,
                        recommended_investigation_goal="continue exploring toward a completion/close/archive step",
                    )
                )
            if workflow.requires_another_actor():
                gaps.append(
                    WorkflowGap(
                        workflow_id=workflow.workflow_id, gap_type="unresolved_actor_handoff",
                        description=f"{workflow.canonical_name}: steps reference an actor that has not participated yet",
                        related_actor_ids=[p.actor_id for p in workflow.actors], confidence=0.5,
                        exploration_value=0.8, recommended_investigation_goal="identify and observe the hand-off actor's session",
                    )
                )
            for prereq in workflow.prerequisites:
                if prereq.satisfied is None:
                    gaps.append(
                        WorkflowGap(
                            workflow_id=workflow.workflow_id, gap_type="unknown_prerequisite",
                            description=f"{workflow.canonical_name}: prerequisite {prereq.type} ({prereq.target}) not yet resolved",
                            confidence=0.35, exploration_value=0.5,
                            recommended_investigation_goal=f"determine whether prerequisite {prereq.type} is satisfied",
                        )
                    )
            for outcome in workflow.outcomes:
                if outcome.produced_by_step_id is None:
                    gaps.append(
                        WorkflowGap(
                            workflow_id=workflow.workflow_id, gap_type="outcome_without_producer",
                            description=f"{workflow.canonical_name}: outcome {outcome.description!r} has no known producing step",
                            confidence=0.35, exploration_value=0.6,
                            recommended_investigation_goal="find which action produces this outcome",
                        )
                    )
            for step in workflow.steps:
                if step.status == "blocked" and not any(o.produced_by_step_id == step.step_id for o in workflow.outcomes):
                    gaps.append(
                        WorkflowGap(
                            workflow_id=workflow.workflow_id, gap_type="mutation_without_observed_result",
                            description=f"{workflow.canonical_name}: step {step.semantic_action} attempted but no result observed",
                            related_step_ids=[step.step_id], confidence=0.3, exploration_value=0.5,
                            recommended_investigation_goal="retry this step and observe its outcome",
                        )
                    )
        return gaps

    # -- gaps needing the Entity Registry -------------------------------------

    @staticmethod
    def _entity_gaps(registry: WorkflowRegistry, entity_registry: "EntityRegistry") -> list[WorkflowGap]:
        gaps: list[WorkflowGap] = []
        for entity in entity_registry.known_entities():
            related_workflows = registry.workflows_by_entity(entity.canonical_name)
            if entity.known_states and not any(
                any(t.entity_id == entity.canonical_name for t in w.transitions) for w in related_workflows
            ):
                gaps.append(
                    WorkflowGap(
                        gap_type="status_without_changing_action",
                        description=f"entity {entity.canonical_name!r} has known states {entity.known_states} but no observed status-changing action",
                        related_entity_ids=[entity.canonical_name], confidence=0.35, exploration_value=0.6,
                        recommended_investigation_goal=f"find the control that changes {entity.canonical_name}'s status",
                    )
                )
            for rel in entity.relationships:
                other = next((e for e in entity_registry.all_entities() if e.entity_id == rel.object_entity_id), None)
                if other is None:
                    continue
                connected = any(
                    any(p.entity_id == entity.canonical_name for p in w.entities) and any(p.entity_id == other.canonical_name for p in w.entities)
                    for w in registry.all_workflows()
                )
                if not connected:
                    gaps.append(
                        WorkflowGap(
                            gap_type="relationship_without_workflow",
                            description=f"{entity.canonical_name} {rel.kind} {other.canonical_name}, but no workflow connects them",
                            related_entity_ids=[entity.canonical_name, other.canonical_name], confidence=0.3, exploration_value=0.5,
                            recommended_investigation_goal=f"find the workflow linking {entity.canonical_name} and {other.canonical_name}",
                        )
                    )
        return gaps

    # -- gaps needing the Actor Registry ---------------------------------------

    @staticmethod
    def _actor_gaps(registry: WorkflowRegistry, actor_registry: "ActorRegistry") -> list[WorkflowGap]:
        gaps: list[WorkflowGap] = []
        for actor in actor_registry.known_actors():
            for permission_id in actor.granted_permission_ids():
                used = any(
                    any(s.actor_id == actor.canonical_name for s in w.steps) for w in registry.workflows_by_actor(actor.canonical_name)
                )
                if not used:
                    gaps.append(
                        WorkflowGap(
                            gap_type="permission_without_workflow",
                            description=f"actor {actor.canonical_name!r} has permission {permission_id!r} but no known workflow uses it",
                            related_actor_ids=[actor.canonical_name], confidence=0.3, exploration_value=0.5,
                            recommended_investigation_goal=f"exercise the control granting {permission_id}",
                        )
                    )
                    break  # one gap per actor is enough signal, avoid noise
            for dashboard_url in actor.known_dashboards:
                if not any(dashboard_url in w.source_pages or dashboard_url in w.known_exit_points for w in registry.all_workflows()):
                    gaps.append(
                        WorkflowGap(
                            gap_type="dashboard_without_source_workflow",
                            description=f"dashboard {dashboard_url!r} has no known source workflow",
                            related_actor_ids=[actor.canonical_name], confidence=0.3, exploration_value=0.55,
                            recommended_investigation_goal="trace which workflow produces this dashboard's data",
                            evidence=[WorkflowEvidence(source_kind="application_memory", observed_text=dashboard_url, page_url=dashboard_url)],
                        )
                    )
        return gaps
