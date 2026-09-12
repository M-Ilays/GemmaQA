"""Module/page name inference from URLs."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.agent.explorer import humanize_path_segment, module_name_from_url  # noqa: E402


def test_html_extension_stripped_not_mangled():
    """Regression: "inventory.html" was losing only the dot ("." is not in the
    cleaning regex's allowed set) while keeping the letters "html", producing the
    unreadable module name "Inventoryhtml" instead of "Inventory"."""
    assert module_name_from_url("https://example.com/inventory.html") == "Inventory"


def test_hyphenated_html_extension_stripped():
    assert (
        module_name_from_url("https://example.com/inventory-item.html")
        == "Inventory Item"
    )


def test_other_known_extensions_stripped():
    assert module_name_from_url("https://example.com/dashboard.php") == "Dashboard"
    assert module_name_from_url("https://example.com/reports.aspx") == "Reports"


def test_humanize_path_segment_strips_extension_directly():
    assert humanize_path_segment("inventory.html") == "Inventory"
    assert humanize_path_segment("add-contact") == "Add Contact"


def test_mermaid_nav_label_strips_extension():
    """Regression: the mermaid diagram builder had its own parallel copy of this same
    segment-cleaning logic (_humanize_segment in mermaid_builder.py) that wasn't fixed
    alongside module_name_from_url — so the "Modules" section correctly showed
    "Inventory" while the Navigation Structure diagram still showed "inventoryhtml"."""
    from app.reporting.mermaid_builder import page_nav_label
    from app.schemas import PageState
    from app.utils.ids import new_id

    page = PageState(
        page_id=new_id(),
        url="https://example.com/inventory-item.html",
        title="Swag Labs",
    )
    label = page_nav_label(page)
    assert "html" not in label.lower()
    assert "inventory item" in label.lower()


def test_canonical_module_key_strips_extension():
    """Third parallel copy of the same bug, in application/store.py's fallback path
    (triggered when no module_guess/heading is available from page classification)."""
    from app.application.store import canonical_module_key

    _, display = canonical_module_key("", path="/inventory.html")
    assert "html" not in display.lower()
    assert display.lower() == "inventory"
