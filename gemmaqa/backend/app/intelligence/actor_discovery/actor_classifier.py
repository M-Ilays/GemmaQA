"""Actor classification — turns one page's raw actor/role candidates into
structured findings, determines which known actor (or the generic "current
session" placeholder — see below) this observation's session-level evidence
belongs to, and folds in the page-context signals (dashboard/profile/
navigation/visible regions/assignment targets) that permission_discovery's
per-permission evidence alone doesn't carry.

"current session" placeholder: many applications never display their own
role name anywhere in the UI (confirmed live — see
docs/ACTOR_DISCOVERY_ENGINE.md). Session-level evidence (pages visited,
navigation seen, dashboards reached, permissions inferred, login method)
must still be tracked somewhere even when no role NAME is ever observed —
"current session" is a structural bucket for exactly that, never a
business role name. If a profile/account page names exactly one role-ish
term, that term is used instead (best-effort — see Known limitations).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.intelligence.actor_discovery.actor_candidate_builder import ActorCandidateBuilder
from app.intelligence.actor_discovery.permission_discovery import PermissionDiscovery
from app.intelligence.actor_discovery.schemas import ActorEvidence, PermissionCandidate

if TYPE_CHECKING:
    from app.perception.models import CanonicalPageModel

CURRENT_SESSION_TERM = "current session"


@dataclass
class ActorFinding:
    term: str
    evidence: list[ActorEvidence] = field(default_factory=list)
    aliases: set[str] = field(default_factory=set)


@dataclass
class PageActorDiscovery:
    page_url: str
    state_fingerprint: str
    findings: dict[str, ActorFinding] = field(default_factory=dict)
    current_session_term: str = CURRENT_SESSION_TERM
    permissions: dict[str, PermissionCandidate] = field(default_factory=dict)
    navigation_items: set[str] = field(default_factory=set)
    is_dashboard: bool = False
    is_authenticated_context: bool = False
    login_method: str | None = None
    visible_regions: set[str] = field(default_factory=set)
    assignment_targets: set[str] = field(default_factory=set)
    # Co-assignment pairs observed in the SAME cell/row (e.g. "Admin, Support"
    # in one Users-table cell) — multi-role assignment evidence.
    co_assigned_pairs: list[tuple[str, str]] = field(default_factory=list)


class ActorClassifier:
    def __init__(self) -> None:
        self.candidate_builder = ActorCandidateBuilder()
        self.permission_discovery = PermissionDiscovery()

    def classify(
        self,
        model: "CanonicalPageModel",
        *,
        iteration: int = 0,
        authenticated: bool = False,
        login_method: str | None = None,
        known_role_terms: frozenset[str] = frozenset(),
    ) -> PageActorDiscovery:
        discovery = PageActorDiscovery(page_url=model.url, state_fingerprint=model.state_fingerprint or "")
        discovery.is_authenticated_context = authenticated
        discovery.login_method = login_method

        candidates = self.candidate_builder.build(model, iteration=iteration, known_role_terms=known_role_terms)
        for cand in candidates:
            finding = discovery.findings.setdefault(cand.term, ActorFinding(term=cand.term))
            finding.evidence.append(cand.evidence)
            finding.aliases.add(cand.raw_term)

        # Co-assignment: multiple role terms that came from the SAME table
        # cell (i.e. the same element_id, source_kind "role_table_cell") were
        # observed together — direct multi-role evidence.
        by_cell: dict[tuple[str, str], list[str]] = {}
        for cand in candidates:
            if cand.evidence.source_kind == "role_table_cell":
                key = (cand.evidence.element_id or "", cand.evidence.page_url)
                by_cell.setdefault(key, []).append(cand.term)
        for terms in by_cell.values():
            unique = sorted(set(terms))
            for i, a in enumerate(unique):
                for b in unique[i + 1 :]:
                    discovery.co_assigned_pairs.append((a, b))

        context = self.candidate_builder.classify_page_context(model)
        discovery.is_dashboard = context.get("is_dashboard_page", False)

        for region in model.navigation_regions or []:
            for item in region.items or []:
                if item.text:
                    discovery.navigation_items.add(item.text.strip())

        for region in model.regions or []:
            if region.region_type and region.region_type != "unknown_region":
                discovery.visible_regions.add(region.region_type)

        for cand in self.candidate_builder.assignment_targets(model, iteration=iteration):
            discovery.assignment_targets.add(cand.term)

        discovery.permissions = self.permission_discovery.discover(model, iteration=iteration)

        # Best-effort: a profile/account page naming EXACTLY one role-ish
        # term is treated as this session's own role. Ambiguous otherwise —
        # deliberately conservative rather than guessing among several.
        if context.get("is_profile_page") and len(discovery.findings) == 1:
            discovery.current_session_term = next(iter(discovery.findings))

        return discovery
