"""Role relationship discovery — records OBSERVED structural relationships
between actors, and compares known actors' evidence to surface differences.

Deliberately narrow (matches the scope discipline already established for
app.intelligence.entity_discovery.entity_relationship_builder): no workflow
reconstruction, only structural, evidence-grounded patterns:

1. Co-assignment: two role terms observed in the SAME table cell/row (e.g.
   "Admin, Support" in one Users-table cell) -> `co_assigned_with` (multi-role
   assignment evidence).
2. Hierarchy via permission comparison: once two actors both have SOME
   observed permissions, if one's granted set is a strict superset of the
   other's, the superset actor is inferred `parent_of` the other (and the
   other `inherits_from` it) — comparative evidence, never a guess from
   names ("Admin" is not assumed senior to "Viewer" by name alone).

Actor Difference Analysis (`compare_actors`) is a pure, non-relationship
comparison: navigation/dashboards/permissions/pages/visibility differences
between two actors, for reporting and for hierarchy inference to build on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.intelligence.actor_discovery.actor_classifier import PageActorDiscovery
from app.intelligence.actor_discovery.schemas import ActorEvidence, ActorRecord

MIN_PERMISSIONS_FOR_HIERARCHY_COMPARISON = 2


@dataclass
class RoleRelationshipFinding:
    subject_term: str
    kind: str  # parent_of | inherits_from | co_assigned_with
    object_term: str
    evidence: ActorEvidence


@dataclass
class ActorDifference:
    actor_a: str
    actor_b: str
    navigation_only_in_a: set[str] = field(default_factory=set)
    navigation_only_in_b: set[str] = field(default_factory=set)
    dashboards_only_in_a: set[str] = field(default_factory=set)
    dashboards_only_in_b: set[str] = field(default_factory=set)
    pages_only_in_a: set[str] = field(default_factory=set)
    pages_only_in_b: set[str] = field(default_factory=set)
    permissions_only_in_a: set[str] = field(default_factory=set)
    permissions_only_in_b: set[str] = field(default_factory=set)
    entities_only_in_a: set[str] = field(default_factory=set)
    entities_only_in_b: set[str] = field(default_factory=set)
    visibility_only_in_a: set[str] = field(default_factory=set)
    visibility_only_in_b: set[str] = field(default_factory=set)

    def has_any_difference(self) -> bool:
        return any(
            [
                self.navigation_only_in_a, self.navigation_only_in_b,
                self.dashboards_only_in_a, self.dashboards_only_in_b,
                self.pages_only_in_a, self.pages_only_in_b,
                self.permissions_only_in_a, self.permissions_only_in_b,
                self.entities_only_in_a, self.entities_only_in_b,
                self.visibility_only_in_a, self.visibility_only_in_b,
            ]
        )

    def to_dict(self) -> dict:
        return {
            "actor_a": self.actor_a,
            "actor_b": self.actor_b,
            "navigation_only_in_a": sorted(self.navigation_only_in_a),
            "navigation_only_in_b": sorted(self.navigation_only_in_b),
            "dashboards_only_in_a": sorted(self.dashboards_only_in_a),
            "dashboards_only_in_b": sorted(self.dashboards_only_in_b),
            "pages_only_in_a": sorted(self.pages_only_in_a),
            "pages_only_in_b": sorted(self.pages_only_in_b),
            "permissions_only_in_a": sorted(self.permissions_only_in_a),
            "permissions_only_in_b": sorted(self.permissions_only_in_b),
            "entities_only_in_a": sorted(self.entities_only_in_a),
            "entities_only_in_b": sorted(self.entities_only_in_b),
            "visibility_only_in_a": sorted(self.visibility_only_in_a),
            "visibility_only_in_b": sorted(self.visibility_only_in_b),
        }


def compare_actors(a: ActorRecord, b: ActorRecord) -> ActorDifference:
    """Pure comparison — works on any two known actors regardless of how
    they were discovered (a live multi-session run, a Users-table listing
    multiple roles, or synthetic fixtures in a test)."""
    a_perms = set(a.granted_permission_ids())
    b_perms = set(b.granted_permission_ids())
    return ActorDifference(
        actor_a=a.canonical_name,
        actor_b=b.canonical_name,
        navigation_only_in_a=set(a.known_navigation) - set(b.known_navigation),
        navigation_only_in_b=set(b.known_navigation) - set(a.known_navigation),
        dashboards_only_in_a=set(a.known_dashboards) - set(b.known_dashboards),
        dashboards_only_in_b=set(b.known_dashboards) - set(a.known_dashboards),
        pages_only_in_a=set(a.known_pages) - set(b.known_pages),
        pages_only_in_b=set(b.known_pages) - set(a.known_pages),
        permissions_only_in_a=a_perms - b_perms,
        permissions_only_in_b=b_perms - a_perms,
        entities_only_in_a=set(a.known_entities) - set(b.known_entities),
        entities_only_in_b=set(b.known_entities) - set(a.known_entities),
        visibility_only_in_a=set(a.known_visibility) - set(b.known_visibility),
        visibility_only_in_b=set(b.known_visibility) - set(a.known_visibility),
    )


class RoleRelationshipBuilder:
    def build_co_assignments(
        self, discovery: PageActorDiscovery, *, iteration: int = 0
    ) -> list[RoleRelationshipFinding]:
        out: list[RoleRelationshipFinding] = []
        for term_a, term_b in discovery.co_assigned_pairs:
            evidence = ActorEvidence(
                source_kind="role_table_cell",
                observed_text=f"{term_a} + {term_b}",
                page_url=discovery.page_url,
                state_fingerprint=discovery.state_fingerprint,
                observed_at_iteration=iteration,
            )
            out.append(RoleRelationshipFinding(term_a, "co_assigned_with", term_b, evidence))
            out.append(RoleRelationshipFinding(term_b, "co_assigned_with", term_a, evidence.model_copy()))
        return out

    def infer_hierarchy(self, actors: list[ActorRecord]) -> list[RoleRelationshipFinding]:
        """Compare every pair of actors with enough observed permissions:
        a strict-superset granted-permission-set implies `parent_of`. Never
        infers anything from names — purely comparative."""
        out: list[RoleRelationshipFinding] = []
        eligible = [a for a in actors if len(a.known_permissions) >= MIN_PERMISSIONS_FOR_HIERARCHY_COMPARISON]
        for i, a in enumerate(eligible):
            for b in eligible[i + 1 :]:
                a_perms = set(a.granted_permission_ids())
                b_perms = set(b.granted_permission_ids())
                if not a_perms and not b_perms:
                    continue
                evidence = ActorEvidence(
                    source_kind="memory",
                    observed_text=f"permission comparison: {a.canonical_name} vs {b.canonical_name}",
                )
                if a_perms > b_perms:
                    out.append(RoleRelationshipFinding(a.canonical_name, "parent_of", b.canonical_name, evidence))
                    out.append(
                        RoleRelationshipFinding(b.canonical_name, "inherits_from", a.canonical_name, evidence.model_copy())
                    )
                elif b_perms > a_perms:
                    out.append(RoleRelationshipFinding(b.canonical_name, "parent_of", a.canonical_name, evidence))
                    out.append(
                        RoleRelationshipFinding(a.canonical_name, "inherits_from", b.canonical_name, evidence.model_copy())
                    )
        return out
