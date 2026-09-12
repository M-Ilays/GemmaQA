"""Dependency candidate generation — connects one output's `MetricDescriptor`
to already-known application knowledge (Entity/Actor/Workflow Registry,
network evidence), producing raw `DependencyCandidate`s BEFORE any
reconstruction/merging happens. Mirrors `WorkflowCandidateBuilder`'s role in
the workflow-discovery pipeline.

Every candidate here is a STRUCTURAL hypothesis from a single observation —
temporal confirmation (does the value actually move the way the hypothesis
predicts?) is a separate concern, handled by `dependency_correlator.py`
against historical before/after observations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term, url_path_terms
from app.intelligence.dependency_discovery.schemas import (
    DependencyCandidate,
    DependencyEffect,
    DependencyEvidence,
    DependencySource,
    DependencyTarget,
    DerivedOutputDescriptor,
    MetricDescriptor,
)

if TYPE_CHECKING:
    from app.intelligence.actor_discovery import ActorRegistry
    from app.intelligence.entity_discovery import EntityRegistry
    from app.intelligence.workflow_discovery import WorkflowRegistry
    from app.perception.models import CanonicalPageModel

_ALERT_LIKE_OUTPUT_TYPES = {"alert_count", "notification_count"}
_QUEUE_LIKE_OUTPUT_TYPES = {"queue_count"}


def _evidence(source_kind: str, text: str, *, page_url: str, fingerprint: str) -> DependencyEvidence:
    return DependencyEvidence(source_kind=source_kind, observed_text=(text or "")[:160], page_url=page_url, state_fingerprint=fingerprint)


def _entity_owning_state(entity_registry, state_term: str) -> str | None:
    if entity_registry is None:
        return None
    for record in entity_registry.all_entities():
        if any(normalize_term(s) == state_term for s in record.known_states):
            return record.canonical_name
    return None


class DependencyCandidateBuilder:
    def build(
        self,
        output: DerivedOutputDescriptor,
        metric: MetricDescriptor,
        *,
        entity_registry: "EntityRegistry | None" = None,
        actor_registry: "ActorRegistry | None" = None,
        workflow_registry: "WorkflowRegistry | None" = None,
        network_evidence: list | None = None,
    ) -> list[DependencyCandidate]:
        out: list[DependencyCandidate] = []
        url, fp = output.page_url, output.state_fingerprint

        for entity_term in metric.candidate_entity_terms:
            out.append(
                DependencyCandidate(
                    dependency_type="entity_dependency",
                    relationship_type="counts" if not metric.candidate_state_terms else "filters_by_state",
                    source=DependencySource(entity_id=entity_term),
                    target=DependencyTarget(output_id=output.output_id),
                    page_url=url,
                    evidence=_evidence("entity_registry", f"entity '{entity_term}' matches output '{output.canonical_label}'", page_url=url, fingerprint=fp),
                )
            )

        for state_term in metric.candidate_state_terms:
            owning_entity = _entity_owning_state(entity_registry, normalize_term(state_term))
            out.append(
                DependencyCandidate(
                    dependency_type="state_dependency",
                    relationship_type="filters_by_state",
                    source=DependencySource(entity_id=owning_entity, state_label=state_term),
                    target=DependencyTarget(output_id=output.output_id),
                    page_url=url,
                    evidence=_evidence("entity_registry", f"state '{state_term}' matches output '{output.canonical_label}'", page_url=url, fingerprint=fp),
                )
            )

        for workflow_term in metric.candidate_workflow_terms:
            relationship = "unknown_effect"
            if output.output_type in _ALERT_LIKE_OUTPUT_TYPES:
                relationship = "creates_alert"
            elif output.output_type in _QUEUE_LIKE_OUTPUT_TYPES:
                relationship = "populates_queue"
            out.append(
                DependencyCandidate(
                    dependency_type="workflow_dependency",
                    relationship_type=relationship,
                    source=DependencySource(workflow_id=workflow_term),
                    target=DependencyTarget(output_id=output.output_id),
                    page_url=url,
                    evidence=_evidence("workflow_registry", f"workflow '{workflow_term}' matches output '{output.canonical_label}'", page_url=url, fingerprint=fp),
                )
            )

        if output.current_actor_term:
            out.append(
                DependencyCandidate(
                    dependency_type="actor_dependency",
                    relationship_type="controls_visibility",
                    source=DependencySource(actor_id=output.current_actor_term),
                    target=DependencyTarget(output_id=output.output_id),
                    page_url=url,
                    evidence=_evidence("actor_registry", f"output '{output.canonical_label}' observed under actor '{output.current_actor_term}'", page_url=url, fingerprint=fp),
                )
            )

        out.extend(self._network_candidates(output, metric, network_evidence or [], entity_registry, workflow_registry))
        return out

    # -- network resource cross-reference ------------------------------------

    @staticmethod
    def _network_candidates(output, metric, network_evidence, entity_registry, workflow_registry) -> list[DependencyCandidate]:
        out: list[DependencyCandidate] = []
        if not network_evidence:
            return out
        known_entity_terms = {normalize_term(r.canonical_name) for r in entity_registry.all_entities()} if entity_registry else set()
        known_workflow_terms = {normalize_term(w.canonical_name) for w in workflow_registry.all_workflows()} if workflow_registry else set()
        for ev in network_evidence:
            resource_text = getattr(ev, "text", None) or ""
            if not resource_text:
                continue
            terms = set(url_path_terms(resource_text)) if "/" in resource_text else set(normalize_term(resource_text).split())
            terms = {normalize_term(t) for t in terms}
            matched_entities = terms & known_entity_terms
            matched_workflows = terms & known_workflow_terms
            for term in matched_entities:
                out.append(
                    DependencyCandidate(
                        dependency_type="aggregate_dependency",
                        relationship_type="counts",
                        source=DependencySource(entity_id=term),
                        target=DependencyTarget(output_id=output.output_id),
                        page_url=output.page_url,
                        evidence=_evidence("network_endpoint", f"network resource '{resource_text}' matches entity '{term}'", page_url=output.page_url, fingerprint=output.state_fingerprint),
                    )
                )
            for term in matched_workflows:
                out.append(
                    DependencyCandidate(
                        dependency_type="aggregate_dependency",
                        relationship_type="unknown_effect",
                        source=DependencySource(workflow_id=term),
                        target=DependencyTarget(output_id=output.output_id),
                        page_url=output.page_url,
                        evidence=_evidence("network_endpoint", f"network resource '{resource_text}' matches workflow '{term}'", page_url=output.page_url, fingerprint=output.state_fingerprint),
                    )
                )
        return out
