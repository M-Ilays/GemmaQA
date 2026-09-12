"""Entity candidate extraction — pulls candidate NOUN TERMS out of one
CanonicalPageModel observation.

Application-neutral by construction: this module ships NO business
vocabulary. What it does ship is generic ENGLISH-UI linguistics —
a universal action-verb lexicon ("add", "edit", "export", ...), a stopword
list of application-chrome words every web app shares ("dashboard",
"settings", "login", ...), and a plural→singular normalizer. The entity
names themselves are always extracted from the target application's own
observed text, never from this file.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Iterable
from urllib.parse import urlparse

from app.intelligence.entity_discovery.schemas import EntityCandidate, EntityEvidence

if TYPE_CHECKING:
    from app.perception.models import CanonicalPageModel

# ---------------------------------------------------------------------------
# Generic linguistic resources (universal UI English, never business terms)
# ---------------------------------------------------------------------------

# Action verbs that prefix entity nouns in UI labels ("Add Customer",
# "Export Orders") and map to canonical operations. Keys are the observed
# verb; values are the canonical operation name. This is the ONLY place
# operations come from besides HTTP methods — nothing is per-entity.
OPERATION_VERBS: dict[str, str] = {
    "create": "create",
    "add": "create",
    "new": "create",
    "register": "create",
    "view": "view",
    "open": "view",
    "show": "view",
    "details": "view",
    "edit": "edit",
    "update": "edit",
    "modify": "edit",
    "rename": "edit",
    "change": "edit",
    "delete": "delete",
    "remove": "delete",
    "assign": "assign",
    "reassign": "assign",
    "approve": "approve",
    "reject": "reject",
    "decline": "reject",
    "archive": "archive",
    "unarchive": "restore",
    "restore": "restore",
    "import": "import",
    "export": "export",
    "clone": "clone",
    "duplicate": "duplicate",
    "copy": "duplicate",
    "merge": "merge",
    "split": "split",
    "search": "search",
    "find": "search",
    "filter": "filter",
    "download": "download",
    "upload": "upload",
    "print": "export",
    # Form/dialog chrome verbs. Recognized so they can be STRIPPED from a
    # label's noun phrase ("Apply filters" -> "filters"), but never recorded
    # as an entity's operation — see NON_ENTITY_OPERATIONS below.
    "cancel": "cancel",
    "submit": "submit",
    "save": "save",
    "apply": "apply",
    "reset": "reset",
    "refresh": "refresh",
}

# Verbs that describe interacting with a FORM or the page itself, not an
# operation the application offers ON an entity. "Save"/"Apply" appear on
# nearly every form in every application; treating them as entity operations
# would report the same meaningless capability for every entity discovered.
NON_ENTITY_OPERATIONS = frozenset({"cancel", "submit", "save", "apply", "reset", "refresh"})

# HTTP method → canonical operation, for API-endpoint evidence.
HTTP_METHOD_OPERATIONS: dict[str, str] = {
    "POST": "create",
    "GET": "view",
    "PUT": "edit",
    "PATCH": "edit",
    "DELETE": "delete",
}

# Application-chrome words every web application shares — never business
# entities in their own right. Universal UI/platform vocabulary only.
UI_STOPWORDS = frozenset(
    {
        # navigation chrome
        "home", "dashboard", "overview", "menu", "navigation", "sidebar",
        "settings", "preferences", "options", "configuration", "config",
        "profile", "account", "help", "support", "docs", "documentation",
        "about", "faq", "admin",
        # NB: deliberately NOT excluding words that are chrome in some apps
        # but genuine entities in others (e.g. a "Contact Us" link vs. a CRM's
        # Contacts module) — excluding those would bake one application's
        # meaning into this lexicon. Such terms simply arrive as low-confidence
        # candidates until corroborated by a second source kind.
        # auth chrome
        "login", "logout", "log", "sign", "signin", "signout", "signup",
        "register", "password", "credential", "session", "auth",
        # legal chrome
        "privacy", "policy", "terms", "condition", "legal", "cookie",
        "copyright", "license",
        # generic structure words
        "page", "list", "table", "form", "field", "row", "column", "tab",
        "section", "panel", "card", "dialog", "modal", "button", "link",
        "item", "detail", "info", "information", "summary", "description",
        "name", "title", "label", "value", "type", "status", "state",
        "date", "time", "id", "number", "count", "total", "action",
        "actions", "operation", "result", "results", "data", "record",
        "records", "entry", "entries", "all", "none", "other", "misc",
        # generic web words
        "html", "index", "main", "content", "site", "web", "app",
        "application", "api", "v1", "v2", "v3", "www", "http", "https",
        # demo/sample-environment chrome (universal, not one app's vocabulary)
        "demo", "sample", "example", "test", "sandbox", "playground", "preview",
        # pagination / search chrome
        "next", "previous", "prev", "first", "last", "more", "back",
        "search", "filter", "sort", "reset", "clear", "apply", "cancel",
        "submit", "save", "ok", "yes", "no", "confirm", "close", "loading",
        "welcome", "error", "warning", "success", "notification",
        "message", "email", "phone", "address", "step",
        # English function words (particles/prepositions/pronouns) that can
        # survive verb-stripping ("Log out" -> "out")
        "out", "in", "into", "up", "down", "off", "on", "to", "from",
        "with", "for", "and", "or", "the", "a", "an", "of", "by", "at",
        "as", "is", "are", "be", "not", "you", "your", "my", "our", "their",
        "this", "that", "these", "those", "it", "we", "they", "here", "there",
        "now", "go", "get", "see",
    }
)

# Irregular plural forms the suffix rules below can't derive.
_IRREGULAR_PLURALS: dict[str, str] = {
    "people": "person",
    "children": "child",
    "men": "man",
    "women": "woman",
    "feet": "foot",
    "teeth": "tooth",
    "mice": "mouse",
    "geese": "goose",
    "criteria": "criterion",
    "media": "medium",
    "data": "data",
    "series": "series",
    "species": "species",
    "staff": "staff",
    "equipment": "equipment",
    "analyses": "analysis",
    "statuses": "status",
    "addresses": "address",
}

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z\-']*")
_ID_LIKE_RE = re.compile(
    r"^(?:\d+|[0-9a-f]{8,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[a-z]+-\d+)$",
    re.IGNORECASE,
)
MAX_TERM_WORDS = 3
MIN_TERM_LENGTH = 3


def singularize(word: str) -> str:
    """Deterministic English plural→singular normalization. Intentionally
    simple suffix rules + an irregulars table — good enough to merge
    "customers"/"customer"; anything it gets wrong just yields two aliases
    that a human can still read."""
    w = word.lower()
    if w in _IRREGULAR_PLURALS:
        return _IRREGULAR_PLURALS[w]
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("ches", "shes", "xes", "zes", "sses")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def normalize_term(text: str) -> str:
    """Lowercase, singularize each word, join with single spaces."""
    words = _WORD_RE.findall(text.lower())
    return " ".join(singularize(w) for w in words)


def _looks_like_entity_term(raw: str) -> bool:
    """A label can yield an entity term if, after removing generic chrome and
    verb words, something substantive remains and the phrase isn't absurdly
    long. Chrome-suffixed phrases still count: a "Customer Profile" heading is
    real evidence about whatever "customer" is, even though "profile" itself
    is chrome — the stripping happens in `_strip_chrome_words`."""
    words = _WORD_RE.findall(raw.lower())
    if not words or len(words) > MAX_TERM_WORDS:
        return False
    remaining = [w for w in words if w not in UI_STOPWORDS and not _is_verb_like(w)]
    if not remaining:
        return False
    return any(len(singularize(w)) >= MIN_TERM_LENGTH for w in remaining)


def split_verb_and_noun(label: str) -> tuple[str | None, str]:
    """"Add Customer" -> ("create", "customer"); "Customers" -> (None,
    "customers"); a bare verb "Export" -> ("export", ""). The verb (if any)
    is the leading word when it's in the generic operation lexicon."""
    words = _WORD_RE.findall(label.lower())
    if not words:
        return None, ""
    if words[0] in OPERATION_VERBS:
        return OPERATION_VERBS[words[0]], " ".join(words[1:])
    return None, " ".join(words)


