"""Autonomous Entity Discovery Engine.

Discovers the application's business entities (whatever they happen to be —
this package contains NO application-specific entity names) from the
structured evidence GemmaQA already collects: the Canonical Page Model
(navigation, headings, breadcrumbs, tabs, forms, tables, dialogs, buttons,
links), URLs, page titles, network/API evidence, and accumulated run memory.

See docs/ENTITY_DISCOVERY_ENGINE.md.
"""

from app.intelligence.entity_discovery.entity_discovery_engine import EntityDiscoveryEngine
from app.intelligence.entity_discovery.entity_registry import EntityRegistry
from app.intelligence.entity_discovery.schemas import (
    EntityEvidence,
    EntityRecord,
    EntityRelationship,
)

__all__ = [
    "EntityDiscoveryEngine",
    "EntityRegistry",
    "EntityEvidence",
    "EntityRecord",
    "EntityRelationship",
]
