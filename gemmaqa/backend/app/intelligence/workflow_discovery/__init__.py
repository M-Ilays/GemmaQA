"""Autonomous Workflow Discovery and Reconstruction Engine.

Reconstructs business workflows — actor performs action on entity, causing a
state transition, producing an outcome that may require another actor — from
accumulated evidence: CanonicalPageModel observations, before/after state
transitions, the Entity Registry, the Actor Registry, and exploration
history. Contains NO application-specific workflow/entity/actor names; every
name comes from the observed application alone.

This milestone discovers and reconstructs workflow STRUCTURE only. It does
NOT validate KPI dependencies, switch actors automatically, execute full
scenarios, or generate QA test strategy — see docs/WORKFLOW_DISCOVERY_ENGINE.md.
"""

from app.intelligence.workflow_discovery.workflow_discovery_engine import WorkflowDiscoveryEngine
from app.intelligence.workflow_discovery.workflow_registry import WorkflowRegistry
from app.intelligence.workflow_discovery.schemas import WorkflowDescriptor, WorkflowEvidence

__all__ = ["WorkflowDiscoveryEngine", "WorkflowRegistry", "WorkflowDescriptor", "WorkflowEvidence"]
