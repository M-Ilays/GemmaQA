"""Dependency gap analysis — surfaces what remains UNKNOWN rather than
letting an incomplete dependency model look complete. Cross-references the
Dependency Registry against the Entity/Workflow Registries the same way
`workflow_discovery.workflow_gap_analyzer` cross-references Entity/Actor.

Every gap is a recorded observation, never an executed investigation (task's
explicit "do not execute the goal yet").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.intelligence.dependency_discovery.schemas import DependencyEvidence, DependencyGap

if TYPE_CHECKING:
    from app.intelligence.dependency_discovery.dependency_registry import DependencyRegistry
    from app.intelligence.entity_discovery import EntityRegistry
    from app.intelligence.workflow_discovery import WorkflowRegistry

_RELATIVE_TIME_HINTS = ("today", "this week", "this month", "this year")
_TENANT_HINTS = ("organisation", "organization", "workspace", "account", "company", "tenant")


def _ev(text: str) -> DependencyEvidence:
    return DependencyEvidence(source_kind="application_memory", observed_text=text[:160])


class DependencyGapAnalyzer:
    def analyze(
        self,
        registry: "DependencyRegistry",
        entity_registry: "EntityRegistry | None" = None,
        workflow_registry: "WorkflowRegistry | None" = None,
    ) -> list[DependencyGap]:
        gaps: list[DependencyGap] = []
        gaps.extend(self._output_gaps(registry))
        gaps.extend(self._dependency_structural_gaps(registry))
        if workflow_registry is not None:
            gaps.extend(self._workflow_outcome_gaps(registry, workflow_registry))
        for gap in gaps:
            registry.add_gap(gap)
        return gaps

    # -- outputs with no explaining dependency at all ------------------------

    @staticmethod
    def _output_gaps(registry: "DependencyRegistry") -> list[DependencyGap]:
        gaps: list[DependencyGap] = []
        explained_output_ids: set[str] = set()
        for dep in registry.all_dependencies():
            explained_output_ids.update(dep.target_output_ids)
        for output in registry.all_outputs():
            if output.output_id in explained_output_ids:
                continue
            if output.output_type == "kpi_card":
                gap_type = "kpi_without_source_entity"
            elif output.output_type in {"alert_count", "notification_count"}:
                gap_type = "alert_without_known_trigger"
            elif output.output_type == "queue_count":
                gap_type = "queue_without_entry_workflow"
            else:
                continue
            gaps.append(
                DependencyGap(
                    gap_type=gap_type, description=f"Output '{output.canonical_label}' has no known producing entity/workflow.",
                    related_output_ids=[output.output_id], evidence=[_ev(output.canonical_label)],
                    confidence=0.4, exploration_value=0.5, risk="low",
                    recommended_investigation_goal=f"Investigate what produces '{output.canonical_label}'.",
                )
            )
        return gaps

    # -- structural gaps on already-known dependencies -----------------------

    @staticmethod
    def _dependency_structural_gaps(registry: "DependencyRegistry") -> list[DependencyGap]:
        gaps: list[DependencyGap] = []
        for dep in registry.all_dependencies():
            if dep.source_entity_ids and not dep.source_workflow_ids:
                gaps.append(
                    DependencyGap(
                        gap_id=f"gap:{dep.dependency_id}:no-workflow", dependency_id=dep.dependency_id,
                        gap_type="output_without_producing_workflow",
                        description=f"'{dep.canonical_name}' is tied to entity '{', '.join(dep.source_entity_ids)}' but no producing workflow is known.",
                        related_entity_ids=list(dep.source_entity_ids), related_output_ids=list(dep.target_output_ids),
                        confidence=0.3, exploration_value=0.5, risk="low",
                        recommended_investigation_goal="Explore the entity's create/update workflow.",
                    )
                )
            if dep.aggregation_rule is None or dep.aggregation_rule.rule_type == "unknown":
                gaps.append(
                    DependencyGap(
                        dependency_id=dep.dependency_id, gap_type="report_total_without_aggregation_rule",
                        description=f"No confident aggregation rule known for '{dep.canonical_name}'.",
                        related_output_ids=list(dep.target_output_ids), confidence=0.3, exploration_value=0.3, risk="low",
                        recommended_investigation_goal="Correlate the output against filtered/state-scoped observations to infer its formula.",
                    )
                )
            if dep.competing_aggregation_rules and dep.aggregation_rule is not None:
                close = [r for r in dep.competing_aggregation_rules if abs(r.confidence - dep.aggregation_rule.confidence) < 0.1]
                if close:
                    gaps.append(
                        DependencyGap(
                            dependency_id=dep.dependency_id, gap_type="competing_dependency_explanations",
                            description=f"'{dep.canonical_name}' has {len(close) + 1} similarly-confident aggregation rule candidates.",
                            related_output_ids=list(dep.target_output_ids), confidence=0.3, exploration_value=0.4, risk="low",
                            recommended_investigation_goal="Correlate before/after observations to disambiguate the aggregation rule.",
                        )
                    )
            if dep.requires_actor_switch():
                gaps.append(
                    DependencyGap(
                        dependency_id=dep.dependency_id, gap_type="unresolved_role_switch",
                        description=f"'{dep.canonical_name}' requires an unresolved actor hand-off to verify.",
                        related_actor_ids=list(dep.known_producers) + list(dep.known_consumers),
                        related_output_ids=list(dep.target_output_ids), confidence=0.3, exploration_value=0.6, risk="medium",
                        recommended_investigation_goal="Identify the actor responsible for the producing workflow.",
                    )
                )
            if dep.known_producers and not dep.known_consumers:
                gaps.append(
                    DependencyGap(
                        dependency_id=dep.dependency_id, gap_type="unknown_actor_consumer",
                        description=f"'{dep.canonical_name}' has a known producer but no known consumer actor.",
                        related_actor_ids=list(dep.known_producers), related_output_ids=list(dep.target_output_ids),
                        confidence=0.3, exploration_value=0.3, risk="low",
                        recommended_investigation_goal="Observe which actor's dashboard/report surfaces this output.",
                    )
                )
            if dep.status in {"observed", "partially_observed"} and not dep.scope_dimensions:
                gaps.append(
                    DependencyGap(
                        dependency_id=dep.dependency_id, gap_type="ambiguous_scope",
                        description=f"'{dep.canonical_name}' is a real relationship but its scope (actor/tenant/time/filter) is unknown.",
                        related_output_ids=list(dep.target_output_ids), confidence=0.25, exploration_value=0.3, risk="low",
                        recommended_investigation_goal="Observe this output under a differing filter/actor/time range.",
                    )
                )
        return gaps

    # -- workflow outcomes with no visible consumer --------------------------

    @staticmethod
    def _workflow_outcome_gaps(registry: "DependencyRegistry", workflow_registry: "WorkflowRegistry") -> list[DependencyGap]:
        gaps: list[DependencyGap] = []
        workflow_ids_with_dependency = {wid for dep in registry.all_dependencies() for wid in dep.source_workflow_ids}
        for wf in workflow_registry.known_workflows():
            if not wf.outcomes:
                continue
            if wf.canonical_name in workflow_ids_with_dependency:
                continue
            gaps.append(
                DependencyGap(
                    gap_type="workflow_outcome_without_consumer",
                    description=f"Workflow '{wf.canonical_name}' has an observed outcome but no known visible output consumes it.",
                    related_workflow_ids=[wf.canonical_name], confidence=0.3, exploration_value=0.5, risk="low",
                    recommended_investigation_goal=f"Look for a dashboard/report reflecting '{wf.canonical_name}'.",
                )
            )
        return gaps
