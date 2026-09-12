"""Entity Registry — the central, queryable catalog of discovered entities.

This is the surface the Planner (and reporting) talks to:

- `known_entities()` — confirmed entities (multi-source corroboration).
- `unknown_entities()` — candidate terms seen but not yet corroborated
  enough to be confirmed entities.
- `incomplete_entities()` — confirmed entities with no discovered
  operations or no related pages yet.
- `entities_requiring_exploration()` — incomplete + candidate entities,
  ordered by confidence descending: the most promising things to point
  further exploration at.
"""

from __future__ import annotations

from typing import Any

from app.intelligence.entity_discovery.entity_memory import EntityMemory
from app.intelligence.entity_discovery.schemas import EntityRecord


class EntityRegistry:
    def __init__(self, memory: EntityMemory | None = None) -> None:
        self.memory = memory or EntityMemory()

    # -- basic access ------------------------------------------------------

    @property
    def records(self) -> dict[str, EntityRecord]:
        return self.memory.records

    def all_entities(self) -> list[EntityRecord]:
        return sorted(self.records.values(), key=lambda r: (-r.confidence, r.canonical_name))

    def get(self, term: str) -> EntityRecord | None:
        resolved = self.memory.resolve_term(term.strip().lower())
        return self.records.get(resolved) if resolved else None

    # -- Planner queries -----------------------------------------------------

    def known_entities(self) -> list[EntityRecord]:
        return [r for r in self.all_entities() if r.status in {"confirmed", "incomplete"}]

    def unknown_entities(self) -> list[EntityRecord]:
        """Seen-but-unconfirmed candidate terms — possibly real entities the
        run hasn't corroborated yet, possibly noise."""
        return [r for r in self.all_entities() if r.status == "candidate"]

    def incomplete_entities(self) -> list[EntityRecord]:
        return [r for r in self.all_entities() if r.status == "incomplete"]

    def entities_requiring_exploration(self) -> list[EntityRecord]:
        """What further exploration would pay off on, most-promising first:
        confirmed-but-thin entities before uncorroborated candidates."""
        incomplete = self.incomplete_entities()
        candidates = self.unknown_entities()
        return [*incomplete, *candidates]

    # -- serialization / reporting ---------------------------------------------

    def summary(self) -> dict[str, Any]:
        """Compact snapshot for traces, prompts, and reports."""
        return {
            "entity_count": len(self.records),
            "confirmed": [r.canonical_name for r in self.all_entities() if r.status == "confirmed"],
            "incomplete": [r.canonical_name for r in self.incomplete_entities()],
            "candidates": [r.canonical_name for r in self.unknown_entities()],
            "entities": [r.to_summary_dict() for r in self.known_entities()],
        }

    def to_dict(self) -> dict[str, Any]:
        return {term: record.model_dump(mode="json") for term, record in sorted(self.records.items())}
