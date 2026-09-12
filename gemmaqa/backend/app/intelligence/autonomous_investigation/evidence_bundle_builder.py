"""Assembles an `EvidenceBundle` from an investigation's own execution
trace and accumulated observations -- references only (evidence ids the
existing `EvidenceCollector`/`ActionExecutor` already produced), never a
copy of a raw evidence payload.
"""

from __future__ import annotations

from app.intelligence.autonomous_investigation.schemas import EvidenceBundle, ExecutionTrace


def build_evidence_bundle(
    investigation_id: str,
    scenario_id: str,
    execution_trace: list[ExecutionTrace],
    *,
    screenshot_paths: list[str] | None = None,
    urls_visited: list[str] | None = None,
    console_errors: list[str] | None = None,
    network_errors: list[str] | None = None,
) -> EvidenceBundle:
    evidence_ids: list[str] = []
    total_duration = 0
    for trace in execution_trace:
        evidence_ids.extend(trace.evidence_ids)
        total_duration += trace.duration_ms
    return EvidenceBundle(
        bundle_id=f"evidence-bundle:{investigation_id}",
        investigation_id=investigation_id,
        scenario_id=scenario_id,
        evidence_ids=sorted(dict.fromkeys(evidence_ids)),
        screenshot_paths=sorted(dict.fromkeys(screenshot_paths or [])),
        urls_visited=sorted(dict.fromkeys(urls_visited or [])),
        console_errors=list(dict.fromkeys(console_errors or [])),
        network_errors=list(dict.fromkeys(network_errors or [])),
        total_duration_ms=total_duration,
    )
