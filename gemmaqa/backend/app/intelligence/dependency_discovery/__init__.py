"""Autonomous Business Dependency Discovery Engine.

Determines how visible application outputs (KPIs, counters, badges, charts,
reports, queues) are produced, changed, consumed, and verified — connecting
them to candidate entities, workflows, and actors already discovered by
`app.intelligence.entity_discovery`/`actor_discovery`/`workflow_discovery`.
Contains NO application-specific KPI/entity/actor/workflow names; every name
comes from the observed application alone.

This milestone discovers and represents dependency STRUCTURE and CANDIDATE
verification plans only. It does NOT switch actors automatically, execute
multi-role scenarios, run destructive dependency experiments, create
unrestricted data, or execute speculative dependencies without evidence —
see docs/BUSINESS_DEPENDENCY_DISCOVERY_ENGINE.md.
"""

from app.intelligence.dependency_discovery.dependency_discovery_engine import DependencyDiscoveryEngine
from app.intelligence.dependency_discovery.dependency_registry import DependencyRegistry
from app.intelligence.dependency_discovery.schemas import DependencyDescriptor, DependencyEvidence

__all__ = ["DependencyDiscoveryEngine", "DependencyRegistry", "DependencyDescriptor", "DependencyEvidence"]
