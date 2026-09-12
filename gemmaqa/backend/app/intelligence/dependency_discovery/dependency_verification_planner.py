"""Verification planning — turns a dependency hypothesis into an ordered,
human-or-future-milestone-executable PLAN. Never executes anything itself
(task's explicit "these are plans, not executed scenarios").

For a cross-role dependency, the plan comes from
`DependencyRelationshipBuilder.build_cross_role_finding()` (login as
producer -> create -> login as processor -> transition -> return as
consumer -> compare). For a single-actor dependency, a much shorter plan
(perform the triggering action, then compare the output) still applies —
this module supplies that fallback so every dependency with a KNOWN source
gets SOME verification plan, not just cross-role ones.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.intelligence.dependency_discovery.schemas import DependencyDescriptor, VerificationRequirement

if TYPE_CHECKING:
    from app.intelligence.dependency_discovery.dependency_registry import DependencyRegistry


class DependencyVerificationPlanner:
    def build_plan(self, descriptor: DependencyDescriptor, *, cross_role_requirements: list[VerificationRequirement] | None = None) -> list[VerificationRequirement]:
        if cross_role_requirements:
            return cross_role_requirements
        if not (descriptor.source_entity_ids or descriptor.source_workflow_ids):
            return []
        subject = descriptor.source_entity_ids[0] if descriptor.source_entity_ids else descriptor.source_workflow_ids[0]
        return [
            VerificationRequirement(
                step_type="perform_transition", description=f"Perform the action expected to affect '{descriptor.canonical_name}'",
                entity_term=subject, ordinal=0,
            ),
            VerificationRequirement(step_type="compare_output", description="Compare the output before and after", ordinal=1),
        ]

    def prioritize(self, registry: "DependencyRegistry") -> list[DependencyDescriptor]:
        """Highest-value unresolved verification opportunities first —
        pure query, no execution."""
        return registry.high_value_verification_requirements()
