"""Exploration frontier — the single canonical source of exploration candidates.

Every possible next action (authentication, safe data-creation, links, buttons, tabs,
pagination, candidate URLs, form inspection, workflow continuation) is generated here.
Planner/fallback logic score and select among these candidates; they must not derive an
independent competing set of candidates from raw page elements."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

from app.agent.auth_strategy import AuthFormInfo, AuthenticationStrategy
from app.agent.form_lifecycle import FormLifecycle
from app.agent.form_identity import form_signature
from app.agent.form_workflow import infer_form_purpose
from app.agent.memory import action_signature
from app.agent.temporary_record_registry import identity_matches_cells
from app.application.url_normalize import same_origin
from app.perception.image_extractor import MEANINGFUL_IMAGE_TYPES
from app.schemas import ActionCategory, InteractiveElement, PageState

if TYPE_CHECKING:
    from app.agent.memory import RunMemory
    from app.perception.models import CanonicalPageModel

NAV_HINTS = ("nav", "menu", "sidebar", "header", "cancel", "back", "home")
MODULE_HINTS = (
    "module",
    "section",
    "customers",
    "orders",
    "products",
    "users",
    "projects",
    "contact",
    "contacts",
    "signup",
    "sign up",
    "register",
    "logout",
    "add contact",
    "add user",
    "login",
    "sign in",
)
SETTINGS_HINTS = ("settings", "preferences", "account", "profile")
FILTER_HINTS = ("filter", "facet", "refine")
PAGINATION_HINTS = ("next", "prev", "previous", "page", "pagination")
NAV_BUTTON_HINTS = (
    "sign up",
    "signup",
    "register",
    "login",
    "sign in",
    "home",
    "add contact",
    "add ",
    "new ",
    # Generic multi-step-flow progression vocabulary (checkout/wizard/onboarding
    # flows across many kinds of applications, not one specific target) — without
    # this, a flow-progression button defaults to the same generic priority as
    # incidental UI chrome and can lose ties to it, stalling the flow.
    "checkout",
    "continue",
    "proceed",
    "finish",
    "place order",
    "complete order",
)
# Cancel/back-style controls abandon a flow rather than advance it. They must rank
# below progress controls (NAV_BUTTON_HINTS) even when both appear on the same page
# (e.g. a checkout "Cancel" next to "Finish") — sharing one priority tier and losing
# a tie-break to alphabetically-earlier candidate ids would let the run discard an
# in-progress, budget-costly flow instead of completing it. Still ranked well above
# generic UI chrome so a genuine dead end can still be escaped via Back/Cancel.
NAV_BUTTON_DEFER_HINTS = ("cancel", "back")
# registration=3-4, open_registration=5, inspect_form=20, inspect_table=21,
# safe_test_data_create=25, open_url=30) occupy priorities 1-30. General navigation
# candidates are floored above that band so they can never accidentally outrank an
# available login/registration/safe-write candidate just by having a real href.
NAV_PRIORITY_FLOOR = 40

# How many rows per collection may be clicked on an UNPROVEN openability
# hypothesis (see CollectionRow.activation_basis). Records this run created are
# exempt — those are always worth opening, because opening one both verifies the
# create and is the only route to that record's update/delete controls.
MAX_HYPOTHESIS_ROWS_PER_COLLECTION = 2

# Region types that count as "navigation" for expand_navigation_region vs.
# generic expand_accordion classification. Includes the legacy "nav" value
# (from_page_state's adapter / older PageRegion data) alongside the richer
# Perception Engine region types.
_NAV_REGION_TYPES = frozenset(
    {"primary_navigation", "secondary_navigation", "content_navigation", "sidebar", "nav"}
)

# Maps a CanonicalPageModel-sourced candidate_type to the underlying browser
# verb it will be dispatched as (Planner._candidate_to_action). candidate_type
# is the rich, application-neutral semantic label used for scoring/tracing;
# the dispatched action itself always stays within the existing, already-
# validated ActionType vocabulary — nothing here invents a new browser
# capability.
CANDIDATE_ACTION_HINT: dict[str, str] = {
    "select_tab": "open_tab",
    "open_dropdown": "click",
    "open_context_menu": "click",
    "expand_navigation_region": "click",
    "expand_accordion": "click",
    "inspect_card": "click",
    "open_table_row": "click",
    "verify_internal_link": "click",
    "verify_external_link": "hover",
    "open_navigation_item": "click",
    "open_dialog": "click",
    "close_dialog": "press",
    "open_record_row": "click",
    "inspect_image": "take_screenshot",
    "inspect_document": "take_screenshot",
    "inspect_unknown_component": "hover",
}


def _has_updatable_owned_record(memory: "RunMemory") -> bool:
    """Does this run own a record that exists and has not been updated yet?

    `verified` is exactly that state in the Temporary Record Registry's
    lifecycle: the record was created AND independently confirmed to exist, and
    `mark_updated` has not moved it on. Reading the registry rather than
    tracking form ids also avoids a real trap — a create form and the edit form
    for the record it created can carry the SAME per-observation form id.
    """
    registry = getattr(memory, "temporary_record_registry", None)
    if registry is None:
        return False
    try:
        return any(entry.current_state == "verified" for entry in registry.entries.values())
    except Exception:  # pragma: no cover - defensive
        return False


def _region_type_lookup(canonical_model: "CanonicalPageModel") -> dict[str, str]:
    """dom_id -> region_type, keyed by both stable_id and element_id (regions
    from `classify_landmark_regions` set both to the same landmark dom_id)."""
    lookup: dict[str, str] = {}
    for region in canonical_model.regions or []:
        if region.stable_id:
            lookup[region.stable_id] = region.region_type
        if region.element_id:
            lookup[region.element_id] = region.region_type
    return lookup


def _nav_item_element_ids(canonical_model: "CanonicalPageModel") -> set[str]:
    return {
        item.element_id
        for region in (canonical_model.navigation_regions or [])
        for item in (region.items or [])
        if item.element_id
    }


def _tab_element_ids(canonical_model: "CanonicalPageModel") -> set[str]:
    return {
        tab.element_id
        for group in (canonical_model.tabs or [])
        for tab in (group.tabs or [])
        if tab.element_id
    }


def _link_locality_ids(canonical_model: "CanonicalPageModel") -> tuple[set[str], set[str]]:
    """(external_element_ids, internal_element_ids) from CanonicalPageModel.links."""
    external: set[str] = set()
    internal: set[str] = set()
    for link in canonical_model.links or []:
        if not link.element_id:
            continue
        is_external = link.is_external if link.is_external is not None else link.is_external_url
        if is_external:
            external.add(link.element_id)
        elif is_external is False:
            internal.add(link.element_id)
    return external, internal


def _classify_canonical_candidate(
    el: InteractiveElement,
    *,
    region_types: dict[str, str],
    nav_item_ids: set[str],
    tab_ids: set[str],
    external_link_ids: set[str],
    internal_link_ids: set[str],
) -> tuple[str, str, list[str]]:
    """Deterministic (candidate_type, semantic_type, evidence) for ONE
    CanonicalPageModel interactive element. Every branch is a rule over
    evidence already attached to the element/model by the Perception Engine —
    never a guess about business meaning. Returns ("", "", []) when nothing
    more specific applies than the existing generic navigation/button scoring,
    which is left completely unchanged in that case."""
    evidence = list(el.evidence or [])
    haspopup = (el.attributes or {}).get("aria-haspopup", "").strip().lower()
    region_type = region_types.get(el.parent_region_id or "", "")

    if el.element_id in tab_ids:
        return "select_tab", "tab", [*evidence, "tab_group_member"]
    if haspopup == "menu":
        return "open_context_menu", "context_menu", [*evidence, "aria-haspopup=menu"]
    if haspopup == "dialog":
        return "open_dialog", "dialog", [*evidence, "aria-haspopup=dialog"]
    if haspopup in {"true", "listbox", "tree", "grid"}:
        return "open_dropdown", haspopup, [*evidence, f"aria-haspopup={haspopup}"]
    if "expandable_control" in evidence:
        if region_type in _NAV_REGION_TYPES:
            return "expand_navigation_region", region_type, [*evidence, f"parent_region={region_type}"]
        return "expand_accordion", "accordion", evidence
    if "navigable_card" in evidence:
        return "inspect_card", "card", evidence
    if "table_row_action" in evidence:
        return "open_table_row", "table_row", evidence
    if el.element_id in external_link_ids:
        return "verify_external_link", "external_link", [*evidence, "external_link"]
    if el.element_id in nav_item_ids:
        return "open_navigation_item", region_type or "navigation", [*evidence, "navigation_region_item"]
    if el.element_id in internal_link_ids:
        return "verify_internal_link", "internal_link", [*evidence, "internal_link"]
    return "", "", evidence


def _canonical_score_hints(candidate_type: str, region_type: str) -> tuple[float, float, float]:
    """(navigation_centrality, workflow_value, external_navigation_cost) tiered
    defaults per candidate_type/region — coarse, deterministic hints, not a
    second scoring system: Planner still uses `priority` as the authoritative
    order: these are additional descriptive scores carried on the candidate."""
    external_cost = 0.8 if candidate_type == "verify_external_link" else 0.0

    nav_centrality = 0.0
    # An external destination never earns a nav-centrality bonus, regardless
    # of which DOM region it physically sits in — live-verified bug: SauceDemo's
    # hamburger-menu drawer is a real <nav> element containing an "About" link
    # to saucelabs.com (a different origin). Its region_type is legitimately
    # "primary_navigation", so without this guard it got both the full +0.9
    # nav-centrality bonus AND the external penalty, which nearly canceled
    # each other out (a link that LEAVES the app scored almost as high as one
    # that stays in it, purely because of where it happened to sit in the DOM).
    if external_cost == 0.0:
        if region_type == "primary_navigation":
            nav_centrality = 0.9
        elif region_type in _NAV_REGION_TYPES:
            nav_centrality = 0.6
        elif candidate_type in {"open_navigation_item", "expand_navigation_region"}:
            nav_centrality = 0.6

    workflow_value = (
        0.8
        if candidate_type in {"start_form_workflow", "continue_form_workflow", "inspect_form", "open_table_row"}
        else 0.0
    )
    return nav_centrality, workflow_value, external_cost


def _confidence_value(confidence: object, default: float = 0.6) -> float:
    """`ConfidenceScore` (perception-native descriptors) or a bare float —
    handles both, mirroring app.perception.evidence_merger's polymorphism."""
    if hasattr(confidence, "value"):
        return float(confidence.value)  # type: ignore[attr-defined]
    if isinstance(confidence, (int, float)):
        return float(confidence)
    return default


