"""Exploration helpers — classify pages, infer modules, discover workflows."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.gemma.base import GemmaProvider
from app.schemas import ModuleRecord, PageClassification, PageState
from app.utils.ids import new_id
from app.utils.logging import get_logger

logger = get_logger("agent.explorer")

PAGE_TYPES = {
    "dashboard",
    "list",
    "detail",
    "create_form",
    "edit_form",
    "authentication",
    "settings",
    "report",
    "search_results",
    "error_page",
    "unknown",
}

# AI / mock often returns page-type synonyms as "modules" — ignore those.
_WEAK_MODULE_NAMES = {
    "form",
    "content",
    "general",
    "list",
    "detail",
    "unknown",
    "page",
    "module",
    "dashboard",
    "login",
    "authentication",
}


_KNOWN_FILE_EXTENSIONS = (
    ".html", ".htm", ".php", ".aspx", ".jsp", ".cfm", ".shtml", ".asp",
)


def strip_known_extension(segment: str) -> str:
    lower = segment.lower()
    for ext in _KNOWN_FILE_EXTENSIONS:
        if lower.endswith(ext):
            return segment[: -len(ext)]
    return segment


def humanize_path_segment(segment: str) -> str:
    """addUser / contact-list / add_contact / inventory.html → readable title."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", strip_known_extension(segment or ""))
    if not cleaned:
        return ""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", cleaned)
    spaced = spaced.replace("-", " ").replace("_", " ")
    return spaced.title()


def module_name_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    if not path:
        return "Home"
    segment = strip_known_extension(path.split("/")[0])
    lower = segment.lower()
    # Domain-specific friendly names for common QA demo apps
    if lower in {"adduser", "signup", "sign-up", "register"}:
        return "Sign Up"
    if lower in {"addcontact", "add-contact"}:
        return "Contacts"
    if lower in {"contactlist", "contact-list", "contacts"}:
        return "Contacts"
    if lower in {"login", "signin", "sign-in"}:
        return "Auth"
    if lower in {"logout", "signout", "sign-out"}:
        return "Auth"
    named = humanize_path_segment(segment)
    return named or "Home"


