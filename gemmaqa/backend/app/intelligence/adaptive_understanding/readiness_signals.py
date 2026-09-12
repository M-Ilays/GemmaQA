"""Extracts observable readiness signals from ONE observation.

Every signal here answers a question a human tester could answer by looking
at the screen, or reads a W3C-standard accessibility attribute. None of them
knows -- or could know -- which framework rendered the page. That is the
whole point: an application that has served its HTML shell but rendered
nothing yet is observably identical whether the cause is a slow SPA
bootstrap, a blocked script, a pending fetch, or a server that returned an
empty body. GemmaQA does not need to diagnose the cause to notice the
condition.

The signals are deliberately *ordinal, not diagnostic*: "nothing is
interactive" is strong evidence against readiness regardless of why.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.adaptive_understanding.schemas import ReadinessSignal

# Thresholds below which a page is considered implausibly empty for a real,
# rendered application screen. These are deliberately low so that genuinely
# minimal pages (a bare confirmation screen, a single-field form) are not
# mistaken for unrendered ones -- the cost of a false "not ready" is one
# extra observation, but the cost of a false "ready" is reasoning from an
# empty DOM, which is far worse.
_SPARSE_INTERACTIVE_THRESHOLD = 2
_SPARSE_CONTENT_THRESHOLD = 2

# Weights express how strongly each signal argues its direction. They are
# combined by `readiness_estimator`, never used alone.
_SIGNAL_WEIGHTS: dict[str, float] = {
    "empty_document": 1.0,
    "no_interactive_elements": 0.9,
    "no_content": 0.8,
    "busy_region_present": 0.85,
    "progress_indicator_present": 0.8,
    "repeated_empty_containers": 0.5,
    "sparse_interactive_elements": 0.35,
    "sparse_content": 0.3,
    "pending_network_activity": 0.4,
    "failed_network_activity": 0.3,
    "console_errors_present": 0.25,
    "dom_still_changing": 0.7,
    "content_growing": 0.6,
    "dom_settled": 0.6,
    "interactive_surface_present": 0.8,
    "content_present": 0.6,
}


def _signal(kind: str, *, supports_ready: bool, observed_value: str, description: str) -> ReadinessSignal:
    return ReadinessSignal(
        signal_id=f"readiness-signal:{kind}",
        kind=kind,
        supports_ready=supports_ready,
        weight=_SIGNAL_WEIGHTS.get(kind, 0.3),
        observed_value=observed_value,
        description=description,
    )


def _interactive_count(model: Any) -> int:
    return len(getattr(model, "interactive_elements", None) or [])


def _content_count(model: Any) -> int:
    """Rendered content volume: headings plus text blocks. Counting both
    avoids treating a heading-only skeleton OR a text-only page as empty."""
    headings = len(getattr(model, "headings", None) or [])
    blocks = len(getattr(model, "text_blocks", None) or [])
    return headings + blocks


def _has_busy_region(model: Any) -> tuple[bool, str]:
    """`aria-busy="true"` is a W3C ARIA standard meaning "this region is
    being updated". Any conforming application may set it; no framework
    owns it."""
    for region in getattr(model, "regions", None) or []:
        attrs = getattr(region, "attributes", None) or {}
        if str(attrs.get("aria-busy", "")).lower() == "true":
            return True, getattr(region, "stable_id", "") or getattr(region, "region_type", "region")
    for el in getattr(model, "interactive_elements", None) or []:
        if str(getattr(el, "aria_busy", "") or "").lower() == "true":
            return True, getattr(el, "element_id", "") or "element"
    return False, ""


def _has_progress_indicator(model: Any) -> tuple[bool, str]:
    """`role="progressbar"` (and the closely-related `role="status"` used as
    a live loading announcement) are W3C ARIA standards."""
    for el in getattr(model, "interactive_elements", None) or []:
        role = str(getattr(el, "role", "") or "").lower()
        if role in {"progressbar", "status"}:
            return True, getattr(el, "element_id", "") or role
    for comp in getattr(model, "unknown_components", None) or []:
        role = str(getattr(comp, "role", "") or "").lower()
        if role == "progressbar":
            return True, getattr(comp, "stable_id", "") or role
    return False, ""


def _repeated_empty_containers(model: Any) -> tuple[bool, str]:
    """Placeholder scaffolding (commonly called a "skeleton") is, structurally,
    several same-shaped containers carrying no text. Detected by shape, never
    by CSS class name -- class names are a per-application convention, whereas
    "repeated empty boxes where content belongs" is universal."""
    collections = getattr(model, "collections", None) or []
    for coll in collections:
        rows = getattr(coll, "visible_rows", None) or []
        if len(rows) < 3:
            continue
        empty_rows = 0
        for row in rows:
            cells = getattr(row, "cell_values", None) or getattr(row, "values", None) or []
            if not any(str(c).strip() for c in cells):
                empty_rows += 1
        if empty_rows >= 3 and empty_rows == len(rows):
            return True, f"{empty_rows} empty repeated rows in collection {getattr(coll, 'stable_id', '') or '?'}"
    return False, ""


def _network_signals(model: Any) -> tuple[int, int]:
    """(pending, failed) request counts from already-captured network
    evidence. Pending requests suggest the page is still assembling itself;
    failures may explain why content never appeared."""
    pending = failed = 0
    for entry in getattr(model, "network_evidence", None) or []:
        if getattr(entry, "failed", False):
            failed += 1
            continue
        status = getattr(entry, "http_status", None)
        if status is None:
            pending += 1
    return pending, failed


def _console_error_count(page_state: Any) -> int:
    return len(getattr(page_state, "console_errors", None) or [])


def extract_signals(model: Any, page_state: Any = None) -> list[ReadinessSignal]:
    """All readiness signals derivable from a SINGLE observation.

    `model` is a `CanonicalPageModel`; `page_state` is the matching
    `PageState` (used only for console errors, which the canonical model
    does not carry). Both are duck-typed so this stays testable without
    constructing full perception objects.
    """
    signals: list[ReadinessSignal] = []

    interactive = _interactive_count(model)
    content = _content_count(model)

    if interactive == 0 and content == 0:
        signals.append(
            _signal(
                "empty_document",
                supports_ready=False,
                observed_value="0 interactive elements, 0 content blocks",
                description="Nothing has rendered: no controls and no text. The document shell exists but its content does not.",
            )
        )
    else:
        if interactive == 0:
            signals.append(
                _signal(
                    "no_interactive_elements",
                    supports_ready=False,
                    observed_value="0",
                    description="No control is clickable or fillable, so no action could be taken even if one were planned.",
                )
            )
        elif interactive < _SPARSE_INTERACTIVE_THRESHOLD:
            signals.append(
                _signal(
                    "sparse_interactive_elements",
                    supports_ready=False,
                    observed_value=str(interactive),
                    description="Very few controls are present for a rendered application screen.",
                )
            )
        else:
            signals.append(
                _signal(
                    "interactive_surface_present",
                    supports_ready=True,
                    observed_value=str(interactive),
                    description="A usable set of controls is present.",
                )
            )

        if content == 0:
            signals.append(
                _signal(
                    "no_content",
                    supports_ready=False,
                    observed_value="0",
                    description="No headings or text blocks have rendered.",
                )
            )
        elif content < _SPARSE_CONTENT_THRESHOLD:
            signals.append(
                _signal(
                    "sparse_content",
                    supports_ready=False,
                    observed_value=str(content),
                    description="Very little text has rendered.",
                )
            )
        else:
            signals.append(
                _signal(
                    "content_present",
                    supports_ready=True,
                    observed_value=str(content),
                    description="Readable content is present.",
                )
            )

    busy, busy_where = _has_busy_region(model)
    if busy:
        signals.append(
            _signal(
                "busy_region_present",
                supports_ready=False,
                observed_value=busy_where,
                description='A region declares aria-busy="true" (W3C ARIA), meaning the application itself reports it is still updating.',
            )
        )

    progress, progress_where = _has_progress_indicator(model)
    if progress:
        signals.append(
            _signal(
                "progress_indicator_present",
                supports_ready=False,
                observed_value=progress_where,
                description='A progress/status indicator (W3C ARIA role) is present, which the application uses to say work is in flight.',
            )
        )

    placeholders, placeholder_where = _repeated_empty_containers(model)
    if placeholders:
        signals.append(
            _signal(
                "repeated_empty_containers",
                supports_ready=False,
                observed_value=placeholder_where,
                description="Repeated same-shaped empty containers are present where records would appear -- structurally consistent with placeholder scaffolding.",
            )
        )

    pending, failed = _network_signals(model)
    if pending:
        signals.append(
            _signal(
                "pending_network_activity",
                supports_ready=False,
                observed_value=str(pending),
                description="Requests are still in flight, so content may still arrive.",
            )
        )
    if failed:
        signals.append(
            _signal(
                "failed_network_activity",
                supports_ready=False,
                observed_value=str(failed),
                description="Requests failed, which may explain missing content and is itself worth investigating.",
            )
        )

    console_errors = _console_error_count(page_state)
    if console_errors:
        signals.append(
            _signal(
                "console_errors_present",
                supports_ready=False,
                observed_value=str(console_errors),
                description="Scripting errors occurred, so rendering may be incomplete or broken.",
            )
        )

    return signals