def _evidence_strings(items: object) -> list[str]:
    """`EvidenceReference` objects (perception-native descriptors) or plain
    strings -> list[str], for FrontierCandidate.evidence."""
    out: list[str] = []
    for item in items or []:  # type: ignore[union-attr]
        if isinstance(item, str):
            out.append(item)
        else:
            out.append(getattr(item, "description", None) or getattr(item, "kind", None) or str(item))
    return out


def _word_hint_match(text: str, hints: tuple[str, ...]) -> bool:
    """Whole-word hint match — plain substring checks let short hints like "back"
    false-positive inside unrelated words (e.g. a "Sauce Labs Backpack" product link
    was mistaken for a "Back" navigation control, giving it undeserved priority)."""
    return any(re.search(rf"\b{re.escape(h)}\b", text) for h in hints)


def _url_path_group(url: str) -> str:
    """The first non-empty URL path segment — a coarse proxy for "top-level area"
    (e.g. "customers" for both /customers and /customers/cust-002) used only to
    prefer diversity across areas when scoring open_url candidates, never for
    routing/dispatch decisions."""
    try:
        path = urlparse(url).path.strip("/")
    except Exception:
        return url
    return path.split("/", 1)[0] if path else ""


def infer_semantic_operation(el: InteractiveElement, text: str) -> str:
    """A normalized, app-vocabulary-independent operation tag for a candidate — never
    a business-domain word lifted from one specific target application."""
    tag = (el.tag or "").lower()
    cat = (el.category or "").lower()
    if cat == "tab" or (el.role or "") == "tab":
        return "open_tab"
    if any(h in text for h in PAGINATION_HINTS):
        return "paginate"
    if any(h in text for h in FILTER_HINTS):
        return "filter"
    if any(k in text for k in ("add ", "create ", "new ")):
        return "create_record"
    if any(k in text for k in ("remove", "delete")):
        return "remove_record"
    if _word_hint_match(text, ("cancel", "back")):
        return "navigate_back"
    if any(k in text for k in ("logout", "log out", "sign out")):
        return "logout"
    # Generic (non-app-specific) menu/drawer toggle detection. These controls are
    # frequently icon-only with no accessible text, so also check id/title/selector
    # hints rather than relying solely on the shared visible-text blob.
    extra = " ".join(filter(None, [el.id_attr, el.title, el.selector_hint])).lower()
    if any(h in text or h in extra for h in ("menu", "burger", "hamburger", "expand", "collapse", "toggle")):
        return "expand_region"
    if cat == "link" or tag == "a":
        return "navigate"
    if tag == "button" or (el.role or "") == "button":
        return "activate_control"
    return "interact"


def element_display_label(
    page: PageState, element_id: str | None, *, fallback: str = "Interact with control"
) -> str:
    """Derive a human-readable label for an element from what it actually says.

    Never hardcode an app-specific label (e.g. "Add contact") for a generic candidate —
    that leaks one demo app's vocabulary into every other application's action log and
    can corrupt downstream domain inference (a real bug: this exact hardcoded label,
    applied to SauceDemo's "Add to cart" and ServiceFlow's "Create Customer" alike,
    fed the "contacts" keyword into purpose inference for unrelated apps).
    """
    if not element_id:
        return fallback
    el = next(
        (e for e in (page.interactive_elements or []) if e.element_id == element_id),
        None,
    )
    if el is None:
        return fallback
    label = (
        el.accessible_name
        or el.visible_text
        or el.text
        or el.aria_label
        or el.label
        or el.name
        or el.placeholder
    )
    return str(label).strip() if label and str(label).strip() else fallback


CANDIDATE_STATUSES = frozenset(
    {
        "available",
        "selected",
        "attempted",
        "succeeded",
        "failed",
        "blocked",
        "deferred",
        "exhausted",
    }
)


def pending_cleanup_blocks_logout(memory: Any) -> bool:
    """True when Logout would drop the session that still owns test records.

    Cleanup runs after the live loop, against the current page. A Contact List
    run that clicked Logout first left delete discovered (1) and executed (0):
    the Delete Contact control was gone, and the temporary record stayed pending.
    """
    if memory is None:
        return False
    registry = getattr(memory, "temporary_record_registry", None)
    if registry is None:
        return False
    try:
        pending = registry.records_pending_cleanup()
    except Exception:
        return False
    if pending:
        return True
    # Delete click succeeded but absence is not confirmed yet (state `deleted`).
    # Logging out here is what sent run b6509a34 back to the login form.
    try:
        return any(e.current_state == "deleted" for e in registry.entries.values())
    except Exception:
        return False


def this_run_owns_temporary_records(memory: Any) -> bool:
    """True when this run created test records — Logout only lands on login.

    After a successful Contact List delete (run b6509a34) the frontier treated
    Logout as the last remaining candidate, signed out, then clicked Submit on
    the empty login form. The session is no longer needed once cleanup is done;
    finish instead of logging in again.
    """
    if memory is None:
        return False
    registry = getattr(memory, "temporary_record_registry", None)
    if registry is None:
        return False
    try:
        return bool(registry.entries)
    except Exception:
        return False


def _looks_like_logout(*, text: str = "", url: str = "") -> bool:
    haystack = f"{text} {url}".lower()
    return any(h in haystack for h in ("logout", "log out", "sign out"))


