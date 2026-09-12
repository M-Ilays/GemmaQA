"""Metric semantic analysis — classifies a `DerivedOutputDescriptor`'s
generic underlying nature (count/sum/average/percentage/trend/backlog/...)
and finds candidate entity/state/workflow terms it might summarize.

Application-neutral: the keyword hints below are universal English
vocabulary describing metric SHAPE ("total", "average", "backlog",
"completed"), never one application's business nouns. Per the task's
explicit requirement, a candidate entity/state/workflow term is never
accepted from label wording alone — it must be CORROBORATED by actually
matching an already-known Entity/Workflow Registry term (mirrors
`workflow_candidate_builder._entity_cross_references`'s "match against
ALREADY-known terms" pattern), and every match carries its own evidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term
from app.intelligence.dependency_discovery.schemas import DependencyEvidence, DerivedOutputDescriptor, MetricDescriptor

if TYPE_CHECKING:
    from app.intelligence.entity_discovery import EntityRegistry
    from app.intelligence.workflow_discovery import WorkflowRegistry

# Ordered (hints, metric_type) checks — first match wins, deterministic.
_KEYWORD_RULES: list[tuple[tuple[str, ...], str]] = [
    (("percentage", "percent", "rate of"), "percentage"),
    (("average", "avg", "mean"), "average"),
    (("ratio", " per ", " vs "), "ratio"),
    (("backlog", "unassigned", "pending", "in queue", "open "), "backlog"),
    (("completed", "resolved", "closed", "done", "finished"), "completion"),
    (("failed", "failure", "rejected", "declined"), "failure"),
    (("warning", "overdue", "at risk", "expiring"), "warning"),
    (("alert", "notification", "unread"), "alert"),
    (("capacity", "available", "remaining", "slots"), "capacity"),
    (("utilisation", "utilization", "usage"), "utilisation"),
    (("duration", "hours", "minutes", "days since", "age of", "elapsed"), "duration"),
    (("age", "oldest", "days old"), "age_based"),
    (("revenue", "sales", "earned", "paid", "amount due"), "revenue_like"),
    (("recent", "today", "this week", "this month", "activity"), "activity"),
    (("breakdown", "by status", "distribution"), "status_distribution"),
    (("total", "sum", "grand total", "subtotal"), "total"),
    (("count", "number of", "records"), "count"),
]

_UNIT_TO_METRIC_TYPE = {"percent": "percentage", "currency": "revenue_like"}
_OUTPUT_TYPE_TO_METRIC_TYPE = {
    "chart_segment": "status_distribution",
    "alert_count": "alert",
    "notification_count": "alert",
    "queue_count": "backlog",
    "trend_indicator": "trend",
}


def classify_metric_semantic_type(output: DerivedOutputDescriptor) -> str:
    haystack = " ".join(filter(None, [output.canonical_label, *output.nearby_context])).lower()
    for hints, metric_type in _KEYWORD_RULES:
        if any(h in haystack for h in hints):
            return metric_type
    if output.output_type in _OUTPUT_TYPE_TO_METRIC_TYPE:
        return _OUTPUT_TYPE_TO_METRIC_TYPE[output.output_type]
    if output.unit in _UNIT_TO_METRIC_TYPE:
        return _UNIT_TO_METRIC_TYPE[output.unit]
    if output.output_type in {"kpi_card", "counter", "badge", "table_total", "row_count_label"} and output.is_aggregate:
        return "count"
    return "unknown"


class MetricSemanticAnalyzer:
    def analyze(
        self,
        output: DerivedOutputDescriptor,
        *,
        entity_registry: "EntityRegistry | None" = None,
        workflow_registry: "WorkflowRegistry | None" = None,
    ) -> MetricDescriptor:
        metric_type = classify_metric_semantic_type(output)
        label_terms = _candidate_terms(output.canonical_label, output.nearby_context)

        entity_terms, entity_evidence = _match_entities(label_terms, entity_registry, output)
        state_terms, state_evidence = _match_states(label_terms, entity_registry, output)
        workflow_terms, workflow_evidence = _match_workflows(label_terms, workflow_registry, output)

        confidence = 0.15
        if entity_terms:
            confidence += 0.2
        if state_terms:
            confidence += 0.15
        if workflow_terms:
            confidence += 0.15

        return MetricDescriptor(
            output_id=output.output_id,
            metric_semantic_type=metric_type,
            candidate_entity_terms=entity_terms,
            candidate_state_terms=state_terms,
            candidate_workflow_terms=workflow_terms,
            confidence=min(1.0, confidence),
            evidence=[*entity_evidence, *state_evidence, *workflow_evidence],
        )


def _candidate_terms(label: str, nearby_context: list[str]) -> set[str]:
    haystack = " ".join(filter(None, [label, *nearby_context]))
    normalized = normalize_term(haystack)
    words = normalized.split()
    terms: set[str] = set()
    terms.update(words)
    for i in range(len(words) - 1):
        terms.add(f"{words[i]} {words[i + 1]}")
    return {t for t in terms if t}


def _match_entities(
    terms: set[str], entity_registry, output: DerivedOutputDescriptor
) -> tuple[list[str], list[DependencyEvidence]]:
    if entity_registry is None:
        return [], []
    matched: list[str] = []
    evidence: list[DependencyEvidence] = []
    for record in entity_registry.all_entities():
        names = {normalize_term(record.canonical_name), *(normalize_term(a) for a in record.aliases)}
        if names & terms:
            matched.append(record.canonical_name)
            evidence.append(
                DependencyEvidence(
                    source_kind="entity_registry", observed_text=f"'{output.canonical_label}' matches entity '{record.canonical_name}'",
                    page_url=output.page_url, state_fingerprint=output.state_fingerprint,
                )
            )
    return matched, evidence


def _match_states(
    terms: set[str], entity_registry, output: DerivedOutputDescriptor
) -> tuple[list[str], list[DependencyEvidence]]:
    if entity_registry is None:
        return [], []
    matched: list[str] = []
    evidence: list[DependencyEvidence] = []
    for record in entity_registry.all_entities():
        for state in record.known_states:
            if normalize_term(state) in terms:
                matched.append(state)
                evidence.append(
                    DependencyEvidence(
                        source_kind="entity_registry", observed_text=f"'{output.canonical_label}' matches known state '{state}' of entity '{record.canonical_name}'",
                        page_url=output.page_url, state_fingerprint=output.state_fingerprint,
                    )
                )
    return matched, evidence


def _match_workflows(
    terms: set[str], workflow_registry, output: DerivedOutputDescriptor
) -> tuple[list[str], list[DependencyEvidence]]:
    if workflow_registry is None:
        return [], []
    matched: list[str] = []
    evidence: list[DependencyEvidence] = []
    for wf in workflow_registry.all_workflows():
        name_terms = normalize_term(wf.canonical_name).split()
        if any(t in terms for t in name_terms) or normalize_term(wf.canonical_name) in terms:
            matched.append(wf.canonical_name)
            evidence.append(
                DependencyEvidence(
                    source_kind="workflow_registry", observed_text=f"'{output.canonical_label}' matches workflow '{wf.canonical_name}'",
                    page_url=output.page_url, state_fingerprint=output.state_fingerprint,
                )
            )
    return matched, evidence
