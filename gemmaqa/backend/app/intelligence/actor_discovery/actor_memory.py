"""Actor memory — persistence and merging of discovered actors, permissions,
and sessions across observations.

Owns the term -> ActorRecord store (mirrors
app.intelligence.entity_discovery.entity_memory's design). Every page
observation's findings are MERGED here — same normalized term -> same
record, evidence/aliases/permissions/pages/navigation/dashboards accumulate,
nothing is ever discarded. Confidence is recomputed from the full merged
evidence set on every observation using the same "distinct source kinds
corroborate more than repetition" principle as entity confidence scoring.
"""

from __future__ import annotations

from datetime import datetime

from app.intelligence.actor_discovery.actor_candidate_builder import role_modifier_flags
from app.intelligence.actor_discovery.actor_classifier import ActorFinding, PageActorDiscovery
from app.intelligence.actor_discovery.role_relationships import RoleRelationshipFinding
from app.intelligence.actor_discovery.schemas import (
    ActorEvidence,
    ActorRecord,
    ActorSession,
    PermissionCandidate,
    RoleRelationship,
)

MAX_EVIDENCE_PER_ACTOR = 60
MAX_ALIASES_PER_ACTOR = 12

# Corroboration weights for actor/role evidence — the strongest signals are
# the ones that DIRECTLY name a role as a role (a dropdown option, a table
# cell in a roles/users context, a dedicated roles/permissions page);
# incidental text mentions are weakest.
SOURCE_KIND_WEIGHTS: dict[str, float] = {
    "role_dropdown_option": 0.35,
    "role_table_cell": 0.30,
    "roles_page": 0.25,
    "permissions_page": 0.25,
    "user_management_page": 0.20,
    "settings_page": 0.15,
    "user_creation_form": 0.20,
    "invite_dialog": 0.20,
    "assignment_dialog": 0.15,
    "approval_dialog": 0.15,
    "navigation_item": 0.20,
    "breadcrumb": 0.15,
    "profile_menu": 0.15,
    "account_page": 0.15,
    "session_info": 0.10,
    "network_403": 0.15,
    "network_401": 0.15,
    "access_denied_page": 0.10,
    "workflow_ownership": 0.15,
    "visible_control": 0.05,
    "disabled_control": 0.05,
    "visible_text": 0.05,
    "jwt_claim": 0.25,
    "auth_response": 0.20,
    "entity_registry": 0.05,
    "memory": 0.05,
}

MIN_DISTINCT_SOURCES_CONFIRMED = 2
CONFIRMED_MIN_CONFIDENCE = 0.4


def score_confidence(evidence: list[ActorEvidence]) -> float:
    seen: set[str] = set()
    total = 0.0
    for ev in evidence:
        weight = SOURCE_KIND_WEIGHTS.get(ev.source_kind, 0.05)
        if ev.source_kind in seen:
            total += weight * 0.15
        else:
            total += weight
            seen.add(ev.source_kind)
    return min(1.0, total)


def status_for(record: ActorRecord) -> str:
    """`unverified` (named/evidenced but never an actual observed session) is
    the actor-specific addition to entity discovery's
    candidate/incomplete/confirmed lifecycle.

    An actually-OBSERVED session (this actor was really browsing — anonymous
    or authenticated, it doesn't matter which) is judged by SESSION richness
    (pages/permissions/dashboards), not by named-role evidence — the common
    "current session" placeholder never has any `role_dropdown_option`-style
    evidence at all (most applications never display their own role name
    anywhere), and must not be stuck at `candidate` forever just because of
    that. An anonymous visitor is a legitimate actor too (the task's own
    "guest role" concept) — live-verified on a public, unauthenticated demo
    dashboard, which must reach `confirmed` exactly like an authenticated
    session once it has real permission/dashboard evidence, not stay stuck
    at `candidate` merely for lacking a login."""
    was_observed_session = bool(record.known_session_types) and bool(record.known_pages)
    if was_observed_session:
        if not record.known_permissions and not record.known_dashboards:
            return "incomplete"
        return "confirmed"
    distinct_kinds = {ev.source_kind for ev in record.supporting_evidence}
    well_evidenced = len(distinct_kinds) >= MIN_DISTINCT_SOURCES_CONFIRMED and record.confidence >= CONFIRMED_MIN_CONFIDENCE
    return "unverified" if well_evidenced else "candidate"


def session_richness_bonus(record: ActorRecord) -> float:
    """Confidence in a SESSION actor (as opposed to a named role) comes from
    how much is actually known about it, not from evidence-kind diversity —
    a session bucket like "current session" only ever carries one evidence
    kind ("session_info") no matter how much real data has accumulated.
    Live-verified: without this, a fully explored, "confirmed" actor with
    real permissions/dashboard/entities/pages still reported ~0.1 confidence,
    understating how much was actually learned about it."""
    bonus = 0.0
    if record.known_permissions:
        bonus += 0.15
    if record.known_dashboards:
        bonus += 0.15
    if record.known_entities:
        bonus += 0.1
    bonus += min(0.2, 0.05 * max(0, len(record.known_pages) - 1))
    return bonus


