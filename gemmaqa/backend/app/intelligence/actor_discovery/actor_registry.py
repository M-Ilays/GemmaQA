"""Actor Registry — the central, queryable catalog of discovered actors.

The surface the Planner talks to:

- `known_actors()` — confirmed + incomplete: actually-authenticated sessions
  with at least some corroboration.
- `unknown_actors()` — candidate role terms seen but not yet corroborated.
- `unverified_actors()` — a role NAME evidenced (a dropdown option, a table
  cell, a roles page) but never actually observed as an authenticated
  session under that identity.
- `actors_requiring_exploration()` — incomplete, then unverified, then
  candidate: most-promising-first targets for further exploration.
- `actors_missing_permissions()` — known actors with no (or only
  low-confidence) permission evidence yet.
- `actors_missing_dashboard_understanding()` — known actors for whom no
  dashboard-classified page has been reached yet.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.actor_discovery.actor_memory import ActorMemory
from app.intelligence.actor_discovery.role_relationships import ActorDifference, compare_actors
from app.intelligence.actor_discovery.schemas import ActorRecord

LOW_PERMISSION_CONFIDENCE = 0.3


class ActorRegistry:
    def __init__(self, memory: ActorMemory | None = None) -> None:
        self.memory = memory or ActorMemory()

    @property
    def records(self) -> dict[str, ActorRecord]:
        return self.memory.records

    def all_actors(self) -> list[ActorRecord]:
        return sorted(self.records.values(), key=lambda r: (-r.confidence, r.canonical_name))

    def get(self, term: str) -> ActorRecord | None:
        resolved = self.memory.resolve_term(term.strip().lower())
        return self.records.get(resolved) if resolved else None

    # -- Planner queries -----------------------------------------------------

    def known_actors(self) -> list[ActorRecord]:
        return [r for r in self.all_actors() if r.status in {"confirmed", "incomplete"}]

    def unknown_actors(self) -> list[ActorRecord]:
        return [r for r in self.all_actors() if r.status == "candidate"]

    def unverified_actors(self) -> list[ActorRecord]:
        return [r for r in self.all_actors() if r.status == "unverified"]

    def incomplete_actors(self) -> list[ActorRecord]:
        return [r for r in self.all_actors() if r.status == "incomplete"]

    def actors_requiring_exploration(self) -> list[ActorRecord]:
        return [*self.incomplete_actors(), *self.unverified_actors(), *self.unknown_actors()]

    def actors_missing_permissions(self) -> list[ActorRecord]:
        return [
            r
            for r in self.known_actors()
            if not r.known_permissions or all(p.confidence < LOW_PERMISSION_CONFIDENCE for p in r.known_permissions)
        ]

    def actors_missing_dashboard_understanding(self) -> list[ActorRecord]:
        return [r for r in self.known_actors() if not r.known_dashboards]

    # -- session infrastructure (no auto-switching — storage/lookup only) ----

    def session_for(self, term: str) -> Any | None:
        record = self.get(term)
        return record.session if record else None

    def all_sessions(self) -> dict[str, Any]:
        return {r.canonical_name: r.session for r in self.all_actors() if r.session is not None}

    # -- actor difference analysis --------------------------------------------

    def compare(self, term_a: str, term_b: str) -> ActorDifference | None:
        a, b = self.get(term_a), self.get(term_b)
        if a is None or b is None:
            return None
        return compare_actors(a, b)

    def all_pairwise_differences(self) -> list[ActorDifference]:
        actors = self.known_actors()
        diffs: list[ActorDifference] = []
        for i, a in enumerate(actors):
            for b in actors[i + 1 :]:
                diff = compare_actors(a, b)
                if diff.has_any_difference():
                    diffs.append(diff)
        return diffs

    # -- serialization / reporting ---------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "actor_count": len(self.records),
            "known": [r.canonical_name for r in self.known_actors()],
            "unverified": [r.canonical_name for r in self.unverified_actors()],
            "candidates": [r.canonical_name for r in self.unknown_actors()],
            "missing_permissions": [r.canonical_name for r in self.actors_missing_permissions()],
            "missing_dashboard_understanding": [r.canonical_name for r in self.actors_missing_dashboard_understanding()],
            "actors": [r.to_summary_dict() for r in self.known_actors()],
        }

    def to_dict(self) -> dict[str, Any]:
        return {term: record.model_dump(mode="json") for term, record in sorted(self.records.items())}
