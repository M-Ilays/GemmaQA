"""Canonical Page Model — an application-neutral representation of everything
GemmaQA can explicitly observe on the current browser page.

Design rules (see docs/CANONICAL_PAGE_MODEL.md for the full rationale):

- No business-specific fields (no "customer", "invoice", "product", "job",
  "contact", ...). Every field here must make sense for ANY web application.
- Reuse existing schemas instead of duplicating them: `InteractiveElement`,
  `FormDescriptor`, and `TableDescriptor` are imported from `app.schemas` as-is
  (those three were extended in place, backward-compatibly, to carry the
  generic perception fields — see app/schemas.py).
- This module is a passive DATA MODEL plus a deterministic adapter
  (`CanonicalPageModel.from_page_state`) that reuses already-observed data.
  It does not call an LLM, does not invent selectors or browser actions, and
  does not add any hypothesis/inference logic — every field it populates is a
  direct, mechanical mapping from data `PageObserver` already extracted.
- Nothing here changes Planner/FrontierBuilder behavior; nothing currently
  reads this model at runtime.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from app.application.url_normalize import origin_of, same_origin
from app.schemas import (
    FormDescriptor,
    InteractiveElement,
    NetworkEntry,
    PageState,
    TableDescriptor,
)
from app.utils.ids import new_id

# ---------------------------------------------------------------------------
# Controlled, generic (non-business) vocabularies
# ---------------------------------------------------------------------------

REGION_TYPES = frozenset(
    {
        # Original, markup/ARIA-landmark-level types (kept for backward compatibility).
        "header",
        "nav",
        "main",
        "aside",
        "footer",
        "unknown",
        # Richer, still application-neutral region types the Perception Engine's
        # region_classifier can assign — structural/functional roles that exist on
        # any web application, never a business concept.
        "primary_navigation",
        "secondary_navigation",
        "content_navigation",
        "sidebar",
        "main_content",
        "workflow_region",
        "form_region",
        "data_table",
        "card_group",
        "dialog",
        "utility_region",
        "legal_region",
        "unknown_region",
    }
)
DIALOG_TYPES = frozenset({"dialog", "modal", "drawer", "popover"})
ALERT_SEVERITIES = frozenset({"info", "success", "warning", "error"})
TEXT_BLOCK_TYPES = frozenset({"paragraph", "summary", "description", "label", "caption", "other"})
LAYOUT_HINTS = frozenset({"top", "bottom", "left", "right", "center"})
SOURCE_KINDS = frozenset({"dom", "aria", "heuristic", "network", "inferred", "visual"})
STATUS_KINDS = frozenset({"observed", "stale", "inferred", "unknown"})
EVIDENCE_KINDS = frozenset({"screenshot", "dom_snapshot", "network", "console", "manual"})

# The exact 8 categories the visual observation policy classifies IMAGES into
# (a deterministic, heuristic-first step — see app.perception.image_extractor;
# the visual model is only consulted for the "meaningful" subset of these).
IMAGE_SEMANTIC_TYPES = frozenset(
    {"decorative", "informational", "interactive", "document", "chart", "avatar", "logo", "unknown"}
)
# Broader vocabulary for VisualEvidence, which can describe non-image targets
# too (icon-only controls, canvases, ambiguous SVG controls, layout groups,
# suspected visual defects) — superset of IMAGE_SEMANTIC_TYPES so an image
# classification is always a valid VisualEvidence classification too.
VISUAL_ELEMENT_SEMANTIC_TYPES = IMAGE_SEMANTIC_TYPES | {
    "icon_button",
    "canvas_widget",
    "layout_group",
    "possible_defect",
}


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


class EvidenceReference(BaseModel):
    """A pointer to supporting evidence for a perception claim — a screenshot, a
    DOM snapshot fragment, a network/console entry, or a manual note. Never large
    binary data inline; always a reference other tooling can resolve."""

    evidence_id: str = Field(default_factory=new_id)
    kind: str = "screenshot"
    reference: str = ""
    description: str = ""

    @field_validator("kind")
    @classmethod
    def _validate_kind(cls, value: str) -> str:
        if value not in EVIDENCE_KINDS:
            raise ValueError(f"Invalid evidence kind: {value!r} (expected one of {sorted(EVIDENCE_KINDS)})")
        return value


class ConfidenceScore(BaseModel):
    """A confidence value with a stated basis — never a bare float with no
    justification, so a downstream reader can tell a certainty from a guess."""

    value: float = 1.0
    basis: str = "direct_observation"  # direct_observation | heuristic | inferred | llm_hypothesis
    notes: str = ""

    @field_validator("value")
    @classmethod
    def _clamp_value(cls, value: float) -> float:
        if not (0.0 <= value <= 1.0):
            raise ValueError(f"confidence value must be in [0, 1], got {value}")
        return value


class PerceivedElementBase(BaseModel):
    """Common fields every meaningful perceived object carries, where applicable.
    Subclasses that don't need a given field simply leave it at its default —
    that is intentional per the task's "where applicable" instruction, not an
    oversight."""

    stable_id: str = Field(default_factory=new_id)
    element_id: Optional[str] = None
    parent_region_id: Optional[str] = None
    source: str = "dom"
    text: Optional[str] = None
    accessible_name: Optional[str] = None
    accessible_description: Optional[str] = None
    dom_tag: Optional[str] = None
    aria_role: Optional[str] = None
    attributes: dict[str, str] = Field(default_factory=dict)
    is_visible: bool = True
    is_enabled: bool = True
    is_expanded: Optional[bool] = None
    is_selected: Optional[bool] = None
    is_checked: Optional[bool] = None
    is_required: bool = False
    url: Optional[str] = None
    is_external_url: Optional[bool] = None
    bounding_box: Optional[dict[str, float]] = None
    confidence: ConfidenceScore = Field(default_factory=ConfidenceScore)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    status: str = "observed"
    fingerprint_contribution: Optional[str] = None

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> object:
        """Accept a bare float/int as shorthand for `ConfidenceScore(value=...)` —
        every extractor module that only has a single number to report (no
        stated basis/notes) can pass it directly instead of constructing the
        richer object every time."""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return ConfidenceScore(value=float(value))
        return value

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        if value not in SOURCE_KINDS:
            raise ValueError(f"Invalid source: {value!r} (expected one of {sorted(SOURCE_KINDS)})")
        return value

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in STATUS_KINDS:
            raise ValueError(f"Invalid status: {value!r} (expected one of {sorted(STATUS_KINDS)})")
        return value


# ---------------------------------------------------------------------------
# Structural / layout
# ---------------------------------------------------------------------------


class PageRegion(PerceivedElementBase):
    """A structural landmark region (header/nav/main/aside/footer) — purely
    structural, never a business concept."""

    region_type: str = "unknown"
    landmark_role: Optional[str] = None
    child_region_ids: list[str] = Field(default_factory=list)

    @field_validator("region_type")
    @classmethod
    def _validate_region_type(cls, value: str) -> str:
        if value not in REGION_TYPES:
            raise ValueError(f"Invalid region_type: {value!r} (expected one of {sorted(REGION_TYPES)})")
        return value


class VisualRegion(PerceivedElementBase):
    """A layout-based region inferred purely from bounding-box geometry (not
    semantic markup) — e.g. a cluster of elements sharing the page's left edge.
    Distinct from `PageRegion`, which is markup/ARIA-derived."""

    layout_hint: Optional[str] = None
    member_element_ids: list[str] = Field(default_factory=list)

    @field_validator("layout_hint")
    @classmethod
    def _validate_layout_hint(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in LAYOUT_HINTS:
            raise ValueError(f"Invalid layout_hint: {value!r} (expected one of {sorted(LAYOUT_HINTS)})")
        return value


class HeadingDescriptor(PerceivedElementBase):
    level: int = 1

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: int) -> int:
        if not (1 <= value <= 6):
            raise ValueError(f"heading level must be 1-6, got {value}")
        return value


class TextBlockDescriptor(PerceivedElementBase):
    block_type: str = "paragraph"

    @field_validator("block_type")
    @classmethod
    def _validate_block_type(cls, value: str) -> str:
        if value not in TEXT_BLOCK_TYPES:
            raise ValueError(f"Invalid block_type: {value!r} (expected one of {sorted(TEXT_BLOCK_TYPES)})")
        return value


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


class NavigationItem(PerceivedElementBase):
    target_url: Optional[str] = None
    is_current: Optional[bool] = None  # aria-current / "you are here"


class NavigationRegion(PerceivedElementBase):
    items: list[NavigationItem] = Field(default_factory=list)
    landmark_role: Optional[str] = None


class BreadcrumbDescriptor(PerceivedElementBase):
    ordinal: int = 0
    target_url: Optional[str] = None


class PaginationDescriptor(PerceivedElementBase):
    current_page: Optional[int] = None
    total_pages: Optional[int] = None
    item_ids: list[str] = Field(default_factory=list)


class LinkDescriptor(PerceivedElementBase):
    target_url: Optional[str] = None
    is_external: Optional[bool] = None


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


class TabDescriptor(PerceivedElementBase):
    controls_panel_id: Optional[str] = None


class TabGroup(PerceivedElementBase):
    tabs: list[TabDescriptor] = Field(default_factory=list)
    selected_tab_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------


class ImageDescriptor(PerceivedElementBase):
    alt_text: Optional[str] = None
    surrounding_text: Optional[str] = None
    src: Optional[str] = None
    # Deterministic, heuristic-first classification (never an LLM call) — see
    # image_extractor.classify_image_semantic(). Only "meaningful" categories
    # (informational/document/chart/interactive/unknown) are ever escalated to
    # the visual model by VisualObservationPolicy; decorative/avatar/logo are not.
    visual_semantic_type: str = "unknown"

    @field_validator("visual_semantic_type")
    @classmethod
    def _validate_visual_semantic_type(cls, value: str) -> str:
        if value not in IMAGE_SEMANTIC_TYPES:
            raise ValueError(
                f"Invalid visual_semantic_type: {value!r} (expected one of {sorted(IMAGE_SEMANTIC_TYPES)})"
            )
        return value


# ---------------------------------------------------------------------------
# Overlays
# ---------------------------------------------------------------------------


class DialogDescriptor(PerceivedElementBase):
    dialog_type: str = "dialog"
    is_open: bool = True
    contained_element_ids: list[str] = Field(default_factory=list)

    @field_validator("dialog_type")
    @classmethod
    def _validate_dialog_type(cls, value: str) -> str:
        if value not in DIALOG_TYPES:
            raise ValueError(f"Invalid dialog_type: {value!r} (expected one of {sorted(DIALOG_TYPES)})")
        return value


class AlertDescriptor(PerceivedElementBase):
    severity: str = "info"

    @field_validator("severity")
    @classmethod
    def _validate_severity(cls, value: str) -> str:
        if value not in ALERT_SEVERITIES:
            raise ValueError(f"Invalid severity: {value!r} (expected one of {sorted(ALERT_SEVERITIES)})")
        return value


# ---------------------------------------------------------------------------
# Network + unknowns
# ---------------------------------------------------------------------------


class NetworkEvidence(PerceivedElementBase):
    method: Optional[str] = None
    http_status: Optional[int] = None
    resource_type: Optional[str] = None
    failed: bool = False
    failure_text: Optional[str] = None
    timing_ms: Optional[float] = None


class UnknownComponent(PerceivedElementBase):
    """An element GemmaQA detected as *something* (interactive-looking, has a
    bounding box) but could not classify into any known descriptor type. The
    existence of this object is itself the finding — see
    docs/PAGE_PERCEPTION_AUDIT.md §3 ("no fallback bucket for unknown
    components"). Not yet populated by the observer (see docs/CANONICAL_PAGE_MODEL.md)."""

    detection_reason: str = ""


# ---------------------------------------------------------------------------
# Visual evidence — the Visual Observation Policy's output, attached to
# canonical elements. See docs/VISUAL_OBSERVATION_POLICY.md.
# ---------------------------------------------------------------------------


class VisualEvidence(BaseModel):
    """One structured, validated visual-model observation about ONE existing
    canonical element (never a newly-invented one — `element_id` is checked
    against the known element set before this is ever constructed by the
    parser, see app.gemma.parser.parse_visual_analysis). This is always
    ADDITIONAL evidence layered onto deterministic DOM/accessibility evidence
    already on the target element — never a replacement for it, and never the
    source of a browser action (no selector field exists here on purpose)."""

    evidence_id: str = Field(default_factory=new_id)
    element_id: str
    visual_semantic_type: str = "unknown"
    description: str = ""
    confidence: ConfidenceScore = Field(
        default_factory=lambda: ConfidenceScore(value=0.5, basis="llm_hypothesis")
    )
    evidence: list[str] = Field(default_factory=list)
    further_interaction_recommended: bool = False
    trigger_reasons: list[str] = Field(default_factory=list)
    screenshot_reference: Optional[EvidenceReference] = None
    source: str = "visual"

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> object:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return ConfidenceScore(value=float(value), basis="llm_hypothesis")
        return value

    @field_validator("visual_semantic_type")
    @classmethod
    def _validate_visual_semantic_type(cls, value: str) -> str:
        if value not in VISUAL_ELEMENT_SEMANTIC_TYPES:
            raise ValueError(
                f"Invalid visual_semantic_type: {value!r} "
                f"(expected one of {sorted(VISUAL_ELEMENT_SEMANTIC_TYPES)})"
            )
        return value

    @field_validator("element_id")
    @classmethod
    def _validate_element_id(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("VisualEvidence.element_id must not be empty")
        return value


# ---------------------------------------------------------------------------
# Record collections — the universal "data grid" model. Application-neutral:
# a collection is identified by STRUCTURE (repeated rows/cards, ARIA grid
# roles, header-like repeated labels) never by a business word. Covers
# native <table>, role="grid"/"treegrid"/"table"+role="row", div-based
# repeated rows, and repeated record cards under one shape so a Vue/React/
# Angular list page is represented the same way as a semantic <table>.
# ---------------------------------------------------------------------------

COLLECTION_TYPES = frozenset(
    {
        "native_table",
        "aria_grid",
        "aria_treegrid",
        "div_row_group",
        "card_group",
        "unknown_collection",
    }
)


class CollectionColumn(PerceivedElementBase):
    """One inferred/observed column — `header_text` is empty when no
    header-like row exists (a div-grid with no header row is still a valid
    collection; its columns are then purely positional)."""

    column_index: int = 0
    header_text: Optional[str] = None
    data_type: Optional[str] = None  # numeric | date | text | unknown


class CollectionAction(PerceivedElementBase):
    """One control associated with a collection — either scoped to a single
    row (`scope="row"`, `row_id` set) or global to the whole collection
    (`scope="global"`, e.g. an "Add"/"Create" button placed above the grid).
    `semantic_action` is populated by `action_semantics.py` where available —
    never invented here."""

    scope: str = "row"  # row | global
    row_id: Optional[str] = None
    semantic_action: Optional[str] = None


class CollectionRow(PerceivedElementBase):
    row_index: int = 0
    # Whether clicking the ROW itself (not a button inside it) opens the record.
    # Structural evidence only — an onclick attribute, an interactive ARIA role,
    # keyboard focusability, a pointer cursor, or a row-spanning link. Without
    # this the row is data rather than a way in, and a record list offers no
    # route to the record's own update/delete controls.
    is_activatable: bool = False
    activation_evidence: list[str] = Field(default_factory=list)
    # "observed"  — a DOM signal proves the row is interactive.
    # "structural_hypothesis" — no signal, but the row's shape says it probably
    #                is; frameworks attach click handlers in JavaScript and leave
    #                no DOM trace, so absence of a signal proves nothing.
    # "none"     — the row carries its own controls, so those are the actions.
    activation_basis: str = "none"
    cell_values: list[str] = Field(default_factory=list)
    # A best-effort, deterministic hint for "what identifies this row" (e.g.
    # the first non-empty cell, or a stable id-like attribute) — NEVER a
    # claim that this is the record's real primary key, just the strongest
    # available structural signal, stated with its own confidence.
    identity_hint: Optional[str] = None
    row_action_ids: list[str] = Field(default_factory=list)


class RecordCollection(PerceivedElementBase):
    """A universal "this page shows a set of records" signal — the direct
    answer to "detect no tables on div-based data grids" from the CRUD
    surface discovery task. One instance per detected collection (a page can
    show more than one, e.g. a grid plus a "recently viewed" card strip)."""

    collection_type: str = "unknown_collection"
    columns: list[CollectionColumn] = Field(default_factory=list)
    row_count: int = 0
    visible_rows: list[CollectionRow] = Field(default_factory=list)
    row_actions: list[CollectionAction] = Field(default_factory=list)
    global_actions: list[CollectionAction] = Field(default_factory=list)
    has_pagination: bool = False
    pagination_id: Optional[str] = None
    has_search_control: bool = False
    search_control_ids: list[str] = Field(default_factory=list)
    has_filter_controls: bool = False
    filter_control_ids: list[str] = Field(default_factory=list)
    # Structural/framework-neutral signals this collection was detected from
    # — e.g. "repeated_class_signature", "aria_grid_role", "native_table",
    # "vertical_stack_layout" — never a framework name (React/Vue/Angular
    # produce the SAME structural signals; detection never special-cases a
    # framework's internals, per the task's explicit constraint).
    structural_signals: list[str] = Field(default_factory=list)

    @field_validator("collection_type")
    @classmethod
    def _validate_collection_type(cls, value: str) -> str:
        if value not in COLLECTION_TYPES:
            raise ValueError(f"Invalid collection_type: {value!r} (expected one of {sorted(COLLECTION_TYPES)})")
        return value


# ---------------------------------------------------------------------------
# Form intent classification — WHAT a form is for, never assumed from "it
# has input elements" alone. See docs/CRUD_SURFACE_DISCOVERY.md.
# ---------------------------------------------------------------------------

FORM_INTENTS = frozenset(
    {
        "authentication",
        "search",
        "filter",
        "create",
        "edit",
        "delete_confirmation",
        "bulk_action",
        "upload",
        "settings",
        "unknown",
    }
)


class FormIntentAlternative(BaseModel):
    """One rejected-but-plausible intent, kept instead of silently dropped —
    the task's explicit "ambiguous alternatives" requirement."""

    intent: str
    confidence: float = 0.0
    reason: str = ""

    @field_validator("intent")
    @classmethod
    def _validate_intent(cls, value: str) -> str:
        if value not in FORM_INTENTS:
            raise ValueError(f"Invalid intent: {value!r} (expected one of {sorted(FORM_INTENTS)})")
        return value

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, value: float) -> float:
        return max(0.0, min(1.0, value))


class FormIntentClassification(BaseModel):
    """One form's classified purpose, with the evidence that produced it."""

    classification_id: str = Field(default_factory=new_id)
    form_id: str
    intent: str = "unknown"
    confidence: ConfidenceScore = Field(default_factory=ConfidenceScore)
    target_entity_hypothesis: Optional[str] = None
    operation_hypothesis: Optional[str] = None
    evidence: list[str] = Field(default_factory=list)
    alternatives: list[FormIntentAlternative] = Field(default_factory=list)

    @field_validator("intent")
    @classmethod
    def _validate_intent(cls, value: str) -> str:
        if value not in FORM_INTENTS:
            raise ValueError(f"Invalid intent: {value!r} (expected one of {sorted(FORM_INTENTS)})")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> object:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return ConfidenceScore(value=float(value))
        return value


# ---------------------------------------------------------------------------
# Action semantics — the verb a control performs, independent of whether it
# carries visible text (icon-only/kebab/FAB controls included).
# ---------------------------------------------------------------------------

ACTION_VERBS = frozenset(
    {
        "add", "create", "new", "save", "submit", "update", "edit", "delete",
        "remove", "confirm", "cancel", "search", "filter", "reset", "view",
        "open", "next", "previous", "more_actions", "unknown",
    }
)

ACTION_SIGNAL_KINDS = frozenset(
    {
        "visible_text", "aria_label", "title_attribute", "tooltip",
        "icon_class", "svg_icon", "kebab_menu", "floating_action_button",
        "row_action_context", "contextual_menu",
    }
)


class ActionSemantics(BaseModel):
    """One interactive element's classified action verb plus which signal(s)
    produced it — an icon-only trash-can button with `aria-label="Delete"`
    and no visible text still resolves to `semantic_action="delete"`."""

    element_id: str
    semantic_action: str = "unknown"
    confidence: ConfidenceScore = Field(default_factory=ConfidenceScore)
    signals: list[str] = Field(default_factory=list)
    is_icon_only: bool = False
    is_kebab_menu: bool = False
    is_floating_action_button: bool = False

    @field_validator("semantic_action")
    @classmethod
    def _validate_action(cls, value: str) -> str:
        if value not in ACTION_VERBS:
            raise ValueError(f"Invalid semantic_action: {value!r} (expected one of {sorted(ACTION_VERBS)})")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> object:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return ConfidenceScore(value=float(value))
        return value

    @field_validator("signals")
    @classmethod
    def _validate_signals(cls, value: list[str]) -> list[str]:
        for s in value:
            if s not in ACTION_SIGNAL_KINDS:
                raise ValueError(f"Invalid action signal kind: {s!r} (expected one of {sorted(ACTION_SIGNAL_KINDS)})")
        return value


# ---------------------------------------------------------------------------
# Page-state fingerprint, made explicit
# ---------------------------------------------------------------------------


class PageStateDescriptor(BaseModel):
    """Everything that feeds the page-state fingerprint, made explicit and
    inspectable rather than an opaque hash alone."""

    fingerprint: str
    contributing_fields: list[str] = Field(default_factory=list)
    computed_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# The Canonical Page Model
# ---------------------------------------------------------------------------


class CanonicalPageModel(BaseModel):
    """Application-neutral representation of everything observed on the current
    page. Superset view over `PageState` — see `from_page_state()` for the
    deterministic, hypothesis-free adapter that builds one from the other."""

    page_id: str = Field(default_factory=new_id)
    url: str
    domain: str = ""
    title: str = ""
    state_fingerprint: str = ""
    page_state: Optional[PageStateDescriptor] = None

    regions: list[PageRegion] = Field(default_factory=list)
    navigation_regions: list[NavigationRegion] = Field(default_factory=list)
    headings: list[HeadingDescriptor] = Field(default_factory=list)
    text_blocks: list[TextBlockDescriptor] = Field(default_factory=list)
    breadcrumbs: list[BreadcrumbDescriptor] = Field(default_factory=list)
    pagination: list[PaginationDescriptor] = Field(default_factory=list)
    links: list[LinkDescriptor] = Field(default_factory=list)
    tabs: list[TabGroup] = Field(default_factory=list)
    forms: list[FormDescriptor] = Field(default_factory=list)
    tables: list[TableDescriptor] = Field(default_factory=list)
    # Universal collection view — superset of `tables`: covers native
    # <table>, ARIA grid/treegrid, and div/card-based repeated-record
    # groups under one shape. `tables` is kept as-is for backward
    # compatibility; `collections` is the CRUD-surface-discovery-aware
    # answer to "what record sets exist on this page".
    collections: list["RecordCollection"] = Field(default_factory=list)
    form_intents: list["FormIntentClassification"] = Field(default_factory=list)
    action_semantics: list["ActionSemantics"] = Field(default_factory=list)
    images: list[ImageDescriptor] = Field(default_factory=list)
    dialogs: list[DialogDescriptor] = Field(default_factory=list)
    alerts: list[AlertDescriptor] = Field(default_factory=list)
    visual_regions: list[VisualRegion] = Field(default_factory=list)
    interactive_elements: list[InteractiveElement] = Field(default_factory=list)
    unknown_components: list[UnknownComponent] = Field(default_factory=list)
    network_evidence: list[NetworkEvidence] = Field(default_factory=list)
    # Additive visual-model output, attached by element_id — always empty
    # unless VisualObservationPolicy decided visual analysis was warranted
    # (see docs/VISUAL_OBSERVATION_POLICY.md). Never required for basic
    # extraction to be considered complete.
    visual_evidence: list[VisualEvidence] = Field(default_factory=list)

    screenshot_reference: Optional[EvidenceReference] = None
    observed_at: datetime = Field(default_factory=datetime.utcnow)

    # -- validation ---------------------------------------------------------

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("url must not be empty")
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"url must be absolute (scheme + host), got {value!r}")
        return value

    @model_validator(mode="after")
    def _derive_domain(self) -> "CanonicalPageModel":
        if not self.domain:
            self.domain = urlparse(self.url).hostname or ""
        return self

    @model_validator(mode="after")
    def _validate_unique_stable_ids(self) -> "CanonicalPageModel":
        """No two objects WITHIN THE SAME collection may share a stable_id (that
        would be an accidental duplicate — e.g. two distinct HeadingDescriptor
        objects both claiming "h1"). Uniqueness is intentionally NOT enforced
        ACROSS different collections: the same physical DOM element can
        legitimately appear in more than one semantic view at once — e.g. a
        clickable `<h4><a>...</a></h4>` is both a HeadingDescriptor and a
        LinkDescriptor, and a `role="tab"` element is both an InteractiveElement
        and a TabDescriptor. Rejecting that would be rejecting reality, not a
        real bug."""
        for collection in (
            self.regions,
            self.navigation_regions,
            self.headings,
            self.text_blocks,
            self.breadcrumbs,
            self.pagination,
            self.links,
            self.tabs,
            self.images,
            self.dialogs,
            self.alerts,
            self.visual_regions,
            self.unknown_components,
            self.network_evidence,
        ):
            seen: set[str] = set()
            for item in collection:
                sid = getattr(item, "stable_id", None)
                if sid is None:
                    continue
                if sid in seen:
                    raise ValueError(
                        f"Duplicate stable_id within {type(item).__name__} collection: {sid!r}"
                    )
                seen.add(sid)
        return self

    # -- serialization --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict (datetimes as ISO strings) — the primary serialization
        entry point for reports/APIs/evidence."""
        return self.model_dump(mode="json")

    def to_json(self, *, indent: int | None = None) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CanonicalPageModel":
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, data: str) -> "CanonicalPageModel":
        return cls.model_validate_json(data)

    # -- deterministic adapter from the existing PageState ---------------------

    @classmethod
    def from_page_state(cls, page_state: PageState) -> "CanonicalPageModel":
        """Build a CanonicalPageModel from an already-observed `PageState`.

        Purely mechanical: every field below is a direct, deterministic mapping
        from data `PageObserver` already extracted — no LLM call, no hypothesis,
        no guessing. Where the current observer doesn't yet extract something
        this model has room for (images, DOM landmarks/regions, unknown
        components, per-field ARIA expanded/selected state, tab selection), the
        corresponding collection is simply empty; that gap is the observer's,
        not this model's, and is documented in docs/CANONICAL_PAGE_MODEL.md.
        """
        domain = urlparse(page_state.url).hostname or ""

        headings = [
            HeadingDescriptor(text=h, level=1, dom_tag="heading", source="dom")
            for h in page_state.headings
        ]

        text_blocks = []
        if page_state.visible_text_summary:
            text_blocks.append(
                TextBlockDescriptor(
                    text=page_state.visible_text_summary,
                    block_type="summary",
                    source="dom",
                )
            )

        breadcrumbs = [
            BreadcrumbDescriptor(text=b, ordinal=i, source="dom")
            for i, b in enumerate(page_state.breadcrumbs)
        ]

        pagination = []
        if page_state.pagination_controls:
            pagination.append(
                PaginationDescriptor(
                    text=", ".join(page_state.pagination_controls),
                    source="dom",
                )
            )

        links = []
        for el in page_state.interactive_elements:
            if el.category != "link" and el.tag != "a":
                continue
            is_external = None
            if el.href:
                # Resolve relative hrefs against the page URL first — see the
                # identical fix in perception/dom_extractor.py.
                is_external = not same_origin(urljoin(page_state.url, el.href), origin_of(page_state.url))
            links.append(
                LinkDescriptor(
                    element_id=el.element_id,
                    text=el.visible_text or el.text,
                    accessible_name=el.accessible_name,
                    dom_tag=el.tag,
                    aria_role=el.role,
                    url=el.href,
                    target_url=el.href,
                    is_external=is_external,
                    is_external_url=is_external,
                    is_visible=el.is_visible,
                    is_enabled=el.is_enabled,
                    bounding_box=el.bounding_box,
                    source="dom",
                )
            )

        navigation_regions = []
        if page_state.navigation_items:
            nav_items = [
                NavigationItem(text=label, source="dom") for label in page_state.navigation_items
            ]
            navigation_regions.append(
                NavigationRegion(items=nav_items, landmark_role="nav", source="dom")
            )

        tabs = []
        if page_state.tabs:
            tab_descriptors = [TabDescriptor(text=label, source="dom") for label in page_state.tabs]
            tabs.append(TabGroup(tabs=tab_descriptors, source="dom"))

        dialogs = [
            DialogDescriptor(text=d, dialog_type="dialog", source="dom") for d in page_state.dialogs
        ] + [
            DialogDescriptor(text=m, dialog_type="modal", source="dom") for m in page_state.modals
        ]

        alerts = [
            AlertDescriptor(text=a, severity="error", source="dom") for a in page_state.alerts
        ] + [
            AlertDescriptor(text=t, severity="info", source="dom") for t in page_state.toasts
        ]

        network_evidence = [
            cls._network_entry_to_evidence(entry) for entry in page_state.network_entries
        ]

        screenshot_reference = None
        if page_state.screenshot_path:
            screenshot_reference = EvidenceReference(
                kind="screenshot",
                reference=page_state.screenshot_path,
            )

        page_state_descriptor = None
        if page_state.state_fingerprint:
            page_state_descriptor = PageStateDescriptor(
                fingerprint=page_state.state_fingerprint,
                # Matches app.browser.fingerprint.compute_fingerprint's actual
                # inputs exactly — see docs/PAGE_PERCEPTION_AUDIT.md item 11's
                # fingerprint discussion.
                contributing_fields=["url", "title", "headings", "controls", "modals", "dialogs", "text"],
                computed_at=page_state.captured_at,
            )

        return cls(
            page_id=page_state.page_id,
            url=page_state.url,
            domain=domain,
            title=page_state.title,
            state_fingerprint=page_state.state_fingerprint or "",
            page_state=page_state_descriptor,
            navigation_regions=navigation_regions,
            headings=headings,
            text_blocks=text_blocks,
            breadcrumbs=breadcrumbs,
            pagination=pagination,
            links=links,
            tabs=tabs,
            forms=list(page_state.forms),
            tables=list(page_state.tables),
            dialogs=dialogs,
            alerts=alerts,
            interactive_elements=list(page_state.interactive_elements),
            network_evidence=network_evidence,
            screenshot_reference=screenshot_reference,
            observed_at=page_state.captured_at,
        )

    @staticmethod
    def _network_entry_to_evidence(entry: NetworkEntry) -> NetworkEvidence:
        return NetworkEvidence(
            method=entry.method,
            http_status=entry.status,
            resource_type=entry.resource_type,
            failed=entry.failed,
            failure_text=entry.failure_text,
            timing_ms=entry.timing_ms,
            source="network",
            text=f"{entry.method} {entry.url}",
        )
