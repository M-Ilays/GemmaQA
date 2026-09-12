"""Actor candidate extraction — pulls candidate ACTOR/ROLE TERMS out of one
CanonicalPageModel observation.

Application-neutral by construction: this module ships NO role/actor
vocabulary. What it does ship is generic structural/linguistic scaffolding —
hint word lists for locating settings/user-management/roles/permission pages
and role-ish form fields/table columns (universal across web applications),
and a small set of DESCRIPTIVE modifiers ("guest", "default", "system",
"temporary") that describe the NATURE of a role slot, never a business
identity. The actor/role NAMES themselves always come from the target
application's own observed text — a role dropdown's options, a Users
table's role column, a nav item's label.

Reuses app.intelligence.entity_discovery's generic linguistic primitives
(normalize_term/singularize/split_verb_and_noun/url_path_terms/operation
lexicons) rather than duplicating them — permission inference and actor
discovery need the exact same "what verb is this" and "what does this URL
segment mean" logic entity discovery already built.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.intelligence.entity_discovery.entity_candidate_builder import (
    normalize_term,
    singularize,
)
from app.intelligence.actor_discovery.schemas import ActorCandidate, ActorEvidence

if TYPE_CHECKING:
    from app.perception.models import CanonicalPageModel

# ---------------------------------------------------------------------------
# Generic hint vocabularies (universal web-app structure, never one
# application's business vocabulary)
# ---------------------------------------------------------------------------

ROLE_FIELD_HINTS = (
    "role", "roles", "user role", "permission level", "access level",
    "user type", "account type", "membership", "privilege", "user group",
    "group", "team role",
)

ADMIN_PAGE_HINTS = (
    "settings", "configuration", "preferences", "users", "user management",
    "roles", "permissions", "access control", "administration", "admin",
    "security", "audit", "management", "team members", "members", "team",
)

USER_MANAGEMENT_HINTS = ("users", "user management", "members", "team members", "team", "accounts")
ROLES_PAGE_HINTS = ("roles", "role management", "user roles")
PERMISSIONS_PAGE_HINTS = ("permissions", "permission", "access control", "acl")
SETTINGS_PAGE_HINTS = ("settings", "configuration", "preferences", "admin", "administration")

INVITE_DIALOG_HINTS = (
    "invite", "add user", "add member", "new user", "create user",
    "create account", "invite user", "invite member",
)
ASSIGNMENT_HINTS = ("assign", "assignee", "assigned to", "reassign", "assign to")
APPROVAL_HINTS = ("approve", "approval", "reviewer", "approver", "reject", "decline")
ACCESS_DENIED_HINTS = (
    "access denied", "403 forbidden", "forbidden", "not authorized",
    "unauthorized", "permission denied", "you do not have permission",
    "you don't have permission", "insufficient privileges",
    "insufficient permissions", "you are not allowed",
)
PROFILE_HINTS = ("profile", "my account", "account settings", "logged in as", "signed in as")
# Deliberately narrow: "home"/"landing" were tried and rejected — live/unit
# testing showed a plain landing page titled "Home" got misclassified as a
# dashboard (and, via page-reachability permission inference, silently
# promoted a thin session straight to "confirmed" status). "dashboard" and
# "overview" are specific enough not to false-positive on an ordinary page.
DASHBOARD_HINTS = ("dashboard", "overview")

# Descriptive modifiers observed directly IN a role's own option/cell text —
# describe the role SLOT's nature, not a business identity.
ROLE_MODIFIER_FLAGS: dict[str, str] = {
    "guest": "is_guest",
    "default": "is_default",
    "system": "is_system",
    "temporary": "is_temporary",
    "temp": "is_temporary",
    "trial": "is_temporary",
}

# Placeholder option values that are never real roles.
_PLACEHOLDER_OPTIONS = frozenset(
    {"", "select", "select a role", "select role", "choose", "choose one",
     "choose a role", "none", "n/a", "-", "--", "---", "please select"}
)

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z\-']*")
MAX_TERM_WORDS = 4
MIN_TERM_LENGTH = 2


def _looks_like_role_term(raw: str) -> bool:
    # Strip decorative punctuation dropdowns commonly wrap placeholders in
    # ("-- Select a role --", "— Choose —") before the placeholder check.
    text = raw.strip().strip("-—–_. ").strip().lower()
    if not text or text in _PLACEHOLDER_OPTIONS:
        return False
    words = _WORD_RE.findall(text)
    if not words or len(words) > MAX_TERM_WORDS:
        return False
    if len(singularize(words[-1])) < MIN_TERM_LENGTH:
        return False
    return True


def _hint_match(text: str, hints: tuple[str, ...]) -> bool:
    text = (text or "").lower()
    return any(hint in text for hint in hints)


def role_modifier_flags(term: str) -> dict[str, bool]:
    """Which descriptive flags this observed role text carries."""
    words = set(_WORD_RE.findall(term.lower()))
    flags = {"is_guest": False, "is_system": False, "is_default": False, "is_temporary": False}
    for word in words:
        flag = ROLE_MODIFIER_FLAGS.get(word)
        if flag:
            flags[flag] = True
    return flags


def _candidate(
    raw: str, source_kind: str, *, page_url: str, fingerprint: str,
    element_id: str | None = None, iteration: int = 0,
) -> ActorCandidate | None:
    if not raw or not _looks_like_role_term(raw):
        return None
    normalized = normalize_term(raw)
    if not normalized:
        return None
    return ActorCandidate(
        term=normalized,
        raw_term=raw.strip()[:120],
        evidence=ActorEvidence(
            source_kind=source_kind,
            observed_text=raw.strip()[:160],
            page_url=page_url,
            state_fingerprint=fingerprint,
            element_id=element_id,
            observed_at_iteration=iteration,
        ),
    )


class ActorCandidateBuilder:
    """Extracts every actor/role candidate from one CanonicalPageModel, plus
    the page-classification evidence (settings/user-management/roles/
    permissions/profile/dashboard/access-denied) permission_discovery.py and
    the classifier need."""

    def build(
        self, model: "CanonicalPageModel", *, iteration: int = 0, known_role_terms: frozenset[str] = frozenset()
    ) -> list[ActorCandidate]:
        url = model.url
        fp = model.state_fingerprint or ""
        out: list[ActorCandidate] = []

        def add(raw: str | None, kind: str, element_id: str | None = None) -> None:
            if raw is None:
                return
            cand = _candidate(raw, kind, page_url=url, fingerprint=fp, element_id=element_id, iteration=iteration)
            if cand is not None:
                out.append(cand)

        def is_role_hinted_or_known(text: str) -> bool:
            if _hint_match(text, ROLE_FIELD_HINTS) or _hint_match(text, ROLES_PAGE_HINTS):
                return True
            # Cross-reference against ALREADY-discovered role terms (mirrors
            # entity_discovery's relationship builder taking `known_terms`):
            # a nav item/breadcrumb reading exactly "Admin" isn't role-ish by
            # hint words alone, but IS corroborating evidence once "admin"
            # is already a known candidate role from elsewhere (a dropdown,
            # a table).
            return normalize_term(text) in known_role_terms

        # Navigation items / breadcrumbs that are themselves role-ish
        # ("Admins", "Manage Roles") or that name an already-known role
        # ("Admin") — direct evidence of a role's existence.
        for region in model.navigation_regions or []:
            for item in region.items or []:
                text = item.text or ""
                if is_role_hinted_or_known(text):
                    add(text, "navigation_item", item.element_id)
        for crumb in model.breadcrumbs or []:
            text = crumb.text or ""
            if is_role_hinted_or_known(text):
                add(text, "breadcrumb", crumb.element_id)

        # Role dropdowns: any select-like field/control whose label/name
        # hints at a role, mined for its OPTIONS.
        for form in model.forms or []:
            for field in form.field_descriptors or []:
                label = field.label or field.accessible_name or ""
                if not _hint_match(label, ROLE_FIELD_HINTS):
                    continue
                for option in field.options or []:
                    add(option, "role_dropdown_option", field.element_id)
        for el in model.interactive_elements or []:
            label = el.accessible_name or el.label or el.name or ""
            if el.available_options and _hint_match(label, ROLE_FIELD_HINTS):
                for option in el.available_options:
                    add(option, "role_dropdown_option", el.element_id)

        # Role tables: a table with a role-ish column header — each sample
        # row's value in that column is a candidate role/actor term.
        for table in model.tables or []:
            headers = [h or "" for h in (table.headers or [])]
            role_col_idx = next((i for i, h in enumerate(headers) if _hint_match(h, ROLE_FIELD_HINTS)), None)
            if role_col_idx is None:
                continue
            for row in table.sample_rows or []:
                if role_col_idx < len(row) and row[role_col_idx]:
                    # A cell may list multiple roles ("Admin, Support").
                    for piece in re.split(r"[,/;]", str(row[role_col_idx])):
                        add(piece, "role_table_cell", table.table_id)

        return out

    # -- page/context classification (consumed by permission_discovery/classifier) --

    @staticmethod
    def classify_page_context(model: "CanonicalPageModel") -> dict[str, bool]:
        """Cheap, generic page-purpose flags from URL/title/headings/nav —
        never a business classification, purely structural hint matching."""
        haystack = " ".join(
            filter(
                None,
                [
                    model.title,
                    *(h.text or "" for h in (model.headings or [])[:3]),
                    *(c.text or "" for c in (model.breadcrumbs or [])),
                    model.url,
                ],
            )
        ).lower()
        return {
            "is_settings_page": _hint_match(haystack, SETTINGS_PAGE_HINTS),
            "is_user_management_page": _hint_match(haystack, USER_MANAGEMENT_HINTS),
            "is_roles_page": _hint_match(haystack, ROLES_PAGE_HINTS),
            "is_permissions_page": _hint_match(haystack, PERMISSIONS_PAGE_HINTS),
            "is_profile_page": _hint_match(haystack, PROFILE_HINTS),
            "is_dashboard_page": _hint_match(haystack, DASHBOARD_HINTS),
        }

    @staticmethod
    def detect_access_denied(model: "CanonicalPageModel") -> bool:
        """Generic phrase scan across headings/alerts/text blocks — never a
        business classification, universal denial phrasing only."""
        texts = [
            *(h.text or "" for h in (model.headings or [])),
            *(a.text or "" for a in (model.alerts or [])),
            *(t.text or "" for t in (model.text_blocks or [])),
        ]
        blob = " ".join(texts).lower()
        return _hint_match(blob, ACCESS_DENIED_HINTS)

    @staticmethod
    def is_invite_or_creation_context(model: "CanonicalPageModel") -> bool:
        haystack = " ".join(
            filter(None, [model.title, *(h.text or "" for h in (model.headings or [])[:3])])
        ).lower()
        if _hint_match(haystack, INVITE_DIALOG_HINTS):
            return True
        return any(_hint_match(d.text or "", INVITE_DIALOG_HINTS) for d in (model.dialogs or []))

    @staticmethod
    def is_approval_context(model: "CanonicalPageModel") -> bool:
        for el in model.interactive_elements or []:
            label = el.accessible_name or el.text or el.visible_text or ""
            if _hint_match(label, APPROVAL_HINTS):
                return True
        return any(_hint_match(d.text or "", APPROVAL_HINTS) for d in (model.dialogs or []))

    @staticmethod
    def assignment_targets(model: "CanonicalPageModel", *, iteration: int = 0) -> list[ActorCandidate]:
        """Assignment dialogs/dropdowns ("Assign to: <role/person>") — the
        target term is evidence of a known_assignment_type."""
        out: list[ActorCandidate] = []
        url, fp = model.url, model.state_fingerprint or ""
        for form in model.forms or []:
            for field in form.field_descriptors or []:
                label = field.label or field.accessible_name or ""
                if not _hint_match(label, ASSIGNMENT_HINTS):
                    continue
                for option in field.options or []:
                    cand = _candidate(option, "assignment_dialog", page_url=url, fingerprint=fp,
                                       element_id=field.element_id, iteration=iteration)
                    if cand is not None:
                        out.append(cand)
        return out
