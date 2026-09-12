"""CRUD Registry — stores `CRUDWorkflowHypothesis` records, keyed by a
deterministic dedup key so re-observing the same Add/Edit/Delete control
across iterations upserts the existing hypothesis (merging evidence, raising
`observation_count`) rather than minting duplicates."""

from __future__ import annotations

from datetime import datetime

from app.intelligence.crud_discovery.schemas import CRUDRegistrySnapshot, CRUDWorkflowHypothesis

_STATUS_RANK = {"entry_point": 0, "supported": 1, "executed": 2, "verified": 3, "contradicted": -1}


class CRUDRegistry:
    def __init__(self) -> None:
        self.records: dict[str, CRUDWorkflowHypothesis] = {}

    def upsert(self, hypothesis: CRUDWorkflowHypothesis) -> CRUDWorkflowHypothesis:
        key = hypothesis.dedup_key()
        existing = self.records.get(key)
        if existing is None:
            self.records[key] = hypothesis
            return hypothesis

        # Never downgrade a stronger status with a weaker re-observation;
        # never lose evidence already collected.
        if _STATUS_RANK.get(hypothesis.status, 0) >= _STATUS_RANK.get(existing.status, 0):
            existing.status = hypothesis.status
            existing.confidence = max(existing.confidence, hypothesis.confidence)
        existing.evidence = _merge_evidence(existing.evidence, hypothesis.evidence)
        existing.required_controls = sorted(set(existing.required_controls) | set(hypothesis.required_controls))
        existing.required_form_id = existing.required_form_id or hypothesis.required_form_id
        existing.target_state = hypothesis.target_state or existing.target_state
        existing.expected_transition = hypothesis.expected_transition or existing.expected_transition
        existing.verification_surface = existing.verification_surface or hypothesis.verification_surface
        if hypothesis.cleanup_possibility != "unknown":
            existing.cleanup_possibility = hypothesis.cleanup_possibility
        if hypothesis.safety_classification != "unknown":
            existing.safety_classification = hypothesis.safety_classification
        existing.actor_hypothesis = existing.actor_hypothesis or hypothesis.actor_hypothesis
        existing.last_seen = datetime.utcnow()
        existing.observation_count += 1
        return existing

    def all_hypotheses(self) -> list[CRUDWorkflowHypothesis]:
        return list(self.records.values())

    def by_operation(self, operation: str) -> list[CRUDWorkflowHypothesis]:
        return [h for h in self.records.values() if h.operation == operation]

    def supported_or_better(self) -> list[CRUDWorkflowHypothesis]:
        return [h for h in self.records.values() if _STATUS_RANK.get(h.status, 0) >= _STATUS_RANK["supported"]]

    def destructive_hypotheses(self) -> list[CRUDWorkflowHypothesis]:
        return [h for h in self.records.values() if h.safety_classification == "destructive"]

    def snapshot(self) -> CRUDRegistrySnapshot:
        by_op: dict[str, int] = {}
        for h in self.records.values():
            by_op[h.operation] = by_op.get(h.operation, 0) + 1
        return CRUDRegistrySnapshot(
            hypothesis_count=len(self.records),
            by_operation=by_op,
            entry_point_only=[h.hypothesis_id for h in self.records.values() if h.status == "entry_point"],
            supported_or_better=[h.hypothesis_id for h in self.supported_or_better()],
            destructive=[h.hypothesis_id for h in self.destructive_hypotheses()],
            hypotheses=[h.to_summary_dict() for h in self.records.values()],
        )


def _merge_evidence(existing: list, new: list) -> list:
    seen = {(e.source_kind, e.element_id, e.page_url) for e in existing}
    merged = list(existing)
    for e in new:
        key = (e.source_kind, e.element_id, e.page_url)
        if key not in seen:
            seen.add(key)
            merged.append(e)
    return merged
