"""Shared, dependency-free evidence-extraction primitives.

These three functions were written for the visual-analysis call
(`app/perception/visual_analyzer.py`) and are, per docs/MODEL_CONTEXT_AUDIT.md
section H, the only per-element bounded evidence retrieval that already existed
anywhere in the codebase. Section I flagged that every other model call had no
access to them at all.

They live here now so both the visual analyzer and the general evidence
retriever (`app/gemma/evidence_retrieval.py`) share ONE implementation rather
than growing a second, drifting copy. `visual_analyzer` re-exports them under
their original private names, so nothing that imported them before changes.

Everything is duck-typed on purpose: these take plain mappings or any object
exposing the attributes, which keeps this module import-free and therefore
importable from either layer without a cycle.
"""

from __future__ import annotations

from typing import Any

NEARBY_TEXT_CHARS = 200


def dom_evidence_for(raw_el: dict[str, Any]) -> dict[str, Any]:
    """Structural DOM facts about one element. Never a selector or a subtree."""
    return {
        "tag": raw_el.get("tag"),
        "text": raw_el.get("text"),
        "class_name": raw_el.get("class_name"),
        "src": raw_el.get("src"),
        "alt": raw_el.get("alt"),
        "aria_label": raw_el.get("aria_label"),
    }


def accessibility_evidence_for(info: Any | None) -> dict[str, Any]:
    """Accessibility-tree facts about one element, or `{}` when unavailable."""
    if info is None:
        return {}
    return {
        "role": getattr(info, "role", None),
        "accessible_name": getattr(info, "accessible_name", None),
        "accessible_description": getattr(info, "accessible_description", None),
        "is_expanded": getattr(info, "is_expanded", None),
        "is_selected": getattr(info, "is_selected", None),
        "is_checked": getattr(info, "is_checked", None),
        "is_disabled": getattr(info, "is_disabled", None),
    }


def nearby_text_for(raw_el: dict[str, Any]) -> str:
    """Bounded surrounding text — the cheapest useful disambiguator for an
    icon-only or otherwise unlabelled control."""
    return str(
        raw_el.get("surrounding_text")
        or raw_el.get("text")
        or raw_el.get("accessible_name")
        or raw_el.get("label")
        or ""
    )[:NEARBY_TEXT_CHARS]