@dataclass
class FrontierCandidate:
    candidate_id: str
    candidate_type: str
    action: str
    # --- legacy fields (kept for backward compatibility with existing call sites) ---
    goal: str = ""
    risk: str = "read_only"
    form_id: str | None = None
    workflow_id: str | None = None
    element_id: str | None = None
    url: str | None = None
    priority: int = 50
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    category: str = "exploration"
    # --- unified-frontier fields ---
    source_page_id: str | None = None
    source_state_fingerprint: str | None = None
    target_url: str | None = None
    actual_label: str = ""
    semantic_operation: str = ""
    module_id: str | None = None
    submodule_id: str | None = None
    goal_id: str | None = None
    expected_information_gain: float = 0.5
    estimated_business_value: float = 0.5
    novelty_score: float = 1.0
    coverage_gap_score: float = 0.0
    prerequisite_score: float = 1.0
    safety_class: str = "read_only"
    reversibility: str = "reversible"
    destructive_risk: bool = False
    already_attempted_count: int = 0
    previous_failure_reason: str | None = None
    confidence: float = 0.7
    generation_reason: str = ""
    rejection_reason: str | None = None
    status: str = "available"
    # --- CanonicalPageModel integration fields ---
    # Where this candidate came from in the Canonical Page Model, when it was
    # sourced from one — a structural region (nav/sidebar/dialog/form/table
    # region) and the specific element/row/dialog/image it's about. Both are
    # "where applicable"; a PageState-only fallback candidate leaves them None.
    source_region_id: str | None = None
    source_element_id: str | None = None
    # A finer-grained tag than candidate_type (e.g. the actual region_type,
    # aria-haspopup value, or image visual_semantic_type) — informative
    # context, not used for dispatch.
    semantic_type: str = ""
    evidence: list[str] = field(default_factory=list)
    # New task-mandated score names. Each is synced with its pre-existing
    # equivalent below (same single score under two names — never a second,
    # independently-computed ranking system):
    #   business_relevance   <-> estimated_business_value
    #   novelty               <-> novelty_score
    #   coverage_value        <-> coverage_gap_score
    #   state_fingerprint     <-> source_state_fingerprint
    business_relevance: float = 0.5
    novelty: float = 1.0
    coverage_value: float = 0.0
    state_fingerprint: str | None = None
    # Genuinely new scores with no prior equivalent.
    navigation_centrality: float = 0.0
    workflow_value: float = 0.0
    external_navigation_cost: float = 0.0
    repetition_penalty: float = 0.0
    prerequisites: list[str] = field(default_factory=list)
    # Autonomous-Investigation alignment (app.agent.planner._apply_investigation_alignment,
    # set from `context["investigation_hints"]` — see
    # app.intelligence.autonomous_investigation.semantic_step_executor._hints_for_step).
    # Zero/False for every candidate when no scenario is active — standard
    # (non-autonomous) exploration is completely unaffected.
    investigation_alignment: float = 0.0
    off_scenario: bool = False

    def __post_init__(self) -> None:
        # Keep the legacy/unified pairs in sync regardless of which name a caller used.
        if not self.safety_class or self.safety_class == "read_only":
            self.safety_class = self.risk
        if not self.risk or self.risk == "read_only":
            self.risk = self.safety_class
        if not self.generation_reason:
            self.generation_reason = self.reason
        if not self.reason:
            self.reason = self.generation_reason
        if self.target_url is None:
            self.target_url = self.url
        elif self.url is None:
            self.url = self.target_url
        self.destructive_risk = self.destructive_risk or self.safety_class in {
            "destructive",
            "financial",
        }
        if self.status not in CANDIDATE_STATUSES:
            self.status = "available"
        self.source_element_id = self.source_element_id or self.element_id
        # Sync new task-mandated names with their pre-existing equivalents —
        # whichever side a caller actually set (differs from the shared
        # default) wins; if both are still default, they stay in sync trivially.
        if self.business_relevance == 0.5 and self.estimated_business_value != 0.5:
            self.business_relevance = self.estimated_business_value
        elif self.estimated_business_value == 0.5 and self.business_relevance != 0.5:
            self.estimated_business_value = self.business_relevance
        if self.novelty == 1.0 and self.novelty_score != 1.0:
            self.novelty = self.novelty_score
        elif self.novelty_score == 1.0 and self.novelty != 1.0:
            self.novelty_score = self.novelty
        if self.coverage_value == 0.0 and self.coverage_gap_score != 0.0:
            self.coverage_value = self.coverage_gap_score
        elif self.coverage_gap_score == 0.0 and self.coverage_value != 0.0:
            self.coverage_gap_score = self.coverage_value
        if not self.state_fingerprint and self.source_state_fingerprint:
            self.state_fingerprint = self.source_state_fingerprint
        elif not self.source_state_fingerprint and self.state_fingerprint:
            self.source_state_fingerprint = self.state_fingerprint
        if not self.repetition_penalty and self.already_attempted_count:
            self.repetition_penalty = min(1.0, self.already_attempted_count * 0.5)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_type": self.candidate_type,
            "action": self.action,
            "goal": self.goal,
            "risk": self.risk,
            "form_id": self.form_id,
            "workflow_id": self.workflow_id,
            "element_id": self.element_id,
            "url": self.url,
            "priority": self.priority,
            "reason": self.reason,
            "metadata": self.metadata,
            "source_page_id": self.source_page_id,
            "source_state_fingerprint": self.source_state_fingerprint,
            "target_url": self.target_url,
            "actual_label": self.actual_label,
            "semantic_operation": self.semantic_operation,
            "module_id": self.module_id,
            "submodule_id": self.submodule_id,
            "goal_id": self.goal_id,
            "expected_information_gain": self.expected_information_gain,
            "estimated_business_value": self.estimated_business_value,
            "novelty_score": self.novelty_score,
            "coverage_gap_score": self.coverage_gap_score,
            "prerequisite_score": self.prerequisite_score,
            "safety_class": self.safety_class,
            "reversibility": self.reversibility,
            "destructive_risk": self.destructive_risk,
            "already_attempted_count": self.already_attempted_count,
            "previous_failure_reason": self.previous_failure_reason,
            "confidence": self.confidence,
            "generation_reason": self.generation_reason,
            "rejection_reason": self.rejection_reason,
            "status": self.status,
            "source_region_id": self.source_region_id,
            "source_element_id": self.source_element_id,
            "semantic_type": self.semantic_type,
            "evidence": self.evidence,
            "business_relevance": self.business_relevance,
            "novelty": self.novelty,
            "coverage_value": self.coverage_value,
            "state_fingerprint": self.state_fingerprint,
            "navigation_centrality": self.navigation_centrality,
            "workflow_value": self.workflow_value,
            "external_navigation_cost": self.external_navigation_cost,
            "repetition_penalty": self.repetition_penalty,
            "prerequisites": self.prerequisites,
            "investigation_alignment": self.investigation_alignment,
            "off_scenario": self.off_scenario,
        }


