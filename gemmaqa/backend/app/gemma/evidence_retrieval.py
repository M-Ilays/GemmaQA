"""Targeted, bounded evidence retrieval.

The audit (docs/MODEL_CONTEXT_AUDIT.md, section I) found no Evidence Retriever
of any kind outside the visual-analysis call, and no way for any other model
call to cite a piece of evidence. This module supplies one, built on the
primitives that call already used (`app/perception/evidence_primitives.py`)
rather than a parallel browser subsystem.

Non-negotiables, each of them a direct answer to something the audit found:

* **Never the whole DOM.** Retrieval is per-target — a named control, form,
  dialog, or collection — and every result is size-capped. There is no
  "give me the page" mode, by construction.
* **Sanitize before the model, always.** `sanitize_html_fragment` strips
  scripts, styles, event handlers, and credential-shaped attributes while
  keeping the semantics a QA reasoner actually needs: role, label, name, type,
  required, disabled, and the aria-* relationships.
* **Every item gets a stable, content-derived id.** Deterministic ids make a
  stale reference detectable: the same observation yields the same id, so an id
  that no longer appears in the current registry is provably from an earlier
  observation rather than merely unfamiliar.
* **Refuse unknown or stale targets.** Retrieval for an element the current
  observation does not contain returns nothing and says so, rather than
  fabricating an empty-but-plausible record.

One-pass retrieval only. The interfaces are shaped so a future model turn could
request more evidence (see `retrieve_for_targets`), but no recursive model loop
is introduced here — an unbounded evidence-request loop is a far larger safety
question than this milestone should decide.
"""

from __future__ import annotations

import re
from typing import Any

from app.gemma.model_context import ContextProvenance, EvidenceRegistry
from app.perception.evidence_primitives import (
    accessibility_evidence_for,
    dom_evidence_for,
    nearby_text_for,
)
from app.utils.logging import get_logger
from app.utils.sanitization import MASK, sanitize_text, sanitize_url

logger = get_logger("gemma.evidence_retrieval")

# Retrieval caps.
MAX_TARGETS = 12
MAX_FRAGMENT_CHARS = 1200
MAX_NETWORK_ITEMS = 8
MAX_CONSOLE_ITEMS = 8
MAX_TRANSITION_ITEMS = 5

# The closed vocabulary of retrievable evidence kinds. Enforced by
# `retrieve_*` via `_assert_kind`, so a typo becomes an error at the point of
# registration rather than an unrecognised `kind` string in a report.
EVIDENCE_KINDS = frozenset(
    {
        "dom_subtree",
        "accessibility_subtree",
        "screenshot_reference",
        "network_failure",
        "console_error",
        "form_component",
        "graph_evidence",
        "transition_evidence",
    }
)

# Attributes worth keeping on a sanitized fragment: everything a QA reasoner
# needs to understand what a control IS, and nothing that helps it guess at a
# selector or leak a value.
_SEMANTIC_ATTRIBUTES = frozenset(
    {
        "role",
        "type",
        "name",
        "id",
        "for",
        "label",
        "title",
        "alt",
        "placeholder",
        "required",
        "disabled",
        "readonly",
        "checked",
        "selected",
        "multiple",
        "href",
        "value",
    }
)

_SCRIPT_OR_STYLE = re.compile(r"(?is)<\s*(script|style)\b.*?<\s*/\s*\1\s*>")
_COMMENT = re.compile(r"(?s)<!--.*?-->")
_TAG = re.compile(r"(?s)<\s*(/?)\s*([A-Za-z][\w:-]*)((?:\s+[^<>]*?)?)\s*(/?)\s*>")
_ATTRIBUTE = re.compile(r"""([A-Za-z_:][\w:.-]*)\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'<>`]+))""")
_SENSITIVE_ATTR_HINTS = ("password", "token", "secret", "api_key", "apikey", "auth", "session", "cookie", "csrf")


