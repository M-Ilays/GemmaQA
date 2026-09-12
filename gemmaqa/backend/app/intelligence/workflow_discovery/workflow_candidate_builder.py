"""Workflow candidate extraction — per-page structural signals that a
workflow step exists here, BEFORE any cross-page reconstruction.

Application-neutral: every hint list here is generic UI/structural
vocabulary (a "status" column, a "confirm" dialog, a "my tasks" queue —
universal across web applications), never one target application's
business terms. Reuses the Entity Discovery / Actor Discovery hint lists
and cross-references their already-computed page context (the page's
subject entity, the current session actor) rather than recomputing them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.intelligence.actor_discovery.actor_candidate_builder import (
    APPROVAL_HINTS,
    ASSIGNMENT_HINTS,
    ROLE_FIELD_HINTS,
)
from app.intelligence.workflow_discovery.schemas import WorkflowCandidate, WorkflowEvidence
from app.intelligence.workflow_discovery.workflow_step_extractor import classify_semantic_action

if TYPE_CHECKING:
    from app.intelligence.entity_discovery import EntityRegistry
    from app.perception.models import CanonicalPageModel

_STATUS_HINTS = ("status", "state", "stage", "mark as", "change status")
_CONFIRMATION_HINTS = ("confirm", "are you sure", "please confirm")
PROGRESSION_VERBS = (
    "next", "continue", "submit", "finish", "complete", "close", "reopen",
    "cancel", "proceed", "save draft", "save", "done",
)
# "Owner"/"Owned by" columns and fields are ownership-transfer evidence,
# structurally identical to an assignment control — extends (never
# replaces) actor_discovery's own ASSIGNMENT_HINTS for this module's use.
OWNERSHIP_HINTS = ASSIGNMENT_HINTS + ("owner", "owned by", "ownership")
_ACTOR_QUEUE_HINTS = (
    "my tasks", "assigned to me", "my approvals", "my queue", "inbox",
    "to-do", "todo", "pending my action", "my requests", "my work",
)
_VIEW_HINTS = ("view", "details", "open", "inspect")
MAX_STEP_WORDS_FOR_MULTIWORD_HINT = 4


def _hint_match(text: str, hints: tuple[str, ...]) -> bool:
    text = (text or "").lower()
    return any(h in text for h in hints)


def _evidence(source_kind: str, text: str, *, page_url: str, fingerprint: str, element_id: str | None = None) -> WorkflowEvidence:
    return WorkflowEvidence(
        source_kind=source_kind, observed_text=(text or "")[:160], page_url=page_url,
        state_fingerprint=fingerprint, element_id=element_id,
    )


class WorkflowCandidateBuilder:
    def build(
        self,
        model: "CanonicalPageModel",
        *,
        entity_registry: "EntityRegistry | None" = None,
        primary_entity_term: str | None = None,
        current_actor_term: str | None = None,
    ) -> list[WorkflowCandidate]:
        url, fp = model.url, model.state_fingerprint or ""
        out: list[WorkflowCandidate] = []
        claimed: set[str] = set()

        def add(candidate_type: str, source_kind: str, text: str, *, element_id: str | None = None, action_verb: str = "") -> None:
            if element_id:
                claimed.add(element_id)
            out.append(
                WorkflowCandidate(
                    candidate_type=candidate_type,
                    entity_id=primary_entity_term,
                    actor_id=current_actor_term,
                    action_verb=action_verb,
                    page_url=url,
                    evidence=_evidence(source_kind, text, page_url=url, fingerprint=fp, element_id=element_id),
                )
            )

        self._forms(model, add)
        self._status_controls(model, add)
        self._assignment_and_approval_controls(model, add)
        self._dialogs(model, add)
        self._action_controls(model, add, claimed)
        self._multi_step_form(model, add)
        self._list_to_detail_navigation(model, add)
        self._actor_queue(model, add)
        if entity_registry is not None:
            self._entity_cross_references(model, entity_registry, primary_entity_term, add)
        return out

    # -- forms: create vs edit, structurally (mostly-empty vs mostly-filled) --

    @staticmethod
    def _forms(model, add) -> None:
        for form in model.forms or []:
            fields = list(form.field_descriptors or [])
            if not fields:
                candidate_type = "create_form"
            else:
                filled = sum(1 for f in fields if (f.current_value or "").strip())
                candidate_type = "edit_form" if filled >= max(1, len(fields) // 2) else "create_form"
            add(
                candidate_type,
                "create_form" if candidate_type == "create_form" else "edit_form",
                f"form {form.form_id} ({len(fields)} fields, structurally {candidate_type})",
                element_id=form.form_id,
            )

    # -- status controls: a status-hinted field/control, or a status column --

    @staticmethod
    def _status_controls(model, add) -> None:
        for el in model.interactive_elements or []:
            label = el.accessible_name or el.text or el.visible_text or ""
            if _hint_match(label, _STATUS_HINTS):
                add("status_control", "status_control", label, element_id=el.element_id)
        for table in model.tables or []:
            headers = [h or "" for h in (table.headers or [])]
            has_status_header = any(h.lower() in {"status", "state", "stage"} for h in headers)
            has_ownership_header = any(_hint_match(h, OWNERSHIP_HINTS) for h in headers)
            if has_status_header or has_ownership_header:
                add("status_control", "status_control", f"table {table.table_id} has a status/assignment column", element_id=table.table_id)

    # -- assignment / approval / rejection ------------------------------------

    @staticmethod
    def _assignment_and_approval_controls(model, add) -> None:
        for form in model.forms or []:
            for field_ in form.field_descriptors or []:
                label = field_.label or field_.accessible_name or ""
                if _hint_match(label, OWNERSHIP_HINTS) or _hint_match(label, ROLE_FIELD_HINTS):
                    add("assignment_control", "assignment_control", label, element_id=field_.element_id)
        for table in model.tables or []:
            headers = [h or "" for h in (table.headers or [])]
            if any(_hint_match(h, OWNERSHIP_HINTS) or _hint_match(h, ROLE_FIELD_HINTS) for h in headers):
                add(
                    "assignment_control", "assignment_control",
                    f"table {table.table_id} has an assignment/ownership column", element_id=table.table_id,
                )
        for el in model.interactive_elements or []:
            label = el.accessible_name or el.text or el.visible_text or ""
            if not label:
                continue
            lowered = label.lower()
            if "reject" in lowered or "decline" in lowered:
                add("rejection_control", "rejection_control", label, element_id=el.element_id, action_verb="reject")
            elif _hint_match(label, APPROVAL_HINTS):
                add("approval_control", "approval_control", label, element_id=el.element_id, action_verb="approve")

    # -- confirmation dialogs --------------------------------------------------

    @staticmethod
    def _dialogs(model, add) -> None:
        for dialog in model.dialogs or []:
            text = dialog.text or ""
            source_kind = "confirmation_dialog" if _hint_match(text, _CONFIRMATION_HINTS) or not text else "confirmation_dialog"
            add("confirmation_dialog", source_kind, text or "(dialog)", element_id=dialog.element_id)

    # -- action controls: any button whose label resolves to a recognized or
    # unknown-but-preserved workflow verb (Next/Continue/Submit/Dispatch/
    # Resolve/Escalate/Publish/Refresh/...) — never a closed list; an
    # unrecognized verb is still preserved as its own semantic_action
    # (see workflow_step_extractor.classify_semantic_action), never discarded.

    @staticmethod
    def _action_controls(model, add, claimed: set[str]) -> None:
        # Plain navigation-landmark links (Dashboard/Customers/Jobs/Settings-
        # style top nav) are app chrome, not an action ON an entity -- they
        # are already represented as their own navigation candidates
        # elsewhere and must not be attributed to whichever entity happens
        # to be this page's subject.
        nav_element_ids = {
            item.element_id
            for region in (model.navigation_regions or [])
            for item in (region.items or [])
            if item.element_id
        }
        # Tab switches (Overview/Performance/Activity/...) are a VIEW
        # selection, structurally identical to navigation -- already their
        # own candidate concern (select_tab, elsewhere), never an action on
        # the current page's entity.
        tab_element_ids = {
            tab.element_id
            for group in (model.tabs or [])
            for tab in (group.tabs or [])
            if tab.element_id
        }
        for el in model.interactive_elements or []:
            if el.element_id and (el.element_id in claimed or el.element_id in nav_element_ids or el.element_id in tab_element_ids):
                continue
            # Only elements that TRIGGER an action (buttons/links) are
            # candidates here -- form fields (input/select/textarea) are
            # structurally never actions in themselves, just data carriers
            # already covered by the form-level candidates above; treating
            # a field's own label ("First Name", "Email*", ...) as an
            # action verb produced meaningless steps in live testing.
            tag = (el.tag or "").lower()
            role = (el.role or "").lower()
            if tag in {"input", "select", "textarea", "label"} or role in {"textbox", "combobox", "listbox"}:
                continue
            # A plain anchor (no explicit button role) is a navigation
            # transition to another resource, not an action ON the current
            # entity -- e.g. a "Jordan Lee" link on a dashboard's recent-
            # customers list navigates to that customer's detail page; its
            # own display text (a proper noun) is not a workflow verb.
            # List-to-detail navigation is already its own candidate type
            # (see _list_to_detail_navigation); an anchor explicitly given
            # role="button" is kept, since that IS an action trigger.
            if tag == "a" and role != "button":
                continue
            label = (el.accessible_name or el.text or el.visible_text or "").strip()
            if not label:
                continue
            first_word = label.lower().split()[0]
            # A leading digit means this is a KPI/stat figure ("128 Active
            # Customers"), never a verb, in any application.
            if first_word[:1].isdigit():
                continue
            semantic = classify_semantic_action(first_word)
            if not semantic:
                continue
            add("progression_button", "progression_button", label, element_id=el.element_id, action_verb=first_word)

    # -- multi-step form / wizard ----------------------------------------------

    @staticmethod
    def _multi_step_form(model, add) -> None:
        has_form = bool(model.forms)
        if not has_form:
            return
        stepper_like = bool(model.pagination) or any(
            len(tg.tabs) >= 2 and all((t.text or "").strip()[:1].isdigit() for t in tg.tabs if t.text)
            for tg in (model.tabs or [])
        )
        if stepper_like:
            add("multi_step_form", "multi_step_form", "form combined with a stepper/pagination-like control")

    # -- list -> detail -> action navigation -----------------------------------

    @staticmethod
    def _list_to_detail_navigation(model, add) -> None:
        if len(model.breadcrumbs or []) >= 2:
            add("list_to_detail_navigation", "list_to_detail_navigation", " > ".join(b.text or "" for b in model.breadcrumbs))
            return
        has_table = bool(model.tables)
        has_view_control = any(
            _hint_match(el.accessible_name or el.text or "", _VIEW_HINTS) for el in (model.interactive_elements or [])
        )
        if has_table and has_view_control:
            add("list_to_detail_navigation", "list_to_detail_navigation", "table with a view/details control")

    # -- actor-specific task queues --------------------------------------------

    @staticmethod
    def _actor_queue(model, add) -> None:
        haystack = " ".join(
            filter(None, [model.title, *(h.text or "" for h in (model.headings or [])[:3])])
        )
        if _hint_match(haystack, _ACTOR_QUEUE_HINTS):
            add("actor_queue", "actor_queue", haystack[:160])
        for region in model.navigation_regions or []:
            for item in region.items or []:
                if _hint_match(item.text or "", _ACTOR_QUEUE_HINTS):
                    add("actor_queue", "actor_queue", item.text or "", element_id=item.element_id)

    # -- cross-reference against ALREADY-known entities ------------------------

    @staticmethod
    def _entity_cross_references(model, entity_registry, primary_entity_term, add) -> None:
        known = {r.canonical_name for r in entity_registry.all_entities()}
        if primary_entity_term:
            known.discard(primary_entity_term)
        if not known:
            return
        for table in model.tables or []:
            for header in table.headers or []:
                words = (header or "").lower().split()
                if any(w in known for w in words) or (header or "").lower() in known:
                    add("entity_cross_reference", "entity_cross_reference", header or "", element_id=table.table_id)
        for form in model.forms or []:
            for field_ in form.field_descriptors or []:
                label = (field_.label or field_.accessible_name or "").lower()
                words = label.split()
                if any(w in known for w in words) or label in known:
                    add("entity_cross_reference", "entity_cross_reference", label, element_id=field_.element_id)