class FrontierBuilder:
    """Generate actionable exploration candidates beyond link discovery."""

    def __init__(self, auth: AuthenticationStrategy) -> None:
        self.auth = auth

    def build(
        self,
        page: PageState,
        *,
        unexplored_urls: list[str] | None = None,
        allow_login: bool = True,
        allow_registration: bool = True,
        allow_safe_test_data: bool = False,
        inspected_form_ids: set[str] | None = None,
        memory: "RunMemory | None" = None,
        include_navigation: bool | None = None,
    ) -> list[FrontierCandidate]:
        """Build the full candidate set for this page.

        When `memory` is supplied, general navigation candidates (links, buttons,
        tabs, pagination) are generated here too, using the same scoring rules that
        used to live only in Planner._rank_elements — this is the single point where
        every kind of next-action candidate is produced. `include_navigation` can
        force the behavior explicitly; it defaults to "on iff memory is given" so
        existing auth-only call sites (tests, _select_auth_candidate before Phase 2)
        keep their exact prior behavior when they don't pass memory.

        When `memory.canonical_page_model` is available AND matches this exact
        page/state (same url + state_fingerprint — a stale model from a previous
        iteration, e.g. because perception failed this round, is never trusted),
        it becomes the primary source for every NON-authentication candidate:
        navigation/tab/dropdown/accordion/card/table-row classification, plus
        image/document/dialog/unknown-component candidates that a PageState-only
        page can't produce at all. Authentication candidates above are always
        PageState/AuthenticationStrategy-driven regardless — this only affects
        the general-exploration candidates below.
        """
        candidates: list[FrontierCandidate] = []
        inspected = inspected_form_ids or set()
        page_kind = self.auth.classify_page(page)
        auth_forms = self.auth.detect_forms(page)
        creds = self.auth.vault.public_flags()
        want_navigation = include_navigation if include_navigation is not None else memory is not None
        canonical_model = self._resolve_canonical_model(page, memory)

        # Active auth workflow continuation is highest priority
        if self.auth.active_workflow and self.auth.active_workflow.next_action() is not None:
            # Don't consume step here — planner will call next_workflow_action
            wf = self.auth.active_workflow
            candidates.append(
                FrontierCandidate(
                    candidate_id=f"continue_{wf.workflow_id}",
                    candidate_type="continue_active_workflow",
                    action="submit_form" if wf.step_index >= len(wf.steps) - 1 else "complete_form",
                    workflow_id=wf.workflow_id,
                    form_id=wf.form_id,
                    goal="reach_authenticated_application",
                    risk="authentication_write",
                    priority=1,
                    reason="Continue active authentication workflow.",
                )
            )

        if not self.auth.authenticated:
            login_forms = [f for f in auth_forms if f.kind == "login"]
            reg_forms = [f for f in auth_forms if f.kind == "registration"]

            if allow_login and creds.get("credential_profile_available") and login_forms:
                form = login_forms[0]
                if self._form_actionable(form):
                    candidates.append(
                        FrontierCandidate(
                            candidate_id=f"auth_login_{form.form_id}",
                            candidate_type="authenticate_with_credentials",
                            action="complete_form",
                            form_id=form.form_id,
                            goal="reach_authenticated_application",
                            risk="authentication_write",
                            priority=2,
                            reason=(
                                "Credentials are available. Completing login is required "
                                "to explore authenticated application areas."
                            ),
                        )
                    )

            # Never auto-register a fake account when the caller already supplied real
            # credentials — landing on a signup page (e.g. via normal link exploration)
            # should not spend a real account's test budget creating a decoy one.
            has_real_supplied_creds = creds.get("credential_source") == "supplied"
            if allow_registration and reg_forms and not has_real_supplied_creds and (
                not creds.get("credential_profile_available")
                or page_kind == "registration"
                or not login_forms
            ):
                form = reg_forms[0]
                if self._form_actionable(form):
                    candidates.append(
                        FrontierCandidate(
                            candidate_id=f"auth_reg_{form.form_id}",
                            candidate_type="create_test_account",
                            action="complete_form",
                            form_id=form.form_id,
                            goal="reach_authenticated_application",
                            risk="authentication_write",
                            priority=3 if not creds.get("credential_profile_available") else 4,
                            reason=(
                                "Only anonymous authentication pages are accessible. "
                                "Creating a unique test account is necessary to discover "
                                "the authenticated application."
                            ),
                        )
                    )

            # Prefer navigating to registration when stuck on login without creds
            if (
                allow_registration
                and page_kind == "login"
                and not creds.get("credential_profile_available")
                and not reg_forms
            ):
                for el in page.interactive_elements or []:
                    text = " ".join(
                        filter(
                            None,
                            [el.accessible_name, el.visible_text, el.text, el.label],
                        )
                    ).lower()
                    if any(k in text for k in ("sign up", "signup", "register", "create account")):
                        candidates.append(
                            FrontierCandidate(
                                candidate_id=f"goto_reg_{el.element_id}",
                                candidate_type="open_registration",
                                action="click",
                                element_id=el.element_id,
                                goal="reach_authenticated_application",
                                risk="read_only",
                                priority=5,
                                reason="Open registration to create a test account.",
                            )
                        )
                        break

        # Structural form inspection (only if not yet inspected)
        for form in page.forms or []:
            life = self.auth.form_lifecycle.get(form.form_id, FormLifecycle.DISCOVERED)
            if form.form_id not in inspected and life == FormLifecycle.DISCOVERED:
                candidates.append(
                    FrontierCandidate(
                        candidate_id=f"inspect_{form.form_id}",
                        candidate_type="inspect_form",
                        action="inspect_form",
                        form_id=form.form_id,
                        goal="understand_form_structure",
                        risk="read_only",
                        priority=20,
                        reason="Inspect form structure (does not complete the form).",
                        semantic_operation="inspect",
                        # An un-inspected form's structure is entirely unknown
                        # until inspected — inherently high information gain —
                        # and structural inspection is core, business-relevant
                        # investigative work (PriorityEngine strategic order
                        # item 6), not incidental UI chrome.
                        expected_information_gain=0.85,
                        business_relevance=0.7,
                    )
                )

        # Structural table inspection — same "understand the structure, don't
        # complete anything" role as form inspection, kept as its own candidate
        # type so it goes through the same unified frontier rather than a
        # separate check bolted onto the planner.
        if memory is not None:
            for table in page.tables or []:
                sig = action_signature(
                    page_fingerprint=page.state_fingerprint,
                    action_type="inspect_table",
                    element_id=table.table_id,
                )
                if not memory.has_seen_signature(sig):
                    candidates.append(
                        FrontierCandidate(
                            candidate_id=f"inspect_table_{table.table_id}",
                            candidate_type="inspect_table",
                            action="inspect_table",
                            element_id=table.table_id,
                            goal="understand_table_structure",
                            risk="read_only",
                            priority=21,
                            reason="Inspect unexplored table.",
                            semantic_operation="inspect",
                            expected_information_gain=0.85,
                            business_relevance=0.7,
                        )
                    )

        # Safe entity creation after authentication
        if self.auth.authenticated:
            for el in page.interactive_elements or []:
                text = " ".join(
                    filter(
                        None,
                        [el.accessible_name, el.visible_text, el.text, el.label],
                    )
                ).lower()
                if any(k in text for k in ("add ", "create ", "new ")) and "delete" not in text:
                    if not allow_safe_test_data:
                        # The control is right there and the session can reach it;
                        # the ONLY thing stopping us is the operator's setting. Say
                        # so, rather than reporting "no candidates left" later.
                        if memory is not None:
                            memory.note_write_candidate_suppressed(
                                required_flag="allow_safe_test_data_creation",
                                capability="creating a record",
                                evidence=element_display_label(
                                    page, el.element_id, fallback=el.element_id or "creation control"
                                ),
                            )
                        break
                    # Every other generator in this file checks whether this
                    # exact action has already been taken; this one did not, so
                    # the same "Add" control was offered again after every single
                    # click. A live run clicked it 21 times in 70 actions, and
                    # because each click "made progress" the stall counter never
                    # rose and loop detection never engaged.
                    if memory is not None and el.element_id:
                        sig = action_signature(
                            page_fingerprint=page.state_fingerprint,
                            action_type="click",
                            element_id=el.element_id,
                        )
                        if memory.has_seen_signature(sig):
                            break
                    candidates.append(
                        FrontierCandidate(
                            candidate_id=f"safe_create_{el.element_id}",
                            candidate_type="safe_test_data_create",
                            action="click",
                            element_id=el.element_id,
                            goal="discover_entity_workflows",
                            risk="safe_test_data_write",
                            priority=25,
                            reason="Open safe create workflow for test data.",
                            actual_label=element_display_label(
                                page, el.element_id, fallback="Open creation form"
                            ),
                            semantic_operation="create_record",
                        )
                    )
                    break

        # Continue an in-progress generic safe-form-fill workflow (checkout info,
        # create test customer/contact, etc.) — independent of
        # AuthenticationStrategy.active_workflow, but the same shape: a single
        # highest-priority "keep going" candidate whenever one is in flight.
        active_wf = getattr(memory, "active_form_workflow", None) if memory is not None else None
        if active_wf is not None and active_wf.state not in {"verified", "failed", "blocked", "skipped"}:
            candidates.append(
                FrontierCandidate(
                    candidate_id=f"continue_form_{active_wf.workflow_id}",
                    candidate_type="continue_form_workflow",
                    action="continue_form_workflow",
                    workflow_id=active_wf.workflow_id,
                    form_id=active_wf.form_id,
                    goal="complete_safe_form_workflow",
                    risk="safe_test_data_write",
                    priority=6,
                    reason="Continue in-progress safe form workflow.",
                    semantic_operation="create_record",
                    metadata={"form_purpose": active_wf.purpose},
                )
            )

        # Start a generic safe-form-fill workflow for an eligible form on this page —
        # only when nothing is already in progress (one at a time, same discipline as
        # auth workflows) and never for a form AuthenticationStrategy already owns
        # (login/registration forms are its exclusive territory).
        if memory is not None and not allow_safe_test_data and active_wf is None and self.auth.authenticated:
            # Same reporting duty as the creation control above: a fillable
            # non-auth form the session can already see is work offered and
            # refused by policy, not work that does not exist.
            auth_only = {f.form_id for f in auth_forms}
            for form in page.forms or []:
                if form.form_id not in auth_only and (form.fields or []):
                    memory.note_write_candidate_suppressed(
                        required_flag="allow_safe_test_data_creation",
                        capability="filling and submitting a form",
                        evidence=form.form_id,
                    )
        if memory is not None and allow_safe_test_data and active_wf is None and self.auth.authenticated:
            auth_form_ids = {f.form_id for f in auth_forms}
            failed_counts = getattr(memory, "failed_form_workflow_counts", None) or {}
            for form in page.forms or []:
                if form.form_id in auth_form_ids:
                    continue
                # A form whose workflow has already failed/blocked once is not
                # retried — without this, a form that can never actually converge
                # (e.g. its real submit control still isn't resolved correctly)
                # produces an outer start -> fail -> restart loop forever, each
                # attempt individually bounded by GenericFormWorkflow's own step
                # budget but with no cap across attempts.
                if failed_counts.get(form.form_id, 0) >= 1:
                    continue
                # ...and neither is one that already SUCCEEDED. Two separate
                # holes met here, and either alone reopens the loop:
                #
                # 1. every guard above counts failures, so an application that
                #    kept accepting the same create had nothing stopping it;
                # 2. `form_id` is a document-order counter, so the accepted
                #    submit itself renumbered the form and reset guard 1 anyway.
                #
                # Live on OrangeHRM's Buzz newsfeed, where the post box sits
                # below the feed: 20 accepted posts in 44 actions, form_055 ->
                # form_056 -> form_085, until the operator cancelled the run.
                if memory.has_finished_form(form_signature(form, page.url)):
                    continue
                heading = page.headings[0] if page.headings else ""
                purpose = infer_form_purpose(form, page_url=page.url, heading=heading)
                if purpose == "unknown":
                    continue
                if purpose == "safe_test_data_update" and not _has_updatable_owned_record(memory):
                    # Two rules in one, both deliberate:
                    #
                    # 1. GemmaQA only updates records IT created. A prefilled form
                    #    belonging to pre-existing application data is left alone —
                    #    modifying someone else's record is not this run's business.
                    # 2. A record is updated ONCE. Observed live: after an accepted
                    #    update the edit form was still offered, so the run edited
                    #    the same record three times in six actions, each repeat
                    #    adding no coverage. The Temporary Record Registry's own
                    #    `verified` -> `updated` transition is the authority.
                    continue
                candidates.append(
                    FrontierCandidate(
                        candidate_id=f"start_form_{form.form_id}",
                        candidate_type="start_form_workflow",
                        action="start_form_workflow",
                        form_id=form.form_id,
                        goal="complete_safe_form_workflow",
                        risk="safe_test_data_write",
                        # An update to a record this run already created is
                        # preferred over yet another create: it completes a
                        # cycle already paid for instead of starting a new one.
                        priority=24 if purpose == "safe_test_data_update" else 26,
                        reason=f"Begin safe form workflow ({purpose}).",
                        semantic_operation=(
                            "update_record" if purpose == "safe_test_data_update" else "create_record"
                        ),
                        metadata={"form_purpose": purpose},
                    )
                )
                break

        # Prefer the first unexplored URL in an entirely NEW top-level area over yet
        # another URL in an area already being explored — otherwise, once an area
        # with many pages (e.g. a customer list with several detail pages) starts
        # generating new candidate URLs of its own, a uniform priority plus
        # arbitrary hash-based tie-break can starve out a genuinely different,
        # still-unvisited section (e.g. "Jobs") indefinitely by sheer bad luck.
        visited_path_groups = (
            {_url_path_group(u) for u in memory.visited_urls} if memory is not None else set()
        )
        seen_new_group_this_call: set[str] = set()

        for url in unexplored_urls or []:
            if this_run_owns_temporary_records(memory) and _looks_like_logout(url=url):
                continue
            # An open_url attempt can be blocked by policy (e.g. local-target
            # restrictions) without ever changing the page, so it would otherwise be
            # regenerated at this same priority forever, starving out a working
            # alternative (e.g. clicking the same destination as a real link) that
            # only outranks it once this candidate is marked exhausted. Once this
            # exact URL has been attempted on this page (success or failure), stop
            # offering it as available — mirrors the has_seen_signature guard
            # already applied to navigation_control candidates below.
            already_attempted = 0
            status = "available"
            if memory is not None:
                sig = action_signature(
                    page_fingerprint=page.state_fingerprint,
                    action_type="open_url",
                    element_id=None,
                    value_category=url,
                )
                already_attempted = memory.signature_counts.get(sig, 0)
                if memory.has_seen_signature(sig):
                    status = "exhausted"

            group = _url_path_group(url)
            priority = 30
            if group not in visited_path_groups and group not in seen_new_group_this_call:
                priority = 29
                seen_new_group_this_call.add(group)

            candidates.append(
                FrontierCandidate(
                    candidate_id=f"url_{abs(hash(url)) % 10_000_000}",
                    candidate_type="open_url",
                    action="open_url",
                    url=url,
                    goal="explore_unvisited_page",
                    risk="read_only",
                    priority=priority,
                    reason="Visit unexplored same-origin URL.",
                    actual_label=url,
                    semantic_operation="navigate",
                    novelty_score=0.0 if already_attempted else 1.0,
                    coverage_gap_score=1.0,
                    already_attempted_count=already_attempted,
                    status=status,
                )
            )

        if want_navigation and memory is not None:
            candidates.extend(
                self._build_navigation_candidates(
                    page, memory, authenticated=self.auth.authenticated, canonical_model=canonical_model
                )
            )

        if canonical_model is not None and memory is not None:
            # These three read the CANONICAL model but must sign their candidates
            # with the LEGACY page fingerprint, because that is the one
            # `RunMemory.remember_action` records. Signing with the canonical
            # fingerprint made every signature un-matchable, so
            # `has_seen_signature` never fired and the same candidate was offered
            # forever — observed live as ten consecutive screenshots of one image.
            fingerprint = page.state_fingerprint
            candidates.extend(self._build_record_row_candidates(canonical_model, memory, fingerprint))
            candidates.extend(self._build_image_candidates(canonical_model, memory, fingerprint))
            candidates.extend(self._build_dialog_close_candidates(canonical_model, memory, fingerprint))
            candidates.extend(self._build_unknown_component_candidates(canonical_model, memory, fingerprint))

        module_id: str | None = None
        if memory is not None and memory.app_store is not None:
            app_page = memory.app_store.model.page_by_url(page.url)
            module_id = app_page.module_id if app_page else None

        for cand in candidates:
            cand.source_page_id = cand.source_page_id or page.page_id
            cand.source_state_fingerprint = cand.source_state_fingerprint or page.state_fingerprint
            cand.state_fingerprint = cand.state_fingerprint or cand.source_state_fingerprint
            cand.module_id = cand.module_id or module_id

        candidates.sort(key=lambda c: (c.priority, c.candidate_id))
        return candidates

    @staticmethod
    def _resolve_canonical_model(
        page: PageState, memory: "RunMemory | None"
    ) -> "CanonicalPageModel | None":
        """`memory.canonical_page_model` is only trusted when it demonstrably
        describes THIS exact page/state — same url, AND the state_fingerprint
        that was current at the moment it was captured
        (`memory.canonical_page_model_source_fingerprint`, stamped in
        controller.py._run_perception_engine from the SAME observation cycle's
        PageState) still matches this page's current state_fingerprint.

        Deliberately NOT compared against `model.state_fingerprint` itself:
        CanonicalPageModel computes its own, richer fingerprint
        (app.perception.state_builder) from a different input set than
        PageObserver's `compute_fingerprint` — the two are never expected to
        be equal even for the exact same instant, so comparing them directly
        would reject every live model (verified live: SauceDemo-style pages
        never produce matching hashes across the two algorithms).

        The Perception Engine runs best-effort alongside `PageObserver`
        (app/agent/controller.py._run_perception_engine); on failure it leaves
        the PREVIOUS iteration's model in place rather than clearing it, so an
        unguarded read here could silently generate candidates for a page that
        is no longer the current one. Falling back to None (PageState-only
        generation) is always safe; trusting a stale model never is."""
        if memory is None:
            return None
        model = getattr(memory, "canonical_page_model", None)
        if model is None:
            return None
        if getattr(model, "url", None) != page.url:
            return None
        source_fingerprint = getattr(memory, "canonical_page_model_source_fingerprint", None)
        if source_fingerprint != page.state_fingerprint:
            return None
        return model

    @staticmethod
    def _build_navigation_candidates(
        page: PageState,
        memory: "RunMemory",
        *,
        authenticated: bool = False,
        canonical_model: "CanonicalPageModel | None" = None,
    ) -> list[FrontierCandidate]:
        """General navigation candidates — links, buttons, tabs, pagination.

        This is the logic that used to live only in Planner._rank_elements, generating
        its own competing candidate set. It now lives here so FrontierBuilder is the
        single source of every kind of next-action candidate; Planner still scores
        and picks among them but no longer discovers them independently.

        When `canonical_model` is given, it is the element SOURCE (the Perception
        Engine's richer interactive-element detection — pointer-style/navigable-card
        elements a plain PageState scan never sees) and each element is additionally
        reclassified into a more specific candidate_type (select_tab, open_dropdown,
        expand_navigation_region, verify_internal_link, ...) via
        `_classify_canonical_candidate`. Every existing filter/scoring rule below is
        otherwise unchanged and applies identically either way.
        """
        candidates: list[FrontierCandidate] = []
        seen_link_labels: set[str] = set()
        elements_source: list[InteractiveElement] = list(
            canonical_model.interactive_elements if canonical_model is not None else (page.interactive_elements or [])
        )
        region_types = _region_type_lookup(canonical_model) if canonical_model is not None else {}
        nav_item_ids = _nav_item_element_ids(canonical_model) if canonical_model is not None else set()
        tab_ids = _tab_element_ids(canonical_model) if canonical_model is not None else set()
        external_link_ids, internal_link_ids = (
            _link_locality_ids(canonical_model) if canonical_model is not None else (set(), set())
        )
        for el in elements_source:
            if not el.is_visible:
                continue
            if el.disabled and canonical_model is None:
                # Legacy PageState-only behavior: skip entirely (unchanged).
                continue
            if memory.is_href_blocked(el.href):
                continue
            if memory.element_failed_too_often(el.element_id):
                continue
            text = " ".join(
                filter(
                    None,
                    [
                        el.accessible_name,
                        el.visible_text,
                        el.text,
                        el.aria_label,
                        el.name,
                        el.href,
                        el.placeholder,
                    ],
                )
            ).lower()
            if any(bad in text for bad in ("delete", "pay", "purchase", "invite")):
                continue
            if memory is not None and any(k in text for k in ("edit ", "edit contact", "edit record")):
                if any("edit" in (u or "").lower() for u in memory.visited_urls):
                    continue
            if "password" in text or (el.input_type or "").lower() == "password":
                continue
            if (
                authenticated
                and any(k in text for k in ("add ", "create ", "new "))
                and "delete" not in text
            ):
                # Owned exclusively by the safe_test_data_create candidate type (gated
                # on allow_safe_test_data_creation) so it can never slip through as an
                # always-available generic navigation candidate and bypass that gate.
                continue

            tag = (el.tag or "").lower()
            cat = (el.category or "").lower()
            input_type = (el.input_type or "").lower()
            if cat in {"input", "textarea", "select"} or tag in {"input", "textarea", "select"}:
                if input_type not in {"button"} and tag != "button":
                    continue
            if input_type == "submit" or text.strip() in {
                "submit",
                "save",
                "create",
                "create account",
            }:
                continue
            if cat == "link" or tag == "a":
                # A page routinely has two separate links to the same destination —
                # e.g. an image wrapper and a title link around the same product card.
                # Without collapsing these, both get ranked as distinct "unexplored"
                # candidates, so exploration burns two turns on one product before ever
                # reaching a different one.
                label_key = (el.accessible_name or el.visible_text or el.text or "").strip().lower()
                if label_key:
                    if label_key in seen_link_labels:
                        continue
                    seen_link_labels.add(label_key)

            priority = 50
            reason = "Explore interactive control"
            category = ActionCategory.EXPLORATION
            is_logout = _looks_like_logout(text=text, url=el.href or "")
            if is_logout and this_run_owns_temporary_records(memory):
                continue

            if cat == "link" or tag == "a":
                href = (el.href or "").strip()
                href_is_placeholder = (not href) or href == "#" or href.lower().startswith("javascript:")
                href_is_external = False
                if not href_is_placeholder:
                    target = href if href.lower().startswith("http") else urljoin(page.url, href)
                    allowed_origin = memory.app_store.model.allowed_origin if memory.app_store else ""
                    if allowed_origin:
                        href_is_external = not same_origin(target, allowed_origin)
                if href_is_external:
                    # A real link to a different domain (social/footer/support) — a
                    # "real href" alone isn't a signal of in-app importance; treating
                    # it as such backwards-favors off-site clutter over same-origin
                    # content, which is what actually needs exploring.
                    priority = min(priority, 60)
                    reason = "External link (deprioritized)"
                elif _word_hint_match(text, NAV_HINTS) or not href_is_placeholder:
                    priority = 1
                    reason = "Main navigation / unvisited link"
                    category = ActionCategory.NAVIGATION_TEST
                else:
                    # In-scope link/anchor with a placeholder href (e.g. href="#" with
                    # a client-side-routed click handler — common for SPA content
                    # links such as a product title/card). Still genuine in-app
                    # navigation, so it must beat generic UI chrome (menu toggles,
                    # decorative buttons) on ties instead of falling to the same
                    # default priority as those.
                    priority = min(priority, 30)
                    reason = "In-app navigation control"
                if any(h in text for h in MODULE_HINTS):
                    priority = min(priority, 2)
                    reason = "Unvisited module navigation"
            if cat == "tab" or (el.role or "") == "tab":
                if (el.accessible_name or el.visible_text or "") not in memory.known_tabs:
                    priority = min(priority, 3)
                    reason = "Unopened tab"
            if page.dialogs or page.modals:
                if cat == "button" and any(k in text for k in ("open", "view", "details", "more")):
                    priority = min(priority, 4)
                    reason = "Potential dialog/modal opener"
            if any(h in text for h in FILTER_HINTS):
                priority = min(priority, 7)
                reason = "Filter control"
            if any(h in text for h in PAGINATION_HINTS):
                priority = min(priority, 9)
                reason = "Pagination control"
            if any(h in text for h in SETTINGS_HINTS):
                priority = min(priority, 10)
                reason = "Settings / account control"
            if tag == "button" or (el.role or "") == "button":
                if any(h in text for h in NAV_BUTTON_HINTS):
                    priority = min(priority, 2)
                    reason = "Navigation control"
                    category = ActionCategory.NAVIGATION_TEST
                elif _word_hint_match(text, NAV_BUTTON_DEFER_HINTS):
                    priority = min(priority, 8)
                    reason = "Navigation control (abandons current flow)"
                    category = ActionCategory.NAVIGATION_TEST
            # General navigation must never outrank authentication/structural
            # candidates (login completion, safe data-creation, form/table
            # inspection — priorities 1-30 in build()): a page can easily contain a
            # real, genuinely-hrefed "Sign up" link that would otherwise beat a
            # priority-2 login-completion candidate merely by having a real href.
            # Relative ordering among navigation candidates is unaffected — this is
            # a uniform floor, not a reshuffle.
            priority += NAV_PRIORITY_FLOOR
            # Logout is real and must be discoverable, but only ever as a last
            # resort — defer it far behind everything else rather than excluding
            # it outright (it must still show up once nothing else remains).
            if is_logout:
                priority = 1000

            label = element_display_label(page, el.element_id, fallback=reason)
            semantic_op = infer_semantic_operation(el, text)

            # Classify BEFORE computing the attempt signature — the signature
            # must use the action_type this candidate will actually be
            # DISPATCHED as (CANDIDATE_ACTION_HINT), never a bare "click".
            # Regression: select_tab dispatches as ActionType.OPEN_TAB and
            # verify_external_link as ActionType.HOVER (Planner._candidate_to_action);
            # RunMemory.record_action() records the real signature using
            # result.action.action.value (e.g. "open_tab"), so a hardcoded
            # "click" lookup here could NEVER match it — already_attempted_count
            # stayed permanently 0, novelty stayed permanently 1.0, and a page
            # with an active tablist got stuck re-selecting the same tab for
            # the rest of the action budget. Live-verified on a real
            # tabs+cards dashboard page before this fix.
            candidate_type = "navigation_control"
            semantic_type = ""
            cand_evidence: list[str] = []
            region_type = region_types.get(el.parent_region_id or "", "")
            if canonical_model is not None:
                classified_type, semantic_type, cand_evidence = _classify_canonical_candidate(
                    el,
                    region_types=region_types,
                    nav_item_ids=nav_item_ids,
                    tab_ids=tab_ids,
                    external_link_ids=external_link_ids,
                    internal_link_ids=internal_link_ids,
                )
                if classified_type:
                    candidate_type = classified_type

            # A persistent navigation item is the SAME action wherever it is
            # clicked from — "go to Admin" does not become a new opportunity just
            # because you are standing on a different page. Signing it by page
            # fingerprint meant its attempt count reset on every page, so the run
            # returned to the same nav item between almost every module: 12 clicks
            # on one sidebar link in 59 actions.
            #
            # Signed by destination instead, which is the identity a person uses.
            # Falls back to the element when there is no destination to sign by
            # (a button-driven SPA link), and `expand_navigation_region` is a
            # different candidate type, so menus can still be reopened.
            sig = action_signature(
                page_fingerprint=page.state_fingerprint,
                action_type=CANDIDATE_ACTION_HINT.get(candidate_type, "click"),
                element_id=el.element_id,
            )
            attempted = memory.signature_counts.get(sig, 0)
            # Judged by DESTINATION, not by attempt count, because the attempt
            # count is signed with the page fingerprint and a sidebar item appears
            # on every page — so its count reset each time and the run returned to
            # the same nav item between almost every module (12 clicks on one link
            # in 59 actions). `visited_urls` is already maintained and is the
            # honest authority: a link whose destination has been seen offers
            # nothing new, wherever it is clicked from.
            if candidate_type == "open_navigation_item":
                nav_target = (el.href or "").strip()
                # BOTH identities count, because either one on its own leaves a
                # gap. Destination alone misses a button-driven SPA route (no
                # href to sign by). Label alone misses a link whose text differs
                # between the sidebar and a breadcrumb. A nav item is exhausted
                # when EITHER says it has already been taken.
                if nav_target and memory.has_visited(nav_target):
                    attempted = max(attempted, 1)
                elif memory.has_taken_navigation(
                    element_display_label(page, el.element_id, fallback="")
                ):
                    # "Admin" is the same navigation item wherever it is clicked
                    # from, whether or not it carries an href. Applying this only
                    # in the no-href case left href-bearing sidebar items being
                    # re-offered on every page whose module they had not yet
                    # reached from.
                    attempted = max(attempted, 1)
            # Menu/drawer-style toggles are frequently the ONLY path to reach whatever
            # they reveal (e.g. a hamburger menu's own nav items) — revealing content
            # then closing it again via one of those items produces a DIFFERENT page
            # fingerprint than the original closed state, so a one-shot exhaustion rule
            # would permanently lock the run out of ever reopening it, even though its
            # children were never all explored. Allow a bounded number of retries
            # instead of a hard single-attempt exhaustion for this operation only.
            retry_limit = 2 if semantic_op == "expand_region" else 1

            status = "exhausted" if attempted >= retry_limit else "available"
            if el.disabled:
                # Invisible elements were already skipped above; a visible but
                # disabled element is never dispatched (Planner skips
                # status="blocked") but is still recorded — e.g. a disabled
                # submit button explains why a workflow is currently stuck,
                # which is useful evidence even though it isn't actionable.
                status = "blocked"

            nav_centrality, workflow_value, ext_cost = _canonical_score_hints(candidate_type, region_type)

            extra_fields: dict[str, Any] = {}
            if canonical_model is not None:
                extra_fields = {
                    "source_region_id": el.parent_region_id,
                    "source_element_id": el.element_id,
                    "semantic_type": semantic_type,
                    "evidence": cand_evidence,
                    "confidence": el.confidence if el.confidence else 0.7,
                    "navigation_centrality": nav_centrality,
                    "workflow_value": workflow_value,
                    "external_navigation_cost": ext_cost,
                }

            candidates.append(
                FrontierCandidate(
                    candidate_id=f"nav_{el.element_id}",
                    candidate_type=candidate_type,
                    action=CANDIDATE_ACTION_HINT.get(candidate_type, "click"),
                    element_id=el.element_id,
                    goal="explore_navigation_region",
                    risk="read_only",
                    priority=priority,
                    reason=reason,
                    actual_label=label,
                    semantic_operation=semantic_op,
                    novelty_score=0.0 if attempted else 1.0,
                    already_attempted_count=attempted,
                    status=status,
                    metadata={"action_label": label},
                    category=category.value,
                    **extra_fields,
                )
            )
        return candidates

    @staticmethod
    def _build_record_row_candidates(
        canonical_model: "CanonicalPageModel", memory: "RunMemory", page_fingerprint: str | None = None
    ) -> list[FrontierCandidate]:
        """Candidates for opening an individual record from a collection.

        Update and delete controls almost never live on a list — they live
        inside a record. Observed live on a record-management application: the
        list rendered one activatable row per record, and because nothing
        offered "open this row" the run could reach neither the record nor its
        edit/delete controls, leaving update and delete permanently
        undiscovered.

        A row GemmaQA itself created is ranked above the rest: it is the one
        record whose expected content is known, so it is the only row where
        opening it also VERIFIES the create — and it is safe to mutate later,
        because the Temporary Record Registry owns it.
        """
        candidates: list[FrontierCandidate] = []
        created_identities = FrontierBuilder._created_record_identities(memory)

        for collection in canonical_model.collections or []:
            collection_id = collection.element_id or collection.stable_id
            hypothesis_offered = 0
            for row in collection.visible_rows or []:
                if not row.element_id or not row.is_activatable:
                    continue
                matched_early = FrontierBuilder._matching_created_identity(row, created_identities)
                if row.activation_basis == "structural_hypothesis" and not matched_early:
                    # Unproven openability, and not a record this run created.
                    # Worth testing on a few rows — a hundred-row grid must not
                    # become a hundred speculative clicks.
                    if hypothesis_offered >= MAX_HYPOTHESIS_ROWS_PER_COLLECTION:
                        continue
                    hypothesis_offered += 1
                sig = action_signature(
                    page_fingerprint=page_fingerprint or canonical_model.state_fingerprint,
                    action_type="click",
                    element_id=row.element_id,
                )
                if memory.has_seen_signature(sig):
                    continue

                matched = matched_early
                proven = row.activation_basis == "observed"
                label = row.identity_hint or f"record row {row.row_index + 1}"
                candidates.append(
                    FrontierCandidate(
                        candidate_id=f"open_record_row_{row.element_id}",
                        candidate_type="open_record_row",
                        action="click",
                        element_id=row.element_id,
                        goal="reach_record_detail",
                        risk="read_only",
                        # Just past form/table inspection (20-25) when this is
                        # the record we created — reaching our own record is how
                        # update/delete become reachable at all. A row proven
                        # interactive outranks a merely presumed one, and both
                        # sit ahead of general navigation.
                        priority=26 if matched else (NAV_PRIORITY_FLOOR - 8 if proven else NAV_PRIORITY_FLOOR - 5),
                        reason=(
                            f"Open the record this run created ({matched}) to verify it and reach "
                            "its own update/delete controls."
                            if matched
                            else "Open a record to reach its detail state and any per-record controls."
                        ),
                        actual_label=str(label)[:80],
                        semantic_operation="read",
                        already_attempted_count=memory.signature_counts.get(sig, 0),
                        metadata={
                            "action_label": str(label)[:80],
                            "collection_element_id": collection_id,
                            "row_index": row.row_index,
                            "activation_evidence": list(row.activation_evidence),
                            "activation_basis": row.activation_basis,
                            "created_record_identity": matched or "",
                        },
                        category=ActionCategory.EXPLORATION.value,
                    )
                )
        return candidates

    @staticmethod
    def _created_record_identities(memory: "RunMemory") -> list[str]:
        """Identity values of records this run created, per the Temporary Record
        Registry — the single authority on what GemmaQA itself made."""
        registry = getattr(memory, "temporary_record_registry", None)
        if registry is None:
            return []
        try:
            return [
                str(entry.generated_identity)
                for entry in registry.entries.values()
                if entry.generated_identity
            ]
        except Exception:  # pragma: no cover - defensive
            return []

    @staticmethod
    def _matching_created_identity(row: Any, identities: list[str]) -> str | None:
        """Does any cell of this row carry a created record's identity value?

        Delegates to `identity_matches_cells` so the frontier and the cleanup
        planner answer this question identically — see that function.
        """
        cells = row.cell_values or []
        for identity in identities:
            if identity_matches_cells(identity, cells):
                return identity
        return None

    @staticmethod
    def _build_image_candidates(
        canonical_model: "CanonicalPageModel", memory: "RunMemory", page_fingerprint: str | None = None
    ) -> list[FrontierCandidate]:
        """Read-only evidence-capture candidates for images the deterministic
        heuristic classifier (image_extractor.classify_image_semantic) found
        "meaningful" — decorative/avatar/logo images never generate a
        candidate here; they already carry enough evidence on their own."""
        candidates: list[FrontierCandidate] = []
        for img in canonical_model.images or []:
            if not img.is_visible or not img.element_id:
                continue
            if img.visual_semantic_type not in MEANINGFUL_IMAGE_TYPES:
                continue
            candidate_type = "inspect_document" if img.visual_semantic_type == "document" else "inspect_image"
            action = CANDIDATE_ACTION_HINT[candidate_type]
            sig = action_signature(
                page_fingerprint=page_fingerprint or canonical_model.state_fingerprint,
                action_type=action,
                element_id=img.element_id,
            )
            if memory.has_seen_signature(sig):
                continue
            attempted = memory.signature_counts.get(sig, 0)
            label = img.accessible_name or img.alt_text or "Inspect image"
            candidates.append(
                FrontierCandidate(
                    candidate_id=f"{candidate_type}_{img.element_id}",
                    candidate_type=candidate_type,
                    action=action,
                    element_id=img.element_id,
                    goal="capture_visual_evidence",
                    risk="read_only",
                    priority=70,
                    reason=f"Meaningful image ({img.visual_semantic_type}) worth capturing as evidence.",
                    actual_label=str(label),
                    semantic_operation="inspect",
                    already_attempted_count=attempted,
                    status="available",
                    source_region_id=img.parent_region_id,
                    source_element_id=img.element_id,
                    semantic_type=img.visual_semantic_type,
                    evidence=_evidence_strings(img.evidence),
                    confidence=_confidence_value(img.confidence, default=0.6),
                    category=ActionCategory.EVIDENCE_CAPTURE.value,
                    metadata={"action_label": str(label)},
                )
            )
        return candidates

    @staticmethod
    def _build_dialog_close_candidates(
        canonical_model: "CanonicalPageModel", memory: "RunMemory", page_fingerprint: str | None = None
    ) -> list[FrontierCandidate]:
        """A currently-open dialog/modal usually blocks the rest of the page —
        offer a safe, standard Escape-key close rather than guessing which
        contained control is the "real" close button (many are icon-only with
        no text at all)."""
        candidates: list[FrontierCandidate] = []
        for dialog in canonical_model.dialogs or []:
            if not dialog.is_open or not dialog.element_id:
                continue
            sig = action_signature(
                page_fingerprint=page_fingerprint or canonical_model.state_fingerprint,
                action_type="press",
                element_id=dialog.element_id,
                value_category="Escape",
            )
            if memory.has_seen_signature(sig):
                continue
            attempted = memory.signature_counts.get(sig, 0)
            label = dialog.text or "Close dialog"
            candidates.append(
                FrontierCandidate(
                    candidate_id=f"close_dialog_{dialog.element_id}",
                    candidate_type="close_dialog",
                    action="press",
                    element_id=dialog.element_id,
                    goal="close_open_overlay",
                    risk="read_only",
                    priority=15,
                    reason="An open dialog/modal may block other controls; close it via Escape.",
                    actual_label=str(label)[:120],
                    semantic_operation="dismiss",
                    already_attempted_count=attempted,
                    status="available",
                    source_region_id=dialog.element_id,
                    source_element_id=dialog.element_id,
                    semantic_type=dialog.dialog_type,
                    evidence=_evidence_strings(dialog.evidence),
                    confidence=_confidence_value(dialog.confidence, default=0.8),
                    category=ActionCategory.EXPLORATION.value,
                    metadata={"key": "Escape", "action_label": "Close dialog (Escape)"},
                )
            )
        return candidates

    @staticmethod
    def _build_unknown_component_candidates(
        canonical_model: "CanonicalPageModel", memory: "RunMemory", page_fingerprint: str | None = None
    ) -> list[FrontierCandidate]:
        """Preserve every UnknownComponent as a low-priority, safe (hover-only)
        investigation candidate rather than silently dropping it — the direct
        fix for docs/PAGE_PERCEPTION_AUDIT.md's "no fallback bucket" finding
        reaching all the way into exploration, not just observation."""
        candidates: list[FrontierCandidate] = []
        for comp in canonical_model.unknown_components or []:
            element_id = comp.element_id or comp.stable_id
            if not element_id or not comp.is_visible:
                continue
            if memory.element_failed_too_often(element_id):
                continue
            sig = action_signature(
                page_fingerprint=page_fingerprint or canonical_model.state_fingerprint,
                action_type="hover",
                element_id=element_id,
            )
            if memory.has_seen_signature(sig):
                continue
            attempted = memory.signature_counts.get(sig, 0)
            label = comp.text or comp.detection_reason or "Unknown component"
            candidates.append(
                FrontierCandidate(
                    candidate_id=f"inspect_unknown_{element_id}",
                    candidate_type="inspect_unknown_component",
                    action="hover",
                    element_id=element_id,
                    goal="investigate_unclassified_component",
                    risk="read_only",
                    priority=85,
                    reason=f"Unclassified element preserved as a safe investigation target ({comp.detection_reason}).",
                    actual_label=str(label)[:120],
                    semantic_operation="inspect",
                    already_attempted_count=attempted,
                    status="available",
                    source_region_id=comp.parent_region_id,
                    source_element_id=element_id,
                    semantic_type="unknown_component",
                    evidence=[comp.detection_reason] if comp.detection_reason else [],
                    confidence=_confidence_value(comp.confidence, default=0.3),
                    category=ActionCategory.EXPLORATION.value,
                    metadata={"action_label": "Inspect unknown component"},
                )
            )
        return candidates

    def _form_actionable(self, form: AuthFormInfo) -> bool:
        state = self.auth.form_lifecycle.get(form.form_id, FormLifecycle.DISCOVERED)
        return state not in {
            FormLifecycle.SUCCEEDED,
            FormLifecycle.BLOCKED,
            FormLifecycle.EXHAUSTED,
            FormLifecycle.POSITIVE_SUBMISSION_ATTEMPTED,
        }