def sanitize_html_fragment(html: str, *, max_chars: int = MAX_FRAGMENT_CHARS) -> str:
    """Reduce an HTML fragment to its semantic skeleton.

    Removes script/style/comments entirely, then rewrites every remaining tag to
    keep only `_SEMANTIC_ATTRIBUTES`, masking any value whose attribute name
    looks credential-bearing and any `value=` on a password input. The result is
    valid-enough markup for a model to read structure from, and carries no
    scripts, no styling, no data-* payloads, and no secrets.

    Truncation is at a tag boundary, never mid-tag, so the fragment never ends
    in a half-written element.
    """
    if not html:
        return ""

    text = _SCRIPT_OR_STYLE.sub("", str(html))
    text = _COMMENT.sub("", text)

    def _rewrite(match: re.Match[str]) -> str:
        closing, tag, attrs, self_closing = match.groups()
        tag_lower = tag.lower()
        if tag_lower in {"script", "style"}:
            return ""
        if closing:
            return f"</{tag_lower}>"

        attr_text_raw = attrs or ""
        kept: list[tuple[str, str]] = []
        is_password = False

        for attr_match in _ATTRIBUTE.finditer(attr_text_raw):
            name = attr_match.group(1).lower()
            value = attr_match.group(3) or attr_match.group(4) or attr_match.group(5) or ""
            if name == "type" and value.lower() == "password":
                is_password = True
            if not (name in _SEMANTIC_ATTRIBUTES or name.startswith("aria-")):
                continue
            if any(hint in name for hint in _SENSITIVE_ATTR_HINTS) or any(
                hint in value.lower() for hint in ("bearer ", "sessionid=")
            ):
                value = MASK
            kept.append((name, value))

        # Valueless boolean attributes (`required`, `disabled`, `checked`) carry
        # real QA meaning and never match the name="value" pattern above.
        consumed = {m.group(1).lower() for m in _ATTRIBUTE.finditer(attr_text_raw)}
        for token in re.findall(r"(?:^|\s)([A-Za-z_:][\w:.-]*)(?=\s|$)", _ATTRIBUTE.sub(" ", attr_text_raw)):
            name = token.lower()
            if name in consumed or name not in _SEMANTIC_ATTRIBUTES:
                continue
            consumed.add(name)
            kept.append((name, ""))

        rendered: list[str] = []
        for name, value in kept:
            if name == "value" and is_password:
                value = MASK
            rendered.append(f'{name}="{sanitize_text(str(value))}"' if value != "" else name)
        attr_text = (" " + " ".join(rendered)) if rendered else ""
        suffix = "/" if self_closing else ""
        return f"<{tag_lower}{attr_text}{suffix}>"

    text = _TAG.sub(_rewrite, text)
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    boundary = cut.rfind(">")
    return (cut[: boundary + 1] if boundary > 0 else cut) + " <!--truncated-->"


# ---------------------------------------------------------------------------
# Structured retrieval
# ---------------------------------------------------------------------------


def _canonical_index(canonical_model: Any) -> dict[str, Any]:
    index: dict[str, Any] = {}
    if canonical_model is None:
        return index
    for attr in ("interactive_elements", "forms", "tables", "collections", "dialogs", "unknown_components"):
        for item in getattr(canonical_model, attr, None) or []:
            for key_attr in ("element_id", "form_id", "table_id", "stable_id"):
                key = getattr(item, key_attr, None)
                if key:
                    index.setdefault(str(key), item)
    return index