def _is_verb_like(word: str) -> bool:
    """The word — or its stem after removing a common participle suffix —
    is an operation verb ("assigned" -> "assign", "editing" -> "edit").
    Purely morphological; no business vocabulary."""
    if word in OPERATION_VERBS:
        return True
    for suffix in ("ed", "d", "ing"):
        if word.endswith(suffix) and word[: -len(suffix)] in OPERATION_VERBS:
            return True
    return False


def _strip_chrome_words(term: str) -> str:
    """Remove pure-chrome and verb-derived words from a candidate noun phrase
    ("customer list" -> "customer"; "assigned driver" -> "driver")."""
    words = [w for w in term.split() if w not in UI_STOPWORDS and not _is_verb_like(w)]
    return " ".join(words)


def _candidate(raw: str, source_kind: str, *, page_url: str, fingerprint: str,
               element_id: str | None = None, iteration: int = 0) -> EntityCandidate | None:
    if not raw or not raw.strip():
        return None
    if not _looks_like_entity_term(raw):
        return None
    normalized = _strip_chrome_words(normalize_term(raw))
    if not normalized or len(normalized) < MIN_TERM_LENGTH:
        return None
    return EntityCandidate(
        term=normalized,
        raw_term=raw.strip()[:120],
        evidence=EntityEvidence(
            source_kind=source_kind,
            observed_text=raw.strip()[:160],
            page_url=page_url,
            state_fingerprint=fingerprint,
            element_id=element_id,
            observed_at_iteration=iteration,
        ),
    )


