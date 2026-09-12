"""Deterministic node identity — every function here is a pure string
formatter, no state. Given the SAME source registry record, it always
returns the SAME `node_id`, which is what makes graph synchronisation
idempotent and stable-identity-preserving across pages/sessions/runs.

Cross-registry identity is largely already solved upstream: Entity/Actor/
Workflow Discovery already reference each other by canonical-name TERM
strings (`WorkflowStep.entity_id == "job"`, not an internal UUID) — so
using the SAME term to build the entity/actor/workflow node id here means
a reference from ANY registry resolves to the SAME node without fuzzy
matching. Only outputs/steps/transitions/etc. (which have no shared-across-
registries term, only a registry-internal UUID) use that UUID directly.
"""

from __future__ import annotations

from app.intelligence.entity_discovery.entity_candidate_builder import normalize_term

# Output types that map onto a more specific node type than the generic
# "derived_output" bucket -- suggested, not exhaustive (task section 6:
# node types must stay extensible).
_OUTPUT_TYPE_TO_NODE_TYPE = {
    "kpi_card": "counter",
    "counter": "counter",
    "badge": "badge",
    "chart_total": "chart",
    "chart_series": "chart",
    "chart_segment": "chart",
    "report_summary": "report",
    "queue_count": "queue",
    "notification_count": "notification",
    "alert_count": "alert",
}


def node_type_for_output(output_type: str) -> str:
    return _OUTPUT_TYPE_TO_NODE_TYPE.get(output_type, "derived_output")


def entity_node_id(canonical_name: str) -> str:
    return f"entity:{normalize_term(canonical_name)}"


def entity_state_node_id(entity_canonical_name: str, state_label: str) -> str:
    return f"entity_state:{normalize_term(entity_canonical_name)}:{normalize_term(state_label)}"


def actor_node_id(canonical_name: str) -> str:
    return f"actor:{normalize_term(canonical_name)}"


def permission_node_id(actor_canonical_name: str, permission_id: str) -> str:
    return f"permission:{normalize_term(actor_canonical_name)}:{permission_id}"


def operation_node_id(entity_canonical_name: str, operation_verb: str) -> str:
    return f"operation:{normalize_term(entity_canonical_name)}:{operation_verb}"


def workflow_node_id(workflow_canonical_name: str) -> str:
    return f"workflow:{normalize_term(workflow_canonical_name)}"


def workflow_step_node_id(step_id: str) -> str:
    return f"workflow_step:{step_id}"


def workflow_branch_node_id(branch_id: str) -> str:
    return f"workflow_branch:{branch_id}"


def prerequisite_node_id(prerequisite_id: str) -> str:
    return f"prerequisite:{prerequisite_id}"


def trigger_node_id(trigger_id: str) -> str:
    return f"trigger:{trigger_id}"


def outcome_node_id(outcome_id: str) -> str:
    return f"outcome:{outcome_id}"


def transition_node_id(transition_id: str) -> str:
    return f"transition:{transition_id}"


def derived_output_node_id(output_id: str) -> str:
    return f"derived_output:{output_id}"


def page_node_id(url: str) -> str:
    return f"page:{url}"


def unknown_node_id(kind: str, identifier: str) -> str:
    return f"unknown:{kind}:{identifier}"


def collection_node_id(collection_id: str) -> str:
    return f"table:{collection_id}"


def crud_form_node_id(form_id: str) -> str:
    return f"form:{form_id}"


def crud_control_node_id(element_id: str) -> str:
    return f"unknown:crud_control:{element_id}"