class ActorMemory:
    def __init__(self) -> None:
        self.records: dict[str, ActorRecord] = {}

    # -- lookup ---------------------------------------------------------------

    def resolve_term(self, term: str) -> str | None:
        return term if term in self.records else None

    def get_or_create(self, term: str, *, now: datetime | None = None, iteration: int = 0) -> ActorRecord:
        record = self.records.get(term)
        if record is None:
            now = now or datetime.utcnow()
            record = ActorRecord(canonical_name=term, first_seen=now, first_seen_iteration=iteration)
            self.records[term] = record
        return record

    # -- merging ------------------------------------------------------------

    def merge_discovery(self, discovery: PageActorDiscovery, *, iteration: int = 0) -> dict[str, list[str]]:
        changes: dict[str, list[str]] = {"created": [], "updated": [], "session_updated": []}
        now = datetime.utcnow()

        for term, finding in sorted(discovery.findings.items()):
            existed = term in self.records
            record = self.get_or_create(term, now=now, iteration=iteration)
            self._merge_finding_evidence(record, finding, now=now, iteration=iteration)
            changes["created" if not existed else "updated"].append(term)

        # Session-level evidence (pages/nav/dashboards/permissions/login
        # method) always attaches to the CURRENT session's actor — the
        # placeholder "current session" unless this page named a specific
        # role for it (see ActorClassifier).
        session_term = discovery.current_session_term
        existed = session_term in self.records
        session_actor = self.get_or_create(session_term, now=now, iteration=iteration)
        self._merge_session_evidence(session_actor, discovery, now=now, iteration=iteration)
        bucket = "created" if not existed else "updated"
        if session_term not in changes[bucket]:
            changes[bucket].append(session_term)
        changes["session_updated"].append(session_term)
        return changes

    def _merge_finding_evidence(
        self, record: ActorRecord, finding: ActorFinding, *, now: datetime, iteration: int
    ) -> None:
        seen = {(ev.source_kind, ev.observed_text, ev.page_url) for ev in record.supporting_evidence}
        for ev in finding.evidence:
            key = (ev.source_kind, ev.observed_text, ev.page_url)
            if key in seen or len(record.supporting_evidence) >= MAX_EVIDENCE_PER_ACTOR:
                continue
            seen.add(key)
            record.supporting_evidence.append(ev)

        for alias in sorted(finding.aliases):
            if alias not in record.aliases and alias.lower() != record.canonical_name:
                if len(record.aliases) < MAX_ALIASES_PER_ACTOR:
                    record.aliases.append(alias)

        flags = role_modifier_flags(record.canonical_name)
        record.is_guest = record.is_guest or flags["is_guest"]
        record.is_system = record.is_system or flags["is_system"]
        record.is_default = record.is_default or flags["is_default"]
        record.is_temporary = record.is_temporary or flags["is_temporary"]

        record.confidence = score_confidence(record.supporting_evidence)
        record.status = status_for(record)
        record.last_seen = now
        record.last_seen_iteration = iteration

    def _merge_session_evidence(
        self, record: ActorRecord, discovery: PageActorDiscovery, *, now: datetime, iteration: int
    ) -> None:
        # A session actor (most commonly the "current session" placeholder,
        # which is never itself named by role_dropdown/table-cell evidence)
        # still needs its OWN evidence trail so confidence reflects how much
        # is actually known about it, not just whether its name was observed.
        session_evidence = ActorEvidence(
            source_kind="session_info",
            observed_text=f"session observed on {discovery.page_url}"[:160],
            page_url=discovery.page_url,
            state_fingerprint=discovery.state_fingerprint,
            observed_at_iteration=iteration,
        )
        seen = {(ev.source_kind, ev.observed_text, ev.page_url) for ev in record.supporting_evidence}
        key = (session_evidence.source_kind, session_evidence.observed_text, session_evidence.page_url)
        if key not in seen and len(record.supporting_evidence) < MAX_EVIDENCE_PER_ACTOR:
            record.supporting_evidence.append(session_evidence)

        if discovery.page_url and discovery.page_url not in record.known_pages:
            record.known_pages.append(discovery.page_url)
        for item in sorted(discovery.navigation_items):
            if item not in record.known_navigation:
                record.known_navigation.append(item)
        if discovery.is_dashboard and discovery.page_url not in record.known_dashboards:
            record.known_dashboards.append(discovery.page_url)
        if discovery.is_authenticated_context and not record.known_home_pages:
            record.known_home_pages.append(discovery.page_url)
        for region in sorted(discovery.visible_regions):
            if region not in record.known_visibility:
                record.known_visibility.append(region)
        for target in sorted(discovery.assignment_targets):
            if target not in record.known_assignment_types:
                record.known_assignment_types.append(target)
        if discovery.login_method and discovery.login_method not in record.known_login_methods:
            record.known_login_methods.append(discovery.login_method)
        session_type = "authenticated" if discovery.is_authenticated_context else "anonymous"
        if session_type not in record.known_session_types:
            record.known_session_types.append(session_type)

        by_id = {p.permission_id: p for p in record.known_permissions}
        for pid, incoming in sorted(discovery.permissions.items()):
            existing = by_id.get(pid)
            if existing is None:
                existing = PermissionCandidate(permission_id=pid)
                record.known_permissions.append(existing)
                by_id[pid] = existing
            self._merge_permission(existing, incoming)

        record.last_seen = now
        record.last_seen_iteration = iteration
        self.finalize(record)

        record.session = self._merge_session_object(record, discovery, iteration=iteration, now=now)

    def finalize(self, record: ActorRecord) -> None:
        """Recompute confidence/status from the CURRENT full record state.
        Called after session-evidence merging, and again by the engine after
        entity cross-referencing (`known_entities` is attached AFTER
        `merge_discovery` returns, so it must be re-applied then too — see
        ActorDiscoveryEngine._attach_entities)."""
        record.confidence = min(1.0, score_confidence(record.supporting_evidence) + session_richness_bonus(record))
        record.status = status_for(record)

    @staticmethod
    def _merge_permission(existing: PermissionCandidate, incoming: PermissionCandidate) -> None:
        seen_pos = {(e.source_kind, e.observed_text, e.page_url) for e in existing.positive_evidence}
        for ev in incoming.positive_evidence:
            key = (ev.source_kind, ev.observed_text, ev.page_url)
            if key not in seen_pos:
                seen_pos.add(key)
                existing.positive_evidence.append(ev)
        seen_neg = {(e.source_kind, e.observed_text, e.page_url) for e in existing.negative_evidence}
        for ev in incoming.negative_evidence:
            key = (ev.source_kind, ev.observed_text, ev.page_url)
            if key not in seen_neg:
                seen_neg.add(key)
                existing.negative_evidence.append(ev)
        distinct = {e.source_kind for e in (existing.positive_evidence + existing.negative_evidence)}
        total = len(existing.positive_evidence) + len(existing.negative_evidence)
        existing.confidence = min(1.0, 0.2 + 0.2 * len(distinct) + 0.05 * total)

    @staticmethod
    def _merge_session_object(
        record: ActorRecord, discovery: PageActorDiscovery, *, iteration: int, now: datetime
    ) -> ActorSession:
        session = record.session or ActorSession(actor_id=record.actor_id)
        session.actor_id = record.actor_id
        if discovery.login_method and not session.login_method:
            session.login_method = discovery.login_method
        if discovery.is_authenticated_context and not session.landing_page:
            session.landing_page = discovery.page_url
        if discovery.is_dashboard and not session.dashboard_url:
            session.dashboard_url = discovery.page_url
        session.known_permission_ids = record.granted_permission_ids()
        session.last_explored_url = discovery.page_url
        session.last_state_fingerprint = discovery.state_fingerprint
        session.last_updated_iteration = iteration
        session.updated_at = now
        return session

    # -- relationships --------------------------------------------------------

    def merge_relationships(self, findings: list[RoleRelationshipFinding]) -> list[str]:
        traced: list[str] = []
        for finding in findings:
            subject_term = self.resolve_term(finding.subject_term)
            object_term = self.resolve_term(finding.object_term)
            if subject_term is None or object_term is None or subject_term == object_term:
                continue
            subject = self.records[subject_term]
            object_record = self.records[object_term]
            existing = next(
                (
                    r
                    for r in subject.relationships
                    if r.kind == finding.kind and r.object_actor_id == object_record.actor_id
                ),
                None,
            )
            if existing is None:
                subject.relationships.append(
                    RoleRelationship(
                        subject_actor_id=subject.actor_id,
                        kind=finding.kind,
                        object_actor_id=object_record.actor_id,
                        evidence=[finding.evidence],
                        confidence=0.4,
                    )
                )
                traced.append(f"{subject_term} {finding.kind} {object_term}")
            else:
                keys = {(e.source_kind, e.observed_text, e.page_url) for e in existing.evidence}
                key = (finding.evidence.source_kind, finding.evidence.observed_text, finding.evidence.page_url)
                if key not in keys:
                    existing.evidence.append(finding.evidence)
                    existing.confidence = min(1.0, 0.4 + 0.15 * len(existing.evidence))
        return traced