def _element_payload(item: Any) -> dict[str, Any]:
    """Structured stand-in for a DOM subtree.

    GemmaQA does not retain raw HTML after observation (by design — see the
    audit's question 8), so the honest bounded equivalent of "the subtree for
    this control" is its structured description. `sanitize_html_fragment` exists
    for callers that DO hold a fragment; this is the path for those that hold
    perception output instead.
    """
    raw = {
        "tag": getattr(item, "dom_tag", None) or getattr(item, "tag", None),
        "text": getattr(item, "text", None),
        "class_name": (getattr(item, "attributes", None) or {}).get("class"),
        "src": getattr(item, "src", None),
        "alt": getattr(item, "alt_text", None),
        "aria_label": (getattr(item, "attributes", None) or {}).get("aria-label")
        or getattr(item, "accessible_name", None),
        "surrounding_text": getattr(item, "surrounding_text", None),
        "accessible_name": getattr(item, "accessible_name", None),
        "label": getattr(item, "label", None),
    }
    payload = {k: v for k, v in dom_evidence_for(raw).items() if v not in (None, "")}
    nearby = nearby_text_for(raw)
    if nearby:
        payload["nearby_text"] = sanitize_text(nearby)
    href = getattr(item, "url", None) or getattr(item, "href", None)
    if href:
        payload["href"] = sanitize_url(str(href))
    return payload


def _accessibility_payload(item: Any) -> dict[str, Any]:
    info = type(
        "_A",
        (),
        {
            "role": getattr(item, "aria_role", None) or getattr(item, "role", None),
            "accessible_name": getattr(item, "accessible_name", None),
            "accessible_description": getattr(item, "accessible_description", None),
            "is_expanded": getattr(item, "is_expanded", None),
            "is_selected": getattr(item, "is_selected", None),
            "is_checked": getattr(item, "is_checked", None),
            "is_disabled": (not getattr(item, "is_enabled", True))
            if getattr(item, "is_enabled", None) is not None
            else None,
        },
    )()
    return {k: v for k, v in accessibility_evidence_for(info).items() if v not in (None, "")}


def _assert_kind(kind: str) -> str:
    if kind not in EVIDENCE_KINDS:
        raise ValueError(f"Unknown evidence kind {kind!r} (expected one of {sorted(EVIDENCE_KINDS)})")
    return kind


def _summarize(kind: str, target: str, payload: dict[str, Any]) -> str:
    parts = [f"{k}={v}" for k, v in list(payload.items())[:6] if v not in (None, "")]
    return f"{kind} for {target}: " + ("; ".join(parts) if parts else "no structured detail available")


def retrieve_for_targets(
    *,
    registry: EvidenceRegistry,
    target_element_ids: list[str],
    canonical_model: Any = None,
    page_state: Any = None,
    observation_version: str = "",
    max_targets: int = MAX_TARGETS,
) -> list[str]:
    """Register DOM + accessibility evidence for specific, known targets.

    Returns the ids that were registered. Unknown targets are skipped with a
    warning — never silently, and never with an invented placeholder record.
    """
    index = _canonical_index(canonical_model)
    if page_state is not None:
        for el in getattr(page_state, "interactive_elements", None) or []:
            element_id = getattr(el, "element_id", None)
            if element_id:
                index.setdefault(str(element_id), el)
        for form in getattr(page_state, "forms", None) or []:
            form_id = getattr(form, "form_id", None)
            if form_id:
                index.setdefault(str(form_id), form)
        # Tables are legitimate, referenceable targets (inspect_table acts on
        # one), so they must be retrievable rather than logged as unknown.
        for table in getattr(page_state, "tables", None) or []:
            table_id = getattr(table, "table_id", None)
            if table_id:
                index.setdefault(str(table_id), table)

    registered: list[str] = []
    for target in list(dict.fromkeys(target_element_ids))[:max_targets]:
        item = index.get(str(target))
        if item is None:
            logger.warning(
                "Evidence retrieval refused for unknown/stale reference %r (not in current observation)",
                target,
            )
            continue

        dom_payload = _element_payload(item)
        if dom_payload:
            registered.append(
                registry.register(
                    kind=_assert_kind("dom_subtree"),
                    summary=_summarize("DOM structure", str(target), dom_payload),
                    reference=str(target),
                    provenance=ContextProvenance(
                        producer="PerceptionEngine/PageObserver", source_ref=str(target)
                    ),
                    observation_version=observation_version or None,
                ).evidence_id
            )

        a11y_payload = _accessibility_payload(item)
        if a11y_payload:
            registered.append(
                registry.register(
                    kind=_assert_kind("accessibility_subtree"),
                    summary=_summarize("Accessibility", str(target), a11y_payload),
                    reference=str(target),
                    provenance=ContextProvenance(
                        producer="AccessibilityExtractor", source_ref=str(target)
                    ),
                    observation_version=observation_version or None,
                ).evidence_id
            )
    return registered


