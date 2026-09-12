"""Permission discovery — infers what the CURRENT actor can and cannot do,
from observable evidence only.

No permission name is ever hardcoded as a business capability list. Every
permission id is built from the SAME generic operation-verb lexicon
app.intelligence.entity_discovery already uses ("Can Approve"/"Can
Export"/"Can Create Entity" are id PATTERNS this module derives, never a
fixed list). Evidence comes from four generic, application-neutral signals:

1. Visible + enabled controls -> positive evidence ("this actor CAN do X").
2. Visible + disabled controls -> negative evidence ("X is currently gated").
3. Reaching a settings/user-management/roles/permissions page -> positive
   evidence for the corresponding `can_access_*`/`can_manage_*` permission.
4. Network 401/403 responses, and generic "access denied" page text ->
   negative evidence for whatever operation/topic was being attempted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.intelligence.actor_discovery.actor_candidate_builder import ActorCandidateBuilder
from app.intelligence.actor_discovery.schemas import ActorEvidence, PermissionCandidate
from app.intelligence.entity_discovery.entity_candidate_builder import (
    HTTP_METHOD_OPERATIONS,
    NON_ENTITY_OPERATIONS,
    normalize_term,
    split_verb_and_noun,
    url_path_terms,
)

if TYPE_CHECKING:
    from app.perception.models import CanonicalPageModel

# Reachability alone is capability evidence for these page topics —
# independent of any button/verb on the page.
PAGE_TOPIC_PERMISSIONS: dict[str, tuple[str, str]] = {
    "is_settings_page": ("can_access_settings", "settings_page"),
    "is_user_management_page": ("can_manage_users", "user_management_page"),
    "is_roles_page": ("can_manage_roles", "roles_page"),
    "is_permissions_page": ("can_manage_permissions", "permissions_page"),
}


def _permission_id(operation: str, noun: str) -> str:
    noun = normalize_term(noun).replace(" ", "_")
    return f"can_{operation}_{noun}" if noun else f"can_{operation}"


class PermissionDiscovery:
    """Stateless: `discover()` returns fresh `PermissionCandidate`s for ONE
    observation; merging into an actor's accumulated `known_permissions` is
    `ActorMemory`'s job (evidence must never be lost across observations)."""

    def discover(self, model: "CanonicalPageModel", *, iteration: int = 0) -> dict[str, PermissionCandidate]:
        found: dict[str, PermissionCandidate] = {}

        def _get(pid: str) -> PermissionCandidate:
            return found.setdefault(pid, PermissionCandidate(permission_id=pid))

        def positive(pid: str, source_kind: str, text: str, element_id: str | None = None) -> None:
            _get(pid).positive_evidence.append(
                ActorEvidence(
                    source_kind=source_kind, observed_text=text[:160], page_url=model.url,
                    state_fingerprint=model.state_fingerprint or "", element_id=element_id,
                    observed_at_iteration=iteration,
                )
            )

        def negative(pid: str, source_kind: str, text: str, element_id: str | None = None) -> None:
            _get(pid).negative_evidence.append(
                ActorEvidence(
                    source_kind=source_kind, observed_text=text[:160], page_url=model.url,
                    state_fingerprint=model.state_fingerprint or "", element_id=element_id,
                    observed_at_iteration=iteration,
                )
            )

        self._from_page_reachability(model, positive)
        self._from_controls(model, positive, negative)
        self._from_network_evidence(model, negative)
        self._from_access_denied_text(model, negative)

        for pid, cand in found.items():
            total = len(cand.positive_evidence) + len(cand.negative_evidence)
            distinct_kinds = {e.source_kind for e in (cand.positive_evidence + cand.negative_evidence)}
            cand.confidence = min(1.0, 0.2 + 0.2 * len(distinct_kinds) + 0.05 * total)
        return found

    # -- signal 1: page reachability -----------------------------------------

    @staticmethod
    def _from_page_reachability(model: "CanonicalPageModel", positive) -> None:
        context = ActorCandidateBuilder.classify_page_context(model)
        for flag, (pid, source_kind) in PAGE_TOPIC_PERMISSIONS.items():
            if context.get(flag):
                positive(pid, source_kind, model.title or model.url)

    # -- signals 2: visible/disabled controls ---------------------------------

    @staticmethod
    def _from_controls(model: "CanonicalPageModel", positive, negative) -> None:
        for el in model.interactive_elements or []:
            if not el.is_visible:
                continue
            label = el.accessible_name or el.visible_text or el.text or ""
            if not label:
                continue
            verb, noun = split_verb_and_noun(label)
            if verb is None or verb in NON_ENTITY_OPERATIONS:
                continue
            pid = _permission_id(verb, noun)
            if el.is_enabled and not el.disabled:
                positive(pid, "visible_control", label, el.element_id)
            else:
                negative(pid, "disabled_control", label, el.element_id)

    # -- signal 3: network 401/403 -------------------------------------------

    @staticmethod
    def _from_network_evidence(model: "CanonicalPageModel", negative) -> None:
        for entry in model.network_evidence or []:
            if entry.http_status not in (401, 403):
                continue
            method = (entry.method or "").upper()
            operation = HTTP_METHOD_OPERATIONS.get(method)
            if operation is None:
                continue
            text = entry.text or ""
            parts = text.split(" ", 1)
            entry_url = parts[1] if len(parts) == 2 else text
            terms = url_path_terms(entry_url)
            noun = terms[-1] if terms else ""
            negative(_permission_id(operation, noun), "network_403" if entry.http_status == 403 else "network_401", text)

    # -- signal 4: generic "access denied" page text -------------------------

    @staticmethod
    def _from_access_denied_text(model: "CanonicalPageModel", negative) -> None:
        if not ActorCandidateBuilder.detect_access_denied(model):
            return
        terms = url_path_terms(model.url)
        noun = terms[-1] if terms else ""
        pid = f"can_access_{normalize_term(noun).replace(' ', '_')}" if noun else "can_access_this_page"
        negative(pid, "access_denied_page", model.title or model.url)
