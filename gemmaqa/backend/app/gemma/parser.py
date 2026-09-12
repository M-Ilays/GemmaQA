"""Parse and validate Gemma JSON responses."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from pydantic import ValidationError

from app.schemas import (
    ActionCategory,
    ActionType,
    BrowserAction,
    BugAnalysisResult,
    BugClassification,
    Defect,
    DefectSeverity,
    PageClassification,
    RiskLevel,
)
from app.utils.ids import new_id
from app.utils.logging import get_logger

logger = get_logger("gemma.parser")

ELEMENT_ACTIONS = {
    ActionType.CLICK,
    ActionType.FILL,
    ActionType.SELECT,
    ActionType.CHECK,
    ActionType.UNCHECK,
    ActionType.PRESS,
    ActionType.HOVER,
    ActionType.OPEN_TAB,
    ActionType.INSPECT_FORM,
    ActionType.INSPECT_TABLE,
}

REQUIRED_ACTION_FIELDS = ("action", "reason", "expected_result", "risk", "category")

CATEGORY_ALIASES = {
    "inspection": ActionCategory.FORM_INSPECTION,
    "form_interaction": ActionCategory.FORM_INSPECTION,
    "navigation": ActionCategory.NAVIGATION_TEST,
    "verification": ActionCategory.NEGATIVE_TEST,
    "evidence": ActionCategory.EVIDENCE_CAPTURE,
    "control": ActionCategory.COMPLETION,
}

SEVERITY_MAP = {
    "critical": DefectSeverity.CRITICAL,
    "high": DefectSeverity.MAJOR,
    "medium": DefectSeverity.MAJOR,
    "low": DefectSeverity.MINOR,
    "blocker": DefectSeverity.BLOCKER,
    "major": DefectSeverity.MAJOR,
    "minor": DefectSeverity.MINOR,
    "trivial": DefectSeverity.TRIVIAL,
}


class ActionParseError(ValueError):
    """Raised when a model action response cannot be accepted."""


def extract_json(text: str) -> dict[str, Any]:
    """
    Extract a JSON object from model output.

    Rejects empty output. Strips markdown fences when present, then parses
    the first JSON object. Raises ActionParseError on failure.
    """
    if text is None:
        raise ActionParseError("Empty model response")
    text = text.strip()
    if not text:
        raise ActionParseError("Empty model response")

    # Explicitly note/strip fences rather than trusting them blindly
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
        logger.debug("Stripped markdown fences from model output")

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        # Arrays for scenario lists
        a_start = text.find("[")
        a_end = text.rfind("]")
        if a_start != -1 and a_end > a_start:
            arr = json.loads(text[a_start : a_end + 1])
            if isinstance(arr, list):
                return {"items": arr}
        raise ActionParseError(f"No JSON object found in response: {text[:200]}")

    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ActionParseError(f"Invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ActionParseError("JSON root must be an object")
    return data


def validate_evidence_ids(
    returned: Any,
    *,
    evidence_registry: Any = None,
    context: str = "model response",
) -> list[str]:
    """Keep only evidence ids the model was actually offered.

    docs/MODEL_CONTEXT_AUDIT.md finding J-6: model-returned `evidence_ids` were
    accepted unvalidated, written onto `Defect`, and exported to CSV — while the
    correct drop-and-log pattern already existed one file over in
    `parse_visual_analysis`. This is that pattern, generalized.

    With no registry supplied the ids pass through unchanged, so older callers
    behave exactly as before rather than silently losing data. A registry is
    what makes "valid" meaningful; without one there is nothing to check against.
    """
    if not isinstance(returned, (list, tuple, set)):
        return []
    if evidence_registry is None:
        return [str(item) for item in returned if str(item).strip()]

    accepted, rejected = evidence_registry.validate(returned)
    if rejected:
        logger.warning(
            "Dropped %d evidence id(s) not present in the registry for this call (%s): %s",
            len(rejected),
            context,
            ", ".join(sorted(rejected)[:10]),
        )
    return accepted


def parse_and_validate_action(
    raw: str | dict[str, Any],
    *,
    known_element_ids: Iterable[str] | None = None,
    allowed_actions: Iterable[str] | None = None,
    reject_high_risk: bool = True,
    evidence_registry: Any = None,
) -> BrowserAction:
    """
    Parse model output into a BrowserAction with strict validation.

    Rejects unknown actions, missing fields, unknown element IDs, and high risk.
    Any `evidence_ids` in the response metadata are validated against
    `evidence_registry` when one is supplied.
    """
    data = extract_json(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict):
        raise ActionParseError("Action payload must be an object")

    for field in REQUIRED_ACTION_FIELDS:
        if field not in data or data[field] is None or str(data[field]).strip() == "":
            raise ActionParseError(f"Missing required field: {field}")

    action_name = str(data.get("action", "")).strip().lower()
    if not action_name:
        raise ActionParseError("Missing required field: action")

    allowed = {a.lower() for a in (allowed_actions or [a.value for a in ActionType])}
    if action_name not in allowed:
        raise ActionParseError(f"Unsupported action type: {action_name}")

    try:
        action_type = ActionType(action_name)
    except ValueError as exc:
        raise ActionParseError(f"Unsupported action type: {action_name}") from exc

    risk_raw = str(data.get("risk", "low")).strip().lower()
    try:
        risk = RiskLevel(risk_raw)
    except ValueError as exc:
        raise ActionParseError(f"Invalid risk: {risk_raw}") from exc

    if reject_high_risk and risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
        raise ActionParseError(f"High-risk actions are rejected: {risk.value}")

    category_raw = str(data.get("category", "exploration")).strip().lower()
    if category_raw in CATEGORY_ALIASES:
        category = CATEGORY_ALIASES[category_raw]
    else:
        try:
            category = ActionCategory(category_raw)
        except ValueError as exc:
            raise ActionParseError(f"Invalid category: {category_raw}") from exc

    element_id = data.get("element_id")
    if element_id is not None:
        element_id = str(element_id).strip() or None

    if action_type in ELEMENT_ACTIONS:
        if not element_id:
            raise ActionParseError(f"Action {action_type.value} requires element_id")
        known = set(known_element_ids or [])
        if known and element_id not in known:
            raise ActionParseError(f"Unknown element_id: {element_id}")

    if action_type == ActionType.OPEN_URL:
        url = data.get("url") or data.get("value")
        if not url:
            raise ActionParseError("open_url requires url or value")

    value = data.get("value")
    if value is not None:
        value = str(value)

    metadata = dict(data.get("metadata") or {})
    # An action may cite the evidence that motivated it. Those citations are
    # validated exactly like element_ids: a reference the model was not offered
    # is dropped rather than carried forward into the run record.
    cited = data.get("evidence_ids") or metadata.get("evidence_ids")
    if cited is not None:
        accepted = validate_evidence_ids(cited, evidence_registry=evidence_registry, context="action selection")
        if accepted:
            metadata["evidence_ids"] = accepted
        else:
            metadata.pop("evidence_ids", None)

    try:
        return BrowserAction(
            action=action_type,
            element_id=element_id,
            value=value,
            reason=str(data.get("reason", "")).strip(),
            expected_result=str(data.get("expected_result", "")).strip(),
            risk=risk,
            category=category,
            url=data.get("url"),
            key=data.get("key"),
            wait_ms=data.get("wait_ms"),
            metadata=metadata,
        )
    except ValidationError as exc:
        raise ActionParseError(f"Pydantic validation failed: {exc}") from exc


def parse_action(raw: str | dict[str, Any]) -> BrowserAction:
    """Backward-compatible lenient parse (no element-id registry checks)."""
    return parse_and_validate_action(raw, known_element_ids=None, reject_high_risk=False)


def parse_visual_analysis(
    raw: str | dict[str, Any],
    *,
    known_element_ids: Iterable[str],
) -> list[Any]:
    """Parse the visual model's structured JSON into validated `VisualEvidence`
    records. Anything that names an element_id outside `known_element_ids` is
    dropped rather than trusted — the visual layer must never invent page
    content or elements GemmaQA didn't already observe. Any parse/validation
    failure degrades to an empty list (never raises) — visual evidence is
    always additive, never required for the rest of perception to proceed."""
    from app.perception.models import VISUAL_ELEMENT_SEMANTIC_TYPES, VisualEvidence

    try:
        data = extract_json(raw) if isinstance(raw, str) else raw
    except ActionParseError as exc:
        logger.warning("Visual analysis response unparseable (%s)", exc)
        return []
    if not isinstance(data, dict):
        return []

    items = data.get("observations")
    if items is None:
        items = data.get("items")
    if not isinstance(items, list):
        return []

    known = {str(eid) for eid in (known_element_ids or [])}
    results: list[VisualEvidence] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        element_id = str(item.get("element_id") or "").strip()
        if not element_id or (known and element_id not in known):
            logger.warning("Dropping visual observation for unknown/invented element_id: %r", element_id)
            continue

        semantic_type = str(item.get("visual_semantic_type") or "unknown").strip().lower()
        if semantic_type not in VISUAL_ELEMENT_SEMANTIC_TYPES:
            semantic_type = "unknown"

        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        try:
            results.append(
                VisualEvidence(
                    element_id=element_id,
                    visual_semantic_type=semantic_type,
                    description=str(item.get("description") or "")[:500],
                    confidence=confidence,
                    evidence=[str(e) for e in (item.get("evidence") or [])][:10],
                    further_interaction_recommended=bool(item.get("further_interaction_recommended", False)),
                )
            )
        except ValidationError as exc:
            logger.warning("Dropping invalid visual observation for %s: %s", element_id, exc)
    return results


def parse_classification(raw: str | dict[str, Any]) -> PageClassification:
    data = extract_json(raw) if isinstance(raw, str) else raw
    return PageClassification(
        page_type=str(data.get("page_type", "unknown")),
        confidence=float(data.get("confidence", 0.0)),
        purpose=data.get("purpose"),
        module_guess=data.get("module_guess"),
        tags=list(data.get("tags") or []),
    )


def parse_bug_analysis(
    raw: str | dict[str, Any],
    *,
    evidence_registry: Any = None,
) -> BugAnalysisResult:
    data = extract_json(raw) if isinstance(raw, str) else raw

    def _from_is_bug() -> BugClassification:
        """The legacy `is_bug` boolean, for payloads with no `classification`."""
        if data.get("is_bug") is False:
            # An explicit "this is not a bug" is exactly `no_defect`. Mapped to
            # OBSERVATION before, which let the legacy format produce findings
            # that denied their own existence.
            return BugClassification.NO_DEFECT
        if data.get("is_bug"):
            return BugClassification.SUSPECTED_BUG
        return BugClassification.OBSERVATION

    # `classification` must be ABSENT, not merely falsy, to fall back — reading a
    # default of "observation" first made the legacy branch unreachable, because
    # "observation" parses successfully and never raises.
    if "classification" in data:
        try:
            classification = BugClassification(str(data["classification"]).lower())
        except ValueError:
            classification = _from_is_bug()
    else:
        classification = _from_is_bug()

    severity = str(data.get("severity", "medium")).lower()
    if severity not in {"critical", "high", "medium", "low"}:
        severity = "medium"
    priority = str(data.get("priority", "medium")).lower()
    if priority not in {"urgent", "high", "medium", "low"}:
        priority = "medium"

    return BugAnalysisResult(
        classification=classification,
        title=str(data.get("title", "")),
        module=str(data.get("module", "")),
        severity=severity,  # type: ignore[arg-type]
        priority=priority,  # type: ignore[arg-type]
        preconditions=list(data.get("preconditions") or []),
        steps=list(data.get("steps") or data.get("steps_to_reproduce") or []),
        expected_result=str(data.get("expected_result") or data.get("expected") or ""),
        actual_result=str(data.get("actual_result") or data.get("actual") or ""),
        business_impact=str(data.get("business_impact", "")),
        possible_root_cause=str(data.get("possible_root_cause", "")),
        confidence=float(data.get("confidence", 0.0)),
        # Audit J-6: never persist an evidence id the model was not offered.
        evidence_ids=validate_evidence_ids(
            data.get("evidence_ids") or [],
            evidence_registry=evidence_registry,
            context="bug analysis",
        ),
    )


def bug_analysis_to_defect(analysis: BugAnalysisResult, run_id: str, page_url: str | None = None) -> Defect | None:
    """Convert BugAnalysisResult to Defect, or None when there is nothing to report.

    Three independent gates, because relying on any single one has already failed
    in production. The confidence gate below was the original filter for "nothing
    is wrong"; it does not work, because a real model reporting a clean page is
    HIGHLY confident, and high confidence satisfied the gate. Nova Pro produced 27
    such non-findings in a 32-action run.
    """
    # 1. The model said so explicitly. This is the intended path.
    if analysis.classification == BugClassification.NO_DEFECT:
        return None
    if analysis.classification == BugClassification.OBSERVATION and not analysis.title:
        return None
    # 2. An observation with nothing observed. A defect — or a noteworthy
    #    non-defect — has to state what was actually seen. This catches a model
    #    that ignores `no_defect` but leaves the discrepancy fields empty.
    if analysis.classification == BugClassification.OBSERVATION and not analysis.actual_result.strip():
        return None
    # 3. The original confidence gate, kept for a model that reports a weak
    #    hunch as an observation. No longer the only thing standing between a
    #    clean page and a bug report.
    if analysis.classification == BugClassification.OBSERVATION and analysis.confidence < 0.35:
        return None
    # 4. The structural gate, which needs no cooperation from the model: an
    #    OBSERVATION must cite something it was actually given. A page that
    #    behaved correctly offers nothing to cite, whereas a real observation is
    #    ABOUT some console error, network failure, alert or dialog — all of
    #    which are registered evidence (see GemmaProvider.analyze_potential_bug).
    #
    #    Gates 1-3 all depend on the model choosing the right classification or
    #    leaving a field blank. This one does not, which is why it exists: a
    #    verbatim replay of the Nova payload that produced "No defects detected
    #    on the login page" passes all three of them.
    #
    #    Deliberately scoped to OBSERVATION, the weakest tier. A confirmed or
    #    suspected bug is never withheld for want of a citation, and the
    #    deterministic detectors never emit OBSERVATION at all.
    if analysis.classification == BugClassification.OBSERVATION and not analysis.evidence_ids:
        return None

    severity = SEVERITY_MAP.get(analysis.severity, DefectSeverity.MAJOR)
    description = analysis.actual_result or analysis.business_impact or analysis.title
    if analysis.possible_root_cause:
        description = (
            f"{description}\n\nPossible root cause (hypothesis only): {analysis.possible_root_cause}"
        )

    return Defect(
        bug_id=new_id(),
        run_id=run_id,
        title=analysis.title or "Untitled defect",
        description=description,
        severity=severity,
        steps_to_reproduce=list(analysis.steps),
        expected=analysis.expected_result,
        actual=analysis.actual_result,
        page_url=page_url,
        evidence_ids=list(analysis.evidence_ids),
        tags=[analysis.classification.value, analysis.module] if analysis.module else [analysis.classification.value],
    )


def parse_defect(raw: str | dict[str, Any], run_id: str) -> Defect | None:
    """Parse either legacy or new bug analysis formats into a Defect."""
    data = extract_json(raw) if isinstance(raw, str) else raw
    if data.get("is_bug") is False and "classification" not in data:
        return None
    analysis = parse_bug_analysis(data)
    return bug_analysis_to_defect(analysis, run_id, page_url=data.get("page_url"))


def parse_scenarios(raw: str | dict[str, Any]) -> list[dict[str, Any]]:
    data = extract_json(raw) if isinstance(raw, str) else raw
    if isinstance(data.get("scenarios"), list):
        return list(data["scenarios"])
    if isinstance(data.get("items"), list):
        return list(data["items"])
    if isinstance(data.get("tests"), list):
        return list(data["tests"])
    return [data]


def _el_attr(el: Any, name: str, default: Any = None) -> Any:
    if isinstance(el, dict):
        return el.get(name, default)
    return getattr(el, name, default)


def _normalize_url_key(url: str | None) -> str:
    if not url:
        return ""
    return str(url).split("#", 1)[0].rstrip("/").lower()


LOGOUT_HINTS = ("logout", "log out", "sign out")

# candidate_type -> (ActionType, ActionCategory) for the subset of frontier candidates
# that translate directly into a self-contained action from their serialized dict alone
# (element_id/url/label only — no live AuthenticationStrategy/workflow object required).
# Credential-driven types (authenticate_with_credentials, create_test_account,
# open_registration, continue_active_workflow) are intentionally NOT handled here: the
# real Planner._select_auth_candidate path already tries every frontier candidate,
# including those, before generate_action() is ever reached, so by the time a frontier
# reaches this function those types have already been attempted or found undispatchable.
_SIMPLE_CANDIDATE_ACTIONS: dict[str, tuple[ActionType, ActionCategory]] = {
    "navigation_control": (ActionType.CLICK, ActionCategory.EXPLORATION),
    "safe_test_data_create": (ActionType.CLICK, ActionCategory.EXPLORATION),
    "inspect_form": (ActionType.INSPECT_FORM, ActionCategory.FORM_INSPECTION),
    "inspect_table": (ActionType.INSPECT_TABLE, ActionCategory.FORM_INSPECTION),
    "open_url": (ActionType.OPEN_URL, ActionCategory.NAVIGATION_TEST),
}


def _dispatch_frontier_candidate(cand: dict[str, Any], reason: str) -> BrowserAction | None:
    """Convert one serialized FrontierCandidate dict into a BrowserAction, or None if
    this candidate's type isn't one of the self-contained ones this fallback can act on."""
    candidate_type = str(cand.get("candidate_type") or "")
    mapping = _SIMPLE_CANDIDATE_ACTIONS.get(candidate_type)
    if mapping is None:
        return None
    action_type, category = mapping
    label = str(cand.get("actual_label") or cand.get("reason") or candidate_type).strip()
    if action_type == ActionType.OPEN_URL:
        url = cand.get("url") or cand.get("target_url")
        if not url:
            return None
        return BrowserAction(
            action=ActionType.OPEN_URL,
            url=str(url),
            reason=f"{reason} {cand.get('reason') or 'Visiting frontier candidate URL.'}",
            expected_result="New application page loads.",
            risk=RiskLevel.LOW,
            category=category,
            metadata={"action_label": label, "value_category": str(url), "candidate_id": cand.get("candidate_id")},
        )
    element_id = cand.get("element_id")
    if not element_id:
        return None
    return BrowserAction(
        action=action_type,
        element_id=str(element_id),
        reason=f"{reason} {cand.get('reason') or 'Acting on frontier candidate.'}",
        expected_result="Frontier candidate explored.",
        risk=RiskLevel.LOW,
        category=category,
        metadata={"action_label": label, "candidate_id": cand.get("candidate_id")},
    )


def fallback_safe_action(
    *,
    page_state: Any | None = None,
    recent_actions: list[dict[str, Any]] | None = None,
    remaining_action_budget: int = 1,
    reason: str = "Model failed; using deterministic safe fallback.",
    unexplored_urls: list[str] | None = None,
    visited_urls: list[str] | None = None,
    blocked_hrefs: set[str] | None = None,
    frontier_candidates: list[dict[str, Any]] | None = None,
) -> BrowserAction:
    """Deterministic safe action when the model cannot produce valid JSON.

    When `frontier_candidates` is supplied (the unified FrontierBuilder output, already
    generated and scored elsewhere), this selects ONLY from that list — it must not
    derive a separate, independent exploration strategy while frontier data exists. The
    legacy element-scanning heuristic below only runs for callers that never built a
    frontier at all (frontier_candidates is None/empty), which remains a legitimate
    standalone fallback for code paths outside the Planner-driven flow.
    """
    if frontier_candidates:
        ranked = sorted(
            frontier_candidates,
            key=lambda c: (c.get("priority", 999), str(c.get("candidate_id") or "")),
        )
        for cand in ranked:
            if str(cand.get("status") or "available") in {"exhausted", "blocked"}:
                continue
            action = _dispatch_frontier_candidate(cand, reason)
            if action is not None:
                return action
        return BrowserAction(
            action=ActionType.FINISH,
            reason=(
                f"{reason} No available frontier candidate remained undispatched "
                "(all were exhausted, blocked, or require live auth/workflow state)."
            ),
            expected_result="Run completes safely.",
            risk=RiskLevel.LOW,
            category=ActionCategory.COMPLETION,
            metadata={"stop_reason_code": "all_safe_candidates_exhausted"},
        )

    recent = recent_actions or []
    failed_ids = {
        a.get("element_id")
        for a in recent
        if a.get("success") is False and a.get("element_id")
    }
    # Only treat IDs from the latest few actions on this page as "seen"
    seen_ids = {
        a.get("element_id")
        for a in recent[-12:]
        if a.get("element_id") and a.get("action") in {"click", "open_url"}
    }
    recent_nav_labels = {
        str(a.get("label") or a.get("reason") or "").strip().lower()
        for a in recent[-8:]
        if a.get("action") == "click"
    }
    visited = {
        _normalize_url_key(u)
        for u in (visited_urls or [])
        if u
    }
    for a in recent:
        if a.get("url"):
            visited.add(_normalize_url_key(str(a.get("url"))))
    if page_state is not None:
        current_url = (
            page_state.get("url")
            if isinstance(page_state, dict)
            else getattr(page_state, "url", None)
        )
        if current_url:
            visited.add(_normalize_url_key(str(current_url)))

    elements = []
    if page_state is not None:
        elements = getattr(page_state, "interactive_elements", None) or []
        if isinstance(page_state, dict):
            elements = page_state.get("interactive_elements") or []

    # 1) Uninspected forms before nav ping-pong
    forms = getattr(page_state, "forms", None) if page_state is not None else None
    if page_state is not None and isinstance(page_state, dict):
        forms = page_state.get("forms")
    if forms:
        inspected_recent = {
            a.get("element_id")
            for a in recent
            if a.get("action") == "inspect_form" and a.get("element_id")
        }
        for form in forms:
            form_id = form.form_id if hasattr(form, "form_id") else form.get("form_id")
            if form_id and form_id not in inspected_recent:
                return BrowserAction(
                    action=ActionType.INSPECT_FORM,
                    element_id=form_id,
                    reason=f"{reason} Inspecting form structure.",
                    expected_result="Form fields recorded.",
                    risk=RiskLevel.LOW,
                    category=ActionCategory.FORM_INSPECTION,
                )

    # 2) Unexplored same-origin URLs before repeating Sign up/Cancel
    for url in unexplored_urls or []:
        if not url:
            continue
        if _normalize_url_key(url) in visited:
            continue
        return BrowserAction(
            action=ActionType.OPEN_URL,
            url=url,
            reason=f"{reason} Opening unexplored same-origin URL.",
            expected_result="New application page loads.",
            risk=RiskLevel.LOW,
            category=ActionCategory.NAVIGATION_TEST,
            metadata={"action_label": "Open URL", "value_category": url},
        )

    # 3) Links whose href is not already visited (logout deferred to last resort)
    deferred_logout: BrowserAction | None = None
    known_blocked = blocked_hrefs or set()
    for el in elements:
        el_id = _el_attr(el, "element_id")
        tag = str(_el_attr(el, "tag") or "").lower()
        visible = bool(_el_attr(el, "is_visible", True))
        href = _el_attr(el, "href")
        text = (
            _el_attr(el, "accessible_name")
            or _el_attr(el, "visible_text")
            or _el_attr(el, "text")
        )
        if not visible or not el_id or el_id in failed_ids or el_id in seen_ids:
            continue
        if href and str(href).strip() in known_blocked:
            continue
        if tag == "a" and href and not str(href).startswith("#"):
            href_key = _normalize_url_key(str(href))
            # Relative paths: treat path-only keys; skip if clearly already visited
            if href_key and any(href_key in v or v.endswith(href_key) for v in visited if v):
                continue
            label = str(text or href).strip()
            action = BrowserAction(
                action=ActionType.CLICK,
                element_id=el_id,
                reason=f"{reason} Exploring unvisited link: {label}",
                expected_result="Navigate to a new page or section.",
                risk=RiskLevel.LOW,
                category=ActionCategory.EXPLORATION,
                metadata={"action_label": label},
            )
            if any(h in label.lower() for h in LOGOUT_HINTS) and deferred_logout is None:
                deferred_logout = action
                continue
            return action

    # 4) Navigation-style buttons (once per label recently — avoid Sign up↔Cancel loops)
    nav_button_hints = (
        "cancel",
        "back",
        "home",
        "sign in",
        "login",
        "contact list",
        "contacts",
        "return",
        "signup",
        "sign up",
        "register",
        "add a new contact",
        "add contact",
        "add user",
    )
    submit_like = ("submit", "save", "create", "delete", "pay", "purchase", "confirm")
    for el in elements:
        el_id = _el_attr(el, "element_id")
        tag = str(_el_attr(el, "tag") or "").lower()
        role = str(_el_attr(el, "role") or "").lower()
        visible = bool(_el_attr(el, "is_visible", True))
        text = (
            _el_attr(el, "accessible_name")
            or _el_attr(el, "visible_text")
            or _el_attr(el, "text")
            or _el_attr(el, "name")
            or ""
        )
        input_type = str(
            _el_attr(el, "input_type") or _el_attr(el, "type") or ""
        ).lower()
        category = str(_el_attr(el, "category") or "").lower()
        text_l = str(text).strip().lower()
        if not visible or not el_id or el_id in failed_ids or el_id in seen_ids:
            continue
        # Never click bare form fields or submit controls in safe fallback
        if category in {"input", "textarea", "select"} or tag in {"input", "textarea", "select"}:
            if input_type not in {"button", ""} and tag != "button":
                continue
        if tag not in {"button", "a", "input"} and role != "button":
            continue
        if input_type in {"submit", "password", "text", "email", "tel", "number", "search"}:
            continue
        if any(s in text_l for s in submit_like):
            continue
        if any(h in text_l for h in LOGOUT_HINTS):
            if deferred_logout is None:
                deferred_logout = BrowserAction(
                    action=ActionType.CLICK,
                    element_id=el_id,
                    reason=f"{reason} Logging out after exploration (deferred to last resort).",
                    expected_result="Session ends.",
                    risk=RiskLevel.LOW,
                    category=ActionCategory.NAVIGATION_TEST,
                    metadata={"action_label": str(text).strip() or text_l},
                )
            continue
        if any(h in text_l for h in nav_button_hints):
            # Skip if this nav label was already used recently (anti-oscillation)
            if text_l and any(text_l in lbl for lbl in recent_nav_labels):
                continue
            label = str(text).strip() or text_l
            return BrowserAction(
                action=ActionType.CLICK,
                element_id=el_id,
                reason=f"{reason} Exploring navigation control: {label}",
                expected_result="Navigate to a related application page.",
                risk=RiskLevel.LOW,
                category=ActionCategory.NAVIGATION_TEST,
                metadata={"action_label": label},
            )

    # Logout only once every other safe target has been exhausted.
    if deferred_logout is not None:
        return deferred_logout

    return BrowserAction(
        action=ActionType.FINISH,
        reason=(
            f"{reason} No remaining same-origin navigation controls, uninspected forms, "
            "or unvisited application URLs were available within the safe exploration policy."
        ),
        expected_result="Run completes.",
        risk=RiskLevel.LOW,
        category=ActionCategory.COMPLETION,
        metadata={"stop_reason_code": "exploration_complete"},
    )