def retrieve_technical_evidence(
    *,
    registry: EvidenceRegistry,
    page_state: Any = None,
    canonical_model: Any = None,
    screenshot_path: str = "",
    observation_version: str = "",
) -> dict[str, Any]:
    """Register the page-level technical evidence a defect claim would cite.

    Console and network failures, plus a screenshot REFERENCE (never the image
    bytes — the image, when sent at all, travels on the provider's own image
    channel). Returns a compact map for the prompt's technical_evidence section.
    """
    console_ids: list[str] = []
    network_ids: list[str] = []

    for entry in (getattr(page_state, "console_errors", None) or [])[:MAX_CONSOLE_ITEMS]:
        console_ids.append(
            registry.register(
                kind=_assert_kind("console_error"),
                summary=sanitize_text(str(entry))[:240],
                provenance=ContextProvenance(producer="PageObserver.console"),
                observation_version=observation_version or None,
            ).evidence_id
        )

    canonical_failures = [
        n for n in (getattr(canonical_model, "network_evidence", None) or []) if getattr(n, "failed", False)
    ][:MAX_NETWORK_ITEMS]
    if canonical_failures:
        for failure in canonical_failures:
            summary = " ".join(
                str(part)
                for part in (
                    getattr(failure, "method", "") or "",
                    getattr(failure, "http_status", "") or "",
                    sanitize_text(str(getattr(failure, "failure_text", "") or "")),
                )
                if part
            ).strip()
            network_ids.append(
                registry.register(
                    kind=_assert_kind("network_failure"),
                    summary=summary[:240] or "network failure with no detail",
                    provenance=ContextProvenance(producer="PerceptionEngine.network_evidence"),
                    observation_version=observation_version or None,
                ).evidence_id
            )
    else:
        for entry in (getattr(page_state, "network_failures", None) or [])[:MAX_NETWORK_ITEMS]:
            network_ids.append(
                registry.register(
                    kind=_assert_kind("network_failure"),
                    summary=sanitize_text(str(entry))[:240],
                    provenance=ContextProvenance(producer="PageObserver.network"),
                    observation_version=observation_version or None,
                ).evidence_id
            )

    screenshot_id = ""
    if screenshot_path:
        screenshot_id = registry.register(
            kind=_assert_kind("screenshot_reference"),
            summary="Screenshot of the current page state",
            reference=str(screenshot_path),
            provenance=ContextProvenance(producer="EvidenceCollector"),
            observation_version=observation_version or None,
        ).evidence_id

    payload: dict[str, Any] = {}
    if console_ids:
        payload["console_error_evidence_ids"] = console_ids
    if network_ids:
        payload["network_failure_evidence_ids"] = network_ids
    if screenshot_id:
        payload["screenshot_evidence_id"] = screenshot_id
    return payload


def retrieve_transition_evidence(
    *,
    registry: EvidenceRegistry,
    run_memory: Any,
    limit: int = MAX_TRANSITION_ITEMS,
) -> list[str]:
    """Register recent observed state transitions as citable evidence."""
    if run_memory is None:
        return []
    try:
        transitions = list(run_memory.observed_state_transitions())[-limit:]
    except Exception:  # pragma: no cover - defensive
        return []
    ids: list[str] = []
    for transition in transitions:
        summary = (
            f"{getattr(transition, 'from_state', '?')} -> {getattr(transition, 'to_state', '?')}"
            f" via {getattr(transition, 'trigger', 'unknown trigger')}"
        )
        ids.append(
            registry.register(
                kind=_assert_kind("transition_evidence"),
                summary=sanitize_text(summary)[:240],
                provenance=ContextProvenance(producer="AdaptiveApplicationUnderstandingEngine"),
            ).evidence_id
        )
    return ids
