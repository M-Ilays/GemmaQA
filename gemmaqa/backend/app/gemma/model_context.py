"""Normalized model context — the single aggregation boundary.

The audit (docs/MODEL_CONTEXT_AUDIT.md, section G) classified GemmaQA as
*centralized prompt construction + scattered context aggregation*: prompt text
was single-sourced in `app/gemma/prompts.py`, but each of the eight call sites
decided independently what its model would see. This module closes that gap
without moving prompt rendering anywhere.

Design rules, all of them consequences of the audit:

* **Aggregate, never recompute.** Every field here is read from an engine that
  already produced it — `RunMemory`'s existing query API and `memory_snapshot()`.
  No business logic is duplicated. If a value is not already computed somewhere,
  it does not belong here.
* **Provenance travels with the value.** Each section records which component
  produced it, which object it came from, its confidence where the producer
  supplies one, and the observation version. The audit's section I listed
  provenance tracking as entirely missing; a value with no traceable origin
  cannot be audited after the fact.
* **Aggregation is not selection.** This module gathers a superset;
  `GraphContextProjector.project_model_context()` decides what actually fits and
  what matters. Keeping the two apart is what stops "add a field" from silently
  becoming "grow every prompt".
* **Degrade, never raise.** Engines are optional and may be absent, half-built,
  or mid-pass. Every read is guarded: a missing engine contributes nothing rather
  than failing a run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.utils.logging import get_logger
from app.utils.sanitization import sanitize_dict, sanitize_text

logger = get_logger("gemma.model_context")

# Bounded collection sizes. These are the aggregation-stage caps: generous
# enough that the projector has real choices to rank, small enough that a
# pathological run cannot build a multi-megabyte aggregate in memory.
MAX_ENGINE_ITEMS = 25
MAX_GAP_ITEMS = 15
MAX_CONTRADICTIONS = 10
MAX_RECENT_ACTIONS = 25
MAX_EVIDENCE_ITEMS = 30


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextProvenance:
    """Where one context section came from."""

    producer: str
    source_ref: str = ""
    confidence: float | None = None
    observation_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"producer": self.producer}
        if self.source_ref:
            data["source_ref"] = self.source_ref
        if self.confidence is not None:
            data["confidence"] = round(float(self.confidence), 3)
        if self.observation_version:
            data["observation_version"] = self.observation_version
        return data


# ---------------------------------------------------------------------------
# Evidence registry
# ---------------------------------------------------------------------------


def stable_evidence_id(kind: str, payload: str) -> str:
    """Deterministic, content-derived evidence id.

    Deterministic on purpose: the same observation must produce the same id
    across passes and across processes, so tests are reproducible and a stale
    reference from an earlier observation is detectable rather than accidentally
    valid. Never `new_id()` here — a random id would make every re-observation
    look like fresh evidence.
    """
    digest = hashlib.sha256(f"{kind}|{payload}".encode("utf-8", "replace")).hexdigest()[:12]
    return f"ev_{kind}_{digest}"


@dataclass
class EvidenceItem:
    """One retrievable, citable piece of evidence offered to the model."""

    evidence_id: str
    kind: str
    summary: str
    reference: str = ""
    provenance: ContextProvenance | None = None
    observation_version: str | None = None

    def to_payload(self) -> dict[str, Any]:
        """Model-facing form. Deliberately excludes `reference` (a filesystem
        path or DOM handle is useless to the model and is a disclosure risk)."""
        data: dict[str, Any] = {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "summary": sanitize_text(self.summary)[:400],
        }
        if self.observation_version:
            data["observation_version"] = self.observation_version
        if self.provenance is not None:
            data["produced_by"] = self.provenance.producer
        return data


class EvidenceRegistry:
    """The authoritative set of evidence ids for ONE model call.

    Two jobs, and they are the same job seen from both directions: tell the model
    which references exist, and reject any reference it returns that does not.
    The audit (J-6) found model-invented `evidence_ids` being written to defects
    and exported to CSV; a per-call registry is what makes rejection possible at
    all, because "valid" is only meaningful relative to what was offered.
    """

    def __init__(self) -> None:
        self._items: dict[str, EvidenceItem] = {}
        self.rejected_ids: list[str] = []

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, evidence_id: object) -> bool:
        return str(evidence_id) in self._items

    @property
    def ids(self) -> list[str]:
        return list(self._items)

    @property
    def items(self) -> list[EvidenceItem]:
        return list(self._items.values())

    def register(
        self,
        *,
        kind: str,
        summary: str,
        reference: str = "",
        provenance: ContextProvenance | None = None,
        observation_version: str | None = None,
        evidence_id: str | None = None,
    ) -> EvidenceItem:
        eid = evidence_id or stable_evidence_id(kind, f"{summary}|{reference}")
        existing = self._items.get(eid)
        if existing is not None:
            return existing
        item = EvidenceItem(
            evidence_id=eid,
            kind=kind,
            summary=summary,
            reference=reference,
            provenance=provenance,
            observation_version=observation_version,
        )
        self._items[eid] = item
        return item

    def get(self, evidence_id: str) -> EvidenceItem | None:
        return self._items.get(str(evidence_id))

    def validate(self, returned: Any) -> tuple[list[str], list[str]]:
        """Split model-returned ids into `(accepted, rejected)`.

        Accepted ids are deduplicated with order preserved. Anything not offered
        in this call is rejected — including an id that was valid in an earlier
        observation, because citing stale evidence for a current claim is exactly
        the failure mode this guards.
        """
        accepted: list[str] = []
        rejected: list[str] = []
        seen: set[str] = set()
        if not isinstance(returned, (list, tuple, set)):
            return accepted, rejected
        for raw in returned:
            eid = str(raw).strip()
            if not eid or eid in seen:
                continue
            seen.add(eid)
            if eid in self._items:
                accepted.append(eid)
            else:
                rejected.append(eid)
        if rejected:
            self.rejected_ids.extend(rejected)
        return accepted, rejected

    def to_payload(self, *, limit: int = MAX_EVIDENCE_ITEMS) -> list[dict[str, Any]]:
        return [item.to_payload() for item in list(self._items.values())[:limit]]


# ---------------------------------------------------------------------------
# The aggregate
# ---------------------------------------------------------------------------


@dataclass
class ModelContext:
    """Everything that *could* inform one model call, normalized and attributed.

    This is a superset, not a prompt. `GraphContextProjector.project_model_context`
    turns it into the bounded, ranked, deduplicated structure a prompt renders.
    """

    # Identity and mode
    run_id: str = ""
    mode: str = "exploration"
    provider_name: str = ""

    # Task
    testing_objective: str = ""
    testing_objective_provided: bool = False
    active_goal: dict[str, Any] | None = None
    active_scenario: dict[str, Any] | None = None
    current_step: dict[str, Any] | None = None
    qa_strategy: dict[str, Any] | None = None
    investigation_state: dict[str, Any] | None = None

    # Page and readiness
    page: dict[str, Any] = field(default_factory=dict)
    page_source: str = "legacy_fallback"
    readiness: dict[str, Any] | None = None

    # History
    recent_actions: list[dict[str, Any]] = field(default_factory=list)
    visited_states: list[str] = field(default_factory=list)
    unexplored: list[str] = field(default_factory=list)
    blocked_reasons: list[dict[str, Any]] = field(default_factory=list)

    # Engine intelligence — actual findings, not counts (audit J-8)
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    coverage_gaps: list[dict[str, Any]] = field(default_factory=list)
    workflow_hypotheses: list[dict[str, Any]] = field(default_factory=list)
    engine_diagnostics: dict[str, Any] = field(default_factory=dict)
    # Flat run state from `memory_snapshot()` — auth status, counts, and
    # credential CAPABILITY flags. Never a secret; see the note in
    # `build_model_context`.
    application_memory: dict[str, Any] = field(default_factory=dict)
    graph_focus_node_ids: list[str] = field(default_factory=list)

    # Constraints
    constraints: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)

    # Evidence
    evidence: EvidenceRegistry = field(default_factory=EvidenceRegistry)
    technical_evidence: dict[str, Any] = field(default_factory=dict)
    temporary_records: list[dict[str, Any]] = field(default_factory=list)

    # Live objects the projector may query (never serialized into a prompt)
    knowledge_graph: Any = field(default=None, repr=False)
    run_memory: Any = field(default=None, repr=False)

    provenance: dict[str, ContextProvenance] = field(default_factory=dict)

    def attribute(self, section: str, provenance: ContextProvenance) -> None:
        self.provenance[section] = provenance

    def provenance_payload(self) -> dict[str, Any]:
        return {key: prov.to_dict() for key, prov in sorted(self.provenance.items())}


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _safe(call, default):  # type: ignore[no-untyped-def]
    """Engine reads are always optional — a half-built engine must not fail a run."""
    try:
        value = call()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Context aggregation read failed (%s); skipping", type(exc).__name__)
        return default
    return default if value is None else value


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for attr in ("to_dict", "model_dump"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                result = fn()
                if isinstance(result, dict):
                    return result
            except Exception:  # pragma: no cover - defensive
                pass
    return {}


def _gap_detail(gap: Any) -> dict[str, Any]:
    """Exact gap detail, not a count (audit J-8 / Phase 5 requirement)."""
    data = _as_dict(gap)
    if data:
        keep = ("gap_id", "gap_type", "description", "exploration_value", "risk", "status", "node_ids")
        detail = {k: data[k] for k in keep if k in data and data[k] not in (None, "", [])}
        if detail:
            return detail
    return {
        "gap_id": str(getattr(gap, "gap_id", "") or ""),
        "gap_type": str(getattr(gap, "gap_type", "") or "unknown"),
        "description": str(getattr(gap, "description", "") or "")[:240],
    }


def _named(record: Any) -> dict[str, Any]:
    data = _as_dict(record)
    name = data.get("canonical_name") or getattr(record, "canonical_name", None)
    out: dict[str, Any] = {"name": str(name or "")}
    for key in ("status", "confidence", "workflow_id", "entity_id", "actor_id"):
        value = data.get(key)
        if value not in (None, "", []):
            out[key] = round(value, 3) if isinstance(value, float) else value
    return out


def build_model_context(
    request: Any,
    *,
    provider_name: str = "",
) -> ModelContext:
    """Aggregate one `ActionGenerationRequest` into a normalized `ModelContext`.

    `request.run_memory`, when supplied, is the live `RunMemory` — read through
    its existing query API so engine business logic stays where it belongs. When
    absent (older callers, unit tests), the pre-existing `request.memory`
    snapshot dict is used and the aggregate is simply thinner.
    """
    memory_snapshot: dict[str, Any] = dict(getattr(request, "memory", None) or {})
    run_memory = getattr(request, "run_memory", None)

    ctx = ModelContext(
        run_id=str(getattr(request, "run_id", "") or ""),
        mode=str(getattr(request, "mode", "") or "exploration"),
        provider_name=provider_name,
        testing_objective=str(getattr(request, "testing_objective", "") or ""),
        testing_objective_provided=bool(getattr(request, "testing_objective_provided", False)),
        run_memory=run_memory,
    )

    # -- constraints and budgets (operator policy, never model-negotiable) ----
    ctx.constraints = {
        "safe_mode": bool(getattr(request, "safe_mode", True)),
        "authorized_domain": str(getattr(request, "authorized_domain", "") or ""),
        "allowed_actions": list(getattr(request, "allowed_actions", None) or []),
    }
    ctx.attribute("constraints", ContextProvenance(producer="SafetyPolicy/RunConfiguration"))

    ctx.budgets = {
        "remaining_action_budget": int(getattr(request, "remaining_action_budget", 0) or 0),
        "remaining_page_budget": int(getattr(request, "remaining_page_budget", 0) or 0),
    }
    ctx.attribute("budgets", ContextProvenance(producer="RunMemory"))

    # -- history --------------------------------------------------------------
    recent = list(getattr(request, "recent_actions", None) or [])[-MAX_RECENT_ACTIONS:]
    ctx.recent_actions = sanitize_dict({"items": recent}).get("items") or []
    ctx.attribute(
        "recent_actions",
        ContextProvenance(producer="ActionExecutor", source_ref="RunMemory.previous_actions_payload"),
    )
    ctx.visited_states = [str(s) for s in (getattr(request, "visited_states", None) or [])][-20:]
    ctx.unexplored = [str(u) for u in (getattr(request, "unexplored", None) or [])][:20]

    blocked = memory_snapshot.get("blocked_hrefs")
    if isinstance(blocked, (list, tuple, set)):
        ctx.blocked_reasons = [{"href": str(h), "reason": "blocked_by_policy"} for h in list(blocked)[:10]]

    # -- task context ---------------------------------------------------------
    active_goal = memory_snapshot.get("active_goal")
    if isinstance(active_goal, dict) and active_goal:
        ctx.active_goal = active_goal
        ctx.attribute(
            "active_goal",
            ContextProvenance(
                producer="GoalGenerationEngine/ExplorationGoal",
                source_ref=str(active_goal.get("goal_id") or ""),
                confidence=active_goal.get("confidence") if isinstance(active_goal.get("confidence"), float) else None,
            ),
        )

    if run_memory is not None:
        scenario = _as_dict(_safe(run_memory.scenario_summary, None))
        if scenario:
            ctx.active_scenario = scenario
            ctx.attribute("active_scenario", ContextProvenance(producer="ScenarioPlanningEngine"))

        strategy = _as_dict(_safe(run_memory.strategy_summary, None))
        if strategy:
            ctx.qa_strategy = strategy
            ctx.attribute("qa_strategy", ContextProvenance(producer="QAStrategyEngine"))

        investigation = _as_dict(_safe(run_memory.current_investigation, None))
        if investigation:
            ctx.investigation_state = {
                k: investigation[k]
                for k in ("investigation_id", "state", "scenario_id", "current_step_index", "outcome")
                if k in investigation
            }
            ctx.current_step = _as_dict(investigation.get("current_step")) or None
            ctx.attribute("investigation_state", ContextProvenance(producer="AutonomousInvestigationEngine"))

        # -- readiness and observation confidence -----------------------------
        latest = _safe(run_memory.latest_understanding, None)
        if latest is not None:
            state = getattr(latest, "state", None)
            confidence = getattr(latest, "observation_confidence", None)
            readiness = getattr(latest, "readiness", None)
            ctx.readiness = {
                "application_state": str(getattr(state, "primary_state", "") or "unknown"),
                "readiness_score": round(float(getattr(readiness, "readiness_score", 0.0) or 0.0), 3),
                "observation_confidence": round(float(getattr(confidence, "value", 0.0) or 0.0), 3),
                "decision": str(getattr(latest, "decision", "") or ""),
                "blocking_signals": list(getattr(readiness, "blocking_signal_kinds", None) or [])[:8],
            }
            ctx.attribute(
                "readiness",
                ContextProvenance(
                    producer="AdaptiveApplicationUnderstandingEngine",
                    confidence=float(getattr(confidence, "value", 0.0) or 0.0),
                    observation_version=str(getattr(latest, "assessment_id", "") or "") or None,
                ),
            )

        # -- contradictions (unresolved beliefs the model should not ignore) --
        raw_contradictions = list(_safe(run_memory.understanding_contradictions, []))[:MAX_CONTRADICTIONS]
        for item in raw_contradictions:
            data = _as_dict(item)
            ctx.contradictions.append(
                {
                    "contradiction_type": str(data.get("contradiction_type") or getattr(item, "contradiction_type", "") or "unknown"),
                    "description": sanitize_text(str(data.get("description") or getattr(item, "description", "") or ""))[:240],
                }
            )
        if ctx.contradictions:
            ctx.attribute(
                "contradictions", ContextProvenance(producer="AdaptiveApplicationUnderstandingEngine")
            )

        # -- coverage gaps, with exact detail ---------------------------------
        gaps: list[dict[str, Any]] = []
        for source_name, reader in (
            ("KnowledgeGraph", getattr(run_memory, "graph_gaps", None)),
            ("WorkflowDiscoveryEngine", run_memory.workflow_gaps),
            ("BusinessDependencyEngine", run_memory.dependency_gaps),
            ("ScenarioPlanningEngine", run_memory.scenario_gaps),
        ):
            if reader is None:
                continue
            for gap in list(_safe(reader, []))[:MAX_GAP_ITEMS]:
                detail = _gap_detail(gap)
                detail["discovered_by"] = source_name
                gaps.append(detail)
        graph = getattr(run_memory, "knowledge_graph", None)
        if graph is not None:
            for gap in list(_safe(graph.gaps, []))[:MAX_GAP_ITEMS]:
                detail = _gap_detail(gap)
                detail["discovered_by"] = "KnowledgeGraph"
                gaps.append(detail)
        ctx.coverage_gaps = gaps[: MAX_GAP_ITEMS * 2]
        if ctx.coverage_gaps:
            ctx.attribute("coverage_gaps", ContextProvenance(producer="gap analyzers (multiple engines)"))

        # -- workflow hypotheses ---------------------------------------------
        hypotheses = [
            _named(w) for w in list(_safe(run_memory.incomplete_workflows, []))[:MAX_ENGINE_ITEMS]
        ] + [
            _named(w) for w in list(_safe(run_memory.low_confidence_workflows, []))[:MAX_ENGINE_ITEMS]
        ]
        ctx.workflow_hypotheses = [h for h in hypotheses if h.get("name")][:MAX_ENGINE_ITEMS]
        if ctx.workflow_hypotheses:
            ctx.attribute("workflow_hypotheses", ContextProvenance(producer="WorkflowDiscoveryEngine"))

        ctx.knowledge_graph = graph

    # -- graph focus: what the projector should slice AROUND --------------
    # The active goal names its own subject; failing that, the highest-value
    # gaps do. Without a focus there is nothing to be relevant TO, and the
    # projector correctly falls back to a size summary rather than a slice.
    focus: list[str] = []
    for source in (ctx.active_goal or {}, ctx.active_scenario or {}):
        for key in ("focus_node_id", "node_id", "entity_id", "actor_id", "workflow_id"):
            value = source.get(key)
            if value:
                focus.append(str(value))
        for key in ("node_ids", "focus_node_ids"):
            values = source.get(key)
            if isinstance(values, (list, tuple)):
                focus.extend(str(v) for v in values if v)
    for gap in ctx.coverage_gaps:
        for value in gap.get("node_ids") or []:
            focus.append(str(value))
    ctx.graph_focus_node_ids = list(dict.fromkeys(focus))[:5]

    # -- engine diagnostics: reuse the existing statistics blocks verbatim ----
    diagnostics = {
        key: value
        for key, value in memory_snapshot.items()
        if key
        in (
            "knowledge_graph",
            "goal_generation",
            "scenario_planning",
            "qa_strategy",
            "autonomous_investigation",
            "adaptive_understanding",
            "entities",
            "actors",
            "workflows",
            "dependencies",
        )
    }
    ctx.engine_diagnostics = diagnostics
    if diagnostics:
        ctx.attribute(
            "engine_diagnostics",
            ContextProvenance(producer="RunMemory.memory_snapshot", source_ref="engine statistics()"),
        )

    # Everything else from the snapshot stays as flat application memory: auth
    # status, discovery counts, and the credential CAPABILITY flags
    # (`credential_profile_available`, `actor_role`) that let the model reason
    # about whether authentication is possible without ever seeing a secret.
    # This is genuinely planning-relevant, so it sits at current-state priority
    # rather than with the reducible diagnostics.
    ctx.application_memory = sanitize_dict(
        {
            key: value
            for key, value in memory_snapshot.items()
            if key not in diagnostics and key != "active_goal"
        }
    )
    if ctx.application_memory:
        ctx.attribute(
            "application_memory",
            ContextProvenance(producer="RunMemory.memory_snapshot", source_ref="run state + credential flags"),
        )

    ctx.temporary_records = []
    ctx.technical_evidence = {}

    ctx.attribute("page", ContextProvenance(producer="PageObserver/PerceptionEngine"))
    return ctx
