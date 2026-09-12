"""Autonomous Actor Discovery Engine.

Discovers every actor participating in the application — any human or system
identity that owns permissions, creates/modifies data, approves workflows, or
consumes information — from structural evidence GemmaQA already collects:
the Canonical Page Model (navigation, settings/roles/permission pages, role
dropdowns, user-creation/invite/assignment/approval dialogs, disabled
controls), the Entity Registry, network 401/403 evidence, and accumulated
run/auth memory.

Contains NO application-specific actor/role names (no "Admin", "Manager",
"Driver", ...) — every name comes from the observed application alone. See
docs/ACTOR_DISCOVERY_ENGINE.md.
"""

from app.intelligence.actor_discovery.actor_discovery_engine import ActorDiscoveryEngine
from app.intelligence.actor_discovery.actor_registry import ActorRegistry
from app.intelligence.actor_discovery.schemas import ActorEvidence, ActorRecord, ActorSession

__all__ = [
    "ActorDiscoveryEngine",
    "ActorRegistry",
    "ActorEvidence",
    "ActorRecord",
    "ActorSession",
]