class Explorer:
    """Classify pages and maintain a lightweight module inventory."""

    def __init__(self, gemma: GemmaProvider | None = None) -> None:
        self.gemma = gemma
        self.modules: dict[str, ModuleRecord] = {}

    async def classify(self, page_state: PageState) -> PageState:
        deterministic = self.classify_deterministic(page_state)
        url_module = module_name_from_url(page_state.url)
        # Local Gemma 4B routinely burns GEMMA_TIMEOUT_SECONDS (~60s) on dense
        # SPAs (OrangeHRM Admin/PIM). Tester already uses skip_llm=True for the
        # same reason. Do not block the live loop on analyze_page for that provider.
        local_gemma = getattr(self.gemma, "name", "") == "openai_compatible"
        if self.gemma is not None and (deterministic.confidence >= 0.75 or local_gemma):
            page_state.classification = deterministic
        elif self.gemma is not None:
            try:
                ai = await self.gemma.analyze_page(page_state)
                page_type = (
                    ai.page_type if ai.page_type in PAGE_TYPES else deterministic.page_type
                )
                ai_module = (ai.module_guess or "").strip()
                # Prefer URL/heading-derived modules over weak AI labels like "Form"
                if not ai_module or ai_module.lower() in _WEAK_MODULE_NAMES:
                    module_guess = deterministic.module_guess or url_module
                else:
                    module_guess = ai_module
                page_state.classification = PageClassification(
                    page_type=page_type,
                    confidence=max(ai.confidence, deterministic.confidence),
                    purpose=ai.purpose or deterministic.purpose,
                    module_guess=module_guess,
                    tags=list({*(ai.tags or []), *(deterministic.tags or [])}),
                )
            except Exception as exc:
                logger.warning("Gemma page classify failed: %s", type(exc).__name__)
                page_state.classification = deterministic
        else:
            page_state.classification = deterministic

        # Final guards: auth forms beat generic "list"/"content" AI labels
        if page_state.classification:
            has_password = any(
                (f.field_type or "").lower() == "password"
                or "password" in ((f.name or "") + (f.label or "")).lower()
                for form in page_state.forms
                for f in form.fields
            )
            if has_password and page_state.classification.page_type not in {
                "authentication",
                "error_page",
            }:
                page_state.classification.page_type = "authentication"
                page_state.classification.module_guess = "Authentication"
                page_state.classification.purpose = "User authentication"
                page_state.classification.confidence = max(
                    page_state.classification.confidence, 0.9
                )
            guess = (page_state.classification.module_guess or "").strip()
            if not guess or guess.lower() in _WEAK_MODULE_NAMES:
                page_state.classification.module_guess = (
                    "Authentication"
                    if page_state.classification.page_type == "authentication"
                    else url_module
                )

        self._upsert_module(page_state)
        logger.info(
            "Classified %s as %s / module=%s",
            page_state.url,
            page_state.classification.page_type if page_state.classification else "unknown",
            page_state.classification.module_guess if page_state.classification else "?",
        )
        return page_state

    _CRASH_PAGE_MARKERS = (
        "not found",
        "404",
        "internal server error",
        "something went wrong",
        "unhandled exception",
        "503 service",
        "502 bad gateway",
    )

    @staticmethod
    def _looks_like_crash_page(page_state: PageState, *, text: str, path: str) -> bool:
        """True only for a broken route — not a form that printed the word 'error'."""
        if page_state.forms:
            return False
        if any(k in path for k in ("add", "edit", "signup", "register", "contact")):
            return False
        return any(k in text for k in Explorer._CRASH_PAGE_MARKERS)

    def classify_deterministic(self, page_state: PageState) -> PageClassification:
        title = (page_state.title or "").lower()
        url = page_state.url.lower()
        text = (page_state.visible_text_summary or "").lower()
        path = urlparse(page_state.url).path.lower()
        heading = " ".join(page_state.headings[:3]).lower() if page_state.headings else ""
        module = module_name_from_url(page_state.url)

        # Auth forms (password fields) before generic "Contact List" title heuristics
        has_password = any(
            (f.field_type or "").lower() == "password"
            or "password" in ((f.name or "") + (f.label or "")).lower()
            for form in page_state.forms
            for f in form.fields
        )
        if has_password or any(
            k in path or k in title
            for k in ("login", "sign-in", "signin", "/auth", "logout", "signout")
        ):
            return PageClassification(
                page_type="authentication",
                confidence=0.92 if has_password else 0.9,
                purpose="User authentication",
                module_guess="Authentication",
                tags=["authentication"],
            )

        # Signup / registration (Contact List /addUser, etc.)
        if any(
            k in path or k in title or k in heading
            for k in (
                "signup",
                "sign-up",
                "register",
                "adduser",
                "add-user",
                "create account",
                "add user",
            )
        ) or (
            "sign up" in text
            and "login" not in path
            and any(k in path for k in ("add", "sign", "register", "user"))
        ):
            return PageClassification(
                page_type="authentication",
                confidence=0.88,
                purpose="User registration / sign-up",
                module_guess="Authentication",
                tags=["authentication", "signup"],
            )
        # Bare "error" matches form validation copy ("Contact form validation
        # error") and must not steal create/edit/detail pages. Require a real
        # crash marker, and never override a page that still has a form.
        if self._looks_like_crash_page(page_state, text=text, path=path):
            return PageClassification(
                page_type="error_page",
                confidence=0.85,
                purpose="Error state",
                module_guess="Errors",
                tags=["error"],
            )
        if "settings" in path or "settings" in title or "preferences" in title:
            return PageClassification(
                page_type="settings",
                confidence=0.8,
                purpose="Application settings",
                module_guess="Settings",
                tags=["settings"],
            )
        # A header/global search box is not a search-results page. Treating
        # `search_fields` as sufficient scored OrangeHRM Admin/PIM at 0.7 and
        # blocked every observation on a 60s Gemma classify.
        if "search" in path or "search" in title:
            return PageClassification(
                page_type="search_results",
                confidence=0.75,
                purpose="Search results",
                module_guess=module,
                tags=["search"],
            )
        if "report" in path or "analytics" in path:
            return PageClassification(
                page_type="report",
                confidence=0.75,
                purpose="Reporting",
                module_guess="Reports",
                tags=["report"],
            )

        # Edit before generic "contact in path → list", otherwise /editContact
        # falls through as list@0.7 and every observation waits on Gemma.
        if (
            any(k in path for k in ("/edit", "/update", "editcontact", "edit-contact"))
            or "edit" in title
            or "edit" in heading
        ):
            purpose = "Edit entity form"
            if "contact" in path or "contact" in title or "contact" in heading:
                module = "Contacts"
                purpose = "Edit a contact"
            return PageClassification(
                page_type="edit_form",
                confidence=0.85,
                purpose=purpose,
                module_guess=module,
                tags=["form", "edit"],
            )

        # Create flows: /new, /create, add-, addContact, camelCase add*
        if (
            any(k in path for k in ("/new", "/create", "add-", "addcontact", "adduser"))
            or re.search(r"/add[a-z0-9]", path)
            or (page_state.forms and any(k in title or k in heading for k in ("create", "new", "add ")))
        ):
            purpose = "Create entity form"
            if "contact" in path or "contact" in title or "contact" in heading:
                module = "Contacts"
                purpose = "Create or add a contact"
            return PageClassification(
                page_type="create_form",
                confidence=0.82,
                purpose=purpose,
                module_guess=module,
                tags=["form", "create"],
            )

        if "contact" in path or ("contact" in title and "login" not in path):
            # Do not classify auth screens titled "Contact List App" as list pages
            if has_password:
                return PageClassification(
                    page_type="authentication",
                    confidence=0.9,
                    purpose="User authentication",
                    module_guess="Authentication",
                    tags=["authentication"],
                )
            if page_state.tables or "list" in path or "list" in title or "contactlist" in path:
                return PageClassification(
                    page_type="list",
                    confidence=0.85,
                    purpose="Contact list",
                    module_guess="Contacts",
                    tags=["list", "contacts"],
                )
            if any(k in path for k in ("contactdetails", "contact-details", "/detail")):
                return PageClassification(
                    page_type="detail",
                    confidence=0.82,
                    purpose="Contact detail",
                    module_guess="Contacts",
                    tags=["detail", "contacts"],
                )
            return PageClassification(
                page_type="detail",
                confidence=0.8,
                purpose="Contacts module",
                module_guess="Contacts",
                tags=["contacts"],
            )
        # Filter/search chrome on a table is still a list (OrangeHRM System
        # Users, Employee List). Requiring "no forms" used to fall through to
        # create_form@0.7 and wait on Gemma, then inspect_form the filter.
        if page_state.tables:
            return PageClassification(
                page_type="list",
                confidence=0.75,
                purpose="Tabular listing",
                module_guess=module,
                tags=["list"],
            )
        if any(k in path or k in title for k in ("dashboard", "home", "overview")):
            return PageClassification(
                page_type="dashboard",
                confidence=0.8,
                purpose="Primary landing / overview",
                module_guess="Dashboard",
                tags=["dashboard"],
            )
        if re.search(r"/\d+/?$", path) or "detail" in path:
            return PageClassification(
                page_type="detail",
                confidence=0.65,
                purpose="Entity detail",
                module_guess=module,
                tags=["detail"],
            )
        if page_state.forms:
            return PageClassification(
                page_type="create_form",
                confidence=0.7,
                purpose=heading.title() if heading else "Form page",
                module_guess=module,
                tags=["form"],
            )
        return PageClassification(
            page_type="unknown",
            confidence=0.45,
            purpose=(page_state.headings[0] if page_state.headings else "Unclassified content"),
            module_guess=module,
            tags=["unknown"],
        )

    def _module_from_url(self, url: str) -> str:
        return module_name_from_url(url)

    def _upsert_module(self, page_state: PageState) -> None:
        classification = page_state.classification
        module_name = (
            (classification.module_guess if classification else None)
            or module_name_from_url(page_state.url)
        )
        if module_name.lower() in _WEAK_MODULE_NAMES:
            module_name = module_name_from_url(page_state.url)
        key = module_name.lower()
        if key not in self.modules:
            self.modules[key] = ModuleRecord(
                module_id=new_id(),
                name=module_name,
                description=(classification.purpose if classification else "") or "",
                entry_urls=[page_state.url],
                page_ids=[page_state.page_id],
            )
        else:
            mod = self.modules[key]
            if page_state.url not in mod.entry_urls:
                mod.entry_urls.append(page_state.url)
            if page_state.page_id not in mod.page_ids:
                mod.page_ids.append(page_state.page_id)
            # Prefer a more specific purpose when available
            if classification and classification.purpose and len(classification.purpose) > len(
                mod.description or ""
            ):
                mod.description = classification.purpose

    def module_list(self) -> list[ModuleRecord]:
        return list(self.modules.values())