def url_path_terms(url: str) -> list[str]:
    """Meaningful (non-id, non-chrome) path segments of a URL — the single
    strongest naming convention in web applications (/customers/cust-001)."""
    try:
        path = urlparse(url).path
    except Exception:
        return []
    terms: list[str] = []
    for segment in path.strip("/").split("/"):
        segment = re.sub(r"\.[a-z0-9]{2,5}$", "", segment)  # drop .html/.php
        segment = segment.replace("-", " ").replace("_", " ").strip()
        if not segment or _ID_LIKE_RE.match(segment.replace(" ", "-")):
            continue
        # Chrome path segments ("api", "v1", "admin", "index") are never
        # entity evidence themselves.
        words = segment.lower().split()
        if all(singularize(w) in UI_STOPWORDS or w in UI_STOPWORDS for w in words):
            continue
        terms.append(segment)
    return terms


class EntityCandidateBuilder:
    """Extracts every entity candidate from one CanonicalPageModel."""

    def build(self, model: "CanonicalPageModel", *, iteration: int = 0) -> list[EntityCandidate]:
        url = model.url
        fp = model.state_fingerprint or ""
        out: list[EntityCandidate] = []

        def add(raw: str | None, kind: str, element_id: str | None = None) -> None:
            if raw is None:
                return
            cand = _candidate(raw, kind, page_url=url, fingerprint=fp, element_id=element_id, iteration=iteration)
            if cand is not None:
                out.append(cand)

        # Navigation items (header/sidebar — wherever the model found them).
        for region in model.navigation_regions or []:
            for item in region.items or []:
                add(item.text, "navigation_item", item.element_id)

        for crumb in model.breadcrumbs or []:
            add(crumb.text, "breadcrumb", crumb.element_id)

        for group in model.tabs or []:
            for tab in group.tabs or []:
                add(tab.text or tab.accessible_name, "tab", tab.element_id)

        for heading in model.headings or []:
            add(heading.text, "heading", heading.element_id)

        # Page titles routinely carry app-name suffixes ("Customers - Acme",
        # "Reports | InsightBoard") — split on the common separators and
        # evaluate each part on its own instead of as one polluted phrase.
        for part in re.split(r"[\-|·:–—>]", model.title or ""):
            add(part.strip(), "page_title")

        for segment in url_path_terms(url):
            add(segment, "url_segment")

        # Tables: the table's own naming context comes from its columns;
        # column headers are weaker (attribute-level) evidence.
        for table in model.tables or []:
            for header in table.headers or []:
                add(header, "table_column", table.table_id)

        # Forms: field labels/names.
        for form in model.forms or []:
            for f in form.fields or []:
                add(f.label or f.name, "form_field", f.element_id)

        for dialog in model.dialogs or []:
            add(dialog.text, "dialog", dialog.element_id)

        # Buttons & links: verb-stripped noun phrases ("Add Customer" ->
        # "customer"); the verb itself is consumed later by the classifier
        # for operation inference.
        for el in model.interactive_elements or []:
            label = el.accessible_name or el.visible_text or el.text or ""
            if not label:
                continue
            kind = "link_label" if (el.category == "link" or el.tag == "a") else "button_label"
            _, noun = split_verb_and_noun(label)
            add(noun, kind, el.element_id)
            if el.aria_label and el.aria_label != label:
                _, aria_noun = split_verb_and_noun(el.aria_label)
                add(aria_noun, "aria_label", el.element_id)

        for link in model.links or []:
            if link.element_id and any(c.evidence.element_id == link.element_id for c in out):
                continue  # already captured via interactive_elements
            add(link.text, "link_label", link.element_id)

        # API endpoints from network evidence ("GET https://x/api/customers").
        for entry in model.network_evidence or []:
            text = entry.text or ""
            parts = text.split(" ", 1)
            entry_url = parts[1] if len(parts) == 2 else text
            for segment in url_path_terms(entry_url):
                add(segment, "api_endpoint")

        return out
