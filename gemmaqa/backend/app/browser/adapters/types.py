"""Shared browser-adapter domain types (no MCP-specific names)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TargetRef:
    """Stable GemmaQA target reference — never arbitrary executable code."""

    target_id: str | None = None
    selector_hint: str | None = None
    role: str | None = None
    name: str | None = None
    tag: str | None = None
    href: str | None = None
    input_type: str | None = None
    placeholder: str | None = None
    test_id: str | None = None

    def display(self) -> str:
        return self.target_id or self.name or self.selector_hint or "unknown"


@dataclass
class AdapterCapabilities:
    navigate: bool = True
    click: bool = True
    fill: bool = True
    select: bool = True
    check: bool = True
    press: bool = True
    hover: bool = True
    go_back: bool = True
    reload: bool = True
    wait: bool = True
    screenshots: bool = True
    console_events: bool = True
    network_events: bool = True
    tabs: bool = False
    accessibility_snapshot: bool = True
    observe: bool = True
    submit: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "navigate": self.navigate,
            "click": self.click,
            "fill": self.fill,
            "select": self.select,
            "check": self.check,
            "press": self.press,
            "hover": self.hover,
            "go_back": self.go_back,
            "reload": self.reload,
            "wait": self.wait,
            "screenshots": self.screenshots,
            "console_events": self.console_events,
            "network_events": self.network_events,
            "tabs": self.tabs,
            "accessibility_snapshot": self.accessibility_snapshot,
            "observe": self.observe,
            "submit": self.submit,
        }


@dataclass
class AdapterActionResult:
    """Normalised adapter execution outcome for memory / evidence / analysis."""

    status: str  # completed | failed | blocked
    action: str
    target_id: str | None = None
    before_url: str = ""
    after_url: str = ""
    navigation_occurred: bool = False
    evidence_ids: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    network_errors: list[str] = field(default_factory=list)
    adapter: str = ""
    message: str = ""
    error: str | None = None
    inspected: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
