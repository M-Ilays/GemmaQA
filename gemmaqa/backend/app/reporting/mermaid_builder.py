"""Mermaid diagram builders with label sanitization."""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlparse

from app.agent.explorer import strip_known_extension
from app.agent.memory import NavigationEdge
from app.schemas import ActionResult, PageState, Workflow


_UNSAFE = re.compile(r'[\[\]{}"\\|<>`]')
_WHITESPACE = re.compile(r"\s+")


def sanitize_mermaid_label(text: str | None, max_len: int = 48) -> str:
    """Sanitize node labels to prevent invalid Mermaid syntax."""
    raw = _WHITESPACE.sub(" ", (text or "").strip())
    raw = _UNSAFE.sub(" ", raw)
    raw = raw.replace("(", " ").replace(")", " ").replace("#", " ")
    raw = _WHITESPACE.sub(" ", raw).strip()
    if not raw:
        raw = "node"
    if len(raw) > max_len:
        raw = raw[: max_len - 1] + "…"
    return raw


def _humanize_segment(segment: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", strip_known_extension(segment or ""))
    if not cleaned:
        return ""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", cleaned)
    return spaced.replace("-", " ").replace("_", " ").strip()


def page_nav_label(page: PageState) -> str:
    """Prefer path/heading over a repeated site-wide document.title."""
    path = urlparse(page.url).path.strip("/")
    segment = path.split("/")[-1] if path else "home"
    path_label = _humanize_segment(segment) or "Home"
    title = (page.title or "").strip()
    heading = (page.headings[0] if page.headings else "") or ""
    generic_titles = {
        "",
        "app",
        "home",
        "contact list app",
        "untitled",
        "react app",
    }
    title_l = title.lower()
    if heading and (title_l in generic_titles or title_l == heading.lower()):
        if path_label.lower() not in heading.lower():
            return f"{path_label}: {heading}"
        return heading
    if title_l in generic_titles or (path_label and path_label.lower() not in title_l):
        if heading and heading.lower() != path_label.lower():
            return f"{path_label}: {heading}"
        return path_label
    if path and path_label.lower() not in title_l:
        return f"{path_label}: {title}"
    return title or path_label or page.url


def build_navigation_mermaid(
    pages: list[PageState],
    edges: Iterable[NavigationEdge] | None = None,
) -> str:
    """Application navigation map from visited pages and edges."""
    if not pages and not edges:
        return "flowchart TD\n  A[No pages visited yet]"

    unique_pages: list[PageState] = []
    seen_urls: set[str] = set()
    for page in pages:
        if page.url in seen_urls:
            continue
        seen_urls.add(page.url)
        unique_pages.append(page)

    lines = ["flowchart TD"]
    url_to_node: dict[str, str] = {}
    for idx, page in enumerate(unique_pages[:40]):
        node = f"P{idx}"
        url_to_node[page.url] = node
        label = sanitize_mermaid_label(page_nav_label(page))
        lines.append(f'  {node}["{label}"]')

    edge_list = list(edges or [])
    drawn = 0
    if edge_list:
        for eidx, edge in enumerate(edge_list[:60]):
            src = url_to_node.get(edge.source_url)
            dst = url_to_node.get(edge.target_url)
            if not src:
                src = f"S{eidx}"
                url_to_node[edge.source_url] = src
                path = urlparse(edge.source_url).path.strip("/") or edge.source_url
                seg = path.split("/")[-1] if "/" in path or path else path
                lines.append(
                    f'  {src}["{sanitize_mermaid_label(_humanize_segment(seg) or path)}"]'
                )
            if not dst:
                dst = f"T{eidx}"
                url_to_node[edge.target_url] = dst
                path = urlparse(edge.target_url).path.strip("/") or edge.target_url
                seg = path.split("/")[-1] if "/" in path or path else path
                lines.append(
                    f'  {dst}["{sanitize_mermaid_label(_humanize_segment(seg) or path)}"]'
                )
            if src == dst:
                continue
            via = sanitize_mermaid_label(
                getattr(edge, "action_label", None) or edge.via_action or "nav",
                24,
            )
            lines.append(f"  {src} -->|{via}| {dst}")
            drawn += 1
    if drawn == 0 and len(unique_pages) > 1:
        for idx in range(1, min(len(unique_pages), 40)):
            lines.append(f"  P{idx - 1} --> P{idx}")

    return "\n".join(lines)


def build_workflow_mermaid(actions: list[ActionResult]) -> str:
    """Action sequence workflow diagram."""
    if not actions:
        return "flowchart LR\n  S[Start] --> E[No actions yet]"

    lines = ["flowchart LR", "  S[Start]"]
    prev = "S"
    for idx, result in enumerate(actions[:40]):
        node = f"A{idx}"
        label = result.action.action.value
        if result.action.element_id:
            label = f"{label} {result.action.element_id}"
        label = sanitize_mermaid_label(label, 36)
        lines.append(f'  {node}["{label}"]')
        lines.append(f"  {prev} --> {node}")
        prev = node
    lines.append(f"  {prev} --> F[Finish]")
    return "\n".join(lines)


def build_user_journey_mermaid(journey_labels: list[str]) -> str:
    """User journey diagram from ordered page/step labels."""
    if not journey_labels:
        return "flowchart LR\n  S[Start] --> E[No journey recorded]"
    lines = ["flowchart LR", "  S[Start]"]
    prev = "S"
    for idx, label in enumerate(journey_labels[:25]):
        node = f"J{idx}"
        lines.append(f'  {node}["{sanitize_mermaid_label(label)}"]')
        lines.append(f"  {prev} --> {node}")
        prev = node
    lines.append(f"  {prev} --> E[End]")
    return "\n".join(lines)


def build_named_workflow_mermaid(workflow: Workflow) -> str:
    if not workflow.steps:
        name = sanitize_mermaid_label(workflow.name or "Workflow")
        return f"flowchart LR\n  S[Start] --> E[{name}]"
    lines = [
        "flowchart LR",
        f'  S["{sanitize_mermaid_label(workflow.starting_page or "Start")}"]',
    ]
    prev = "S"
    for idx, step in enumerate(workflow.steps[:30]):
        node = f"W{idx}"
        label = sanitize_mermaid_label(f"{step.order}. {step.action}")
        lines.append(f'  {node}["{label}"]')
        lines.append(f"  {prev} --> {node}")
        prev = node
    lines.append(f"  {prev} --> E[End]")
    return "\n".join(lines)
