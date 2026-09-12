"""Bounded, deterministic graph inference — the ONLY 7 rules this milestone
is allowed to run (task section 15). Every inferred edge records which rule
produced it, which node/edge ids it depended on, and a confidence capped at
its weakest premise (see `graph_confidence.inferred_edge_confidence`).

No open-ended LLM inference, no arbitrary business-rule derivation from
graph proximity — each rule below is a fixed, one-hop, explainable pattern
match. If a directly-synced edge already covers what a rule would produce
(same edge_id), the rule is a no-op there — it only fills gaps the direct
registry projection didn't already cover.
"""

from __future__ import annotations

from app.intelligence.knowledge_graph import graph_confidence
from app.intelligence.knowledge_graph.graph_edge_factory import edge_id
from app.intelligence.knowledge_graph.knowledge_graph_memory import KnowledgeGraphMemory
from app.intelligence.knowledge_graph.schemas import GraphInference

RULE_A_ACTOR_WORKFLOW_PARTICIPATION = "A_actor_workflow_participation"
RULE_B_WORKFLOW_ENTITY_INVOLVEMENT = "B_workflow_entity_involvement"
RULE_C_PERMISSION_CAPABILITY = "C_permission_capability"
RULE_D_COMPATIBLE_CONTINUATION = "D_compatible_continuation"
RULE_E_DEPENDENCY_PROPAGATION = "E_dependency_propagation"
RULE_F_CONSUMER_VISIBILITY = "F_consumer_visibility"
RULE_G_CROSS_ROLE_HANDOFF = "G_cross_role_handoff"


class GraphInferenceEngine:
    def __init__(self, memory: KnowledgeGraphMemory) -> None:
        self.memory = memory

    def run_all(self, *, iteration: int = 0) -> dict[str, int]:
        counts = {
            RULE_A_ACTOR_WORKFLOW_PARTICIPATION: self._rule_a(iteration),
            RULE_B_WORKFLOW_ENTITY_INVOLVEMENT: self._rule_b(iteration),
            RULE_C_PERMISSION_CAPABILITY: self._rule_c(iteration),
            RULE_D_COMPATIBLE_CONTINUATION: self._rule_d(iteration),
            RULE_E_DEPENDENCY_PROPAGATION: self._rule_e(iteration),
            RULE_F_CONSUMER_VISIBILITY: self._rule_f(iteration),
            RULE_G_CROSS_ROLE_HANDOFF: self._rule_g(iteration),
        }
        return counts

    def _record_inference(self, target_edge_id: str, rule_id: str, *, input_node_ids: list[str], input_edge_ids: list[str], confidence: float, iteration: int) -> None:
        premise_confidences = [round(self.memory.edges[e].confidence, 3) for e in input_edge_ids if e in self.memory.edges]
        self.memory.add_inference(
            GraphInference(
                edge_id=target_edge_id, rule_id=rule_id, input_node_ids=input_node_ids, input_edge_ids=input_edge_ids,
                confidence_derivation=f"{rule_id}: min({premise_confidences}) decayed by depth -> {round(confidence, 3)}",
                graph_version=self.memory.graph_version,
            )
        )

    # -- A: actor-step performed_by + workflow-step has_step -> participates_in --

    def _rule_a(self, iteration: int) -> int:
        created = 0
        for edge in list(self.memory.edges.values()):
            if edge.edge_type != "performed_by" or edge.stale:
                continue
            step_node_id, actor_node_id = edge.source_node_id, edge.target_node_id
            has_step_edges = [e for e in self.memory.incoming_edge_ids(step_node_id) if self.memory.edges[e].edge_type == "has_step"]
            for hs_edge_id in has_step_edges:
                wf_node_id = self.memory.edges[hs_edge_id].source_node_id
                new_edge_id = edge_id("participates_in", actor_node_id, wf_node_id)
                if new_edge_id in self.memory.edges and not self.memory.edges[new_edge_id].stale:
                    continue
                premises = [edge.confidence, self.memory.edges[hs_edge_id].confidence]
                conf = graph_confidence.inferred_edge_confidence(premises, depth=2)
                self.memory.upsert_edge(
                    new_edge_id, edge_type="participates_in", source_node_id=actor_node_id, target_node_id=wf_node_id,
                    status="inferred", confidence=conf, source_registry="graph_inference", source_record_ids=[edge.edge_id, hs_edge_id], iteration=iteration,
                )
                self._record_inference(new_edge_id, RULE_A_ACTOR_WORKFLOW_PARTICIPATION, input_node_ids=[actor_node_id, wf_node_id, step_node_id], input_edge_ids=[edge.edge_id, hs_edge_id], confidence=conf, iteration=iteration)
                created += 1
        return created

    # -- B: step-entity acts_on + workflow-step has_step -> workflow acts_on entity --

    def _rule_b(self, iteration: int) -> int:
        created = 0
        for edge in list(self.memory.edges.values()):
            if edge.edge_type != "acts_on" or edge.stale:
                continue
            source_node = self.memory.nodes.get(edge.source_node_id)
            if source_node is None or source_node.node_type != "workflow_step":
                continue
            step_node_id, entity_node_id = edge.source_node_id, edge.target_node_id
            has_step_edges = [e for e in self.memory.incoming_edge_ids(step_node_id) if self.memory.edges[e].edge_type == "has_step"]
            for hs_edge_id in has_step_edges:
                wf_node_id = self.memory.edges[hs_edge_id].source_node_id
                new_edge_id = edge_id("acts_on", wf_node_id, entity_node_id)
                if new_edge_id in self.memory.edges and not self.memory.edges[new_edge_id].stale:
                    continue
                premises = [edge.confidence, self.memory.edges[hs_edge_id].confidence]
                conf = graph_confidence.inferred_edge_confidence(premises, depth=2)
                self.memory.upsert_edge(
                    new_edge_id, edge_type="acts_on", source_node_id=wf_node_id, target_node_id=entity_node_id,
                    status="inferred", confidence=conf, source_registry="graph_inference", source_record_ids=[edge.edge_id, hs_edge_id], iteration=iteration,
                )
                self._record_inference(new_edge_id, RULE_B_WORKFLOW_ENTITY_INVOLVEMENT, input_node_ids=[wf_node_id, entity_node_id, step_node_id], input_edge_ids=[edge.edge_id, hs_edge_id], confidence=conf, iteration=iteration)
                created += 1
        return created

    # -- C: actor has_permission + permission enables operation -> can_perform --

    def _rule_c(self, iteration: int) -> int:
        created = 0
        for perm_edge in list(self.memory.edges.values()):
            if perm_edge.edge_type != "has_permission" or perm_edge.stale:
                continue
            actor_node_id, perm_node_id = perm_edge.source_node_id, perm_edge.target_node_id
            enables_edges = [e for e in self.memory.outgoing_edge_ids(perm_node_id) if self.memory.edges[e].edge_type == "enables"]
            for en_edge_id in enables_edges:
                operation_node_id = self.memory.edges[en_edge_id].target_node_id
                new_edge_id = edge_id("can_perform", actor_node_id, operation_node_id)
                if new_edge_id in self.memory.edges and not self.memory.edges[new_edge_id].stale:
                    continue
                premises = [perm_edge.confidence, self.memory.edges[en_edge_id].confidence]
                conf = graph_confidence.inferred_edge_confidence(premises, depth=2)
                self.memory.upsert_edge(
                    new_edge_id, edge_type="can_perform", source_node_id=actor_node_id, target_node_id=operation_node_id,
                    status="inferred", confidence=conf, source_registry="graph_inference", source_record_ids=[perm_edge.edge_id, en_edge_id], iteration=iteration,
                )
                self._record_inference(new_edge_id, RULE_C_PERMISSION_CAPABILITY, input_node_ids=[actor_node_id, perm_node_id, operation_node_id], input_edge_ids=[perm_edge.edge_id, en_edge_id], confidence=conf, iteration=iteration)
                created += 1
        return created

    # -- D: step A precedes step B AND A.to_state == B.from_state -> compatible --

    def _rule_d(self, iteration: int) -> int:
        created = 0
        for edge in list(self.memory.edges.values()):
            if edge.edge_type != "precedes" or edge.stale:
                continue
            step_a, step_b = edge.source_node_id, edge.target_node_id
            a_to_states = {self.memory.edges[e].target_node_id for e in self.memory.outgoing_edge_ids(step_a) if self.memory.edges[e].edge_type == "to_state"}
            b_from_states = {self.memory.edges[e].target_node_id for e in self.memory.outgoing_edge_ids(step_b) if self.memory.edges[e].edge_type == "from_state"}
            if not (a_to_states & b_from_states):
                continue
            new_edge_id = edge_id("compatible_continuation", step_a, step_b)
            if new_edge_id in self.memory.edges and not self.memory.edges[new_edge_id].stale:
                continue
            conf = graph_confidence.inferred_edge_confidence([edge.confidence], depth=1)
            self.memory.upsert_edge(
                new_edge_id, edge_type="compatible_continuation", source_node_id=step_a, target_node_id=step_b,
                status="inferred", confidence=conf, source_registry="graph_inference", source_record_ids=[edge.edge_id], iteration=iteration,
            )
            self._record_inference(new_edge_id, RULE_D_COMPATIBLE_CONTINUATION, input_node_ids=[step_a, step_b], input_edge_ids=[edge.edge_id], confidence=conf, iteration=iteration)
            created += 1
        return created

    # -- E: workflow produces to_state entity_state that includes_state a metric -> affects --

    def _rule_e(self, iteration: int) -> int:
        created = 0
        for edge in list(self.memory.edges.values()):
            if edge.edge_type != "to_state" or edge.stale:
                continue
            transition_node_id, state_node_id = edge.source_node_id, edge.target_node_id
            wf_edges = [e for e in self.memory.incoming_edge_ids(transition_node_id) if self.memory.edges[e].edge_type == "transitions"]
            includes_edges = [e for e in self.memory.outgoing_edge_ids(state_node_id) if self.memory.edges[e].edge_type == "includes_state"]
            for wf_edge_id in wf_edges:
                wf_node_id = self.memory.edges[wf_edge_id].source_node_id
                for inc_edge_id in includes_edges:
                    output_node_id = self.memory.edges[inc_edge_id].target_node_id
                    new_edge_id = edge_id("affects", wf_node_id, output_node_id, discriminator=state_node_id)
                    if new_edge_id in self.memory.edges and not self.memory.edges[new_edge_id].stale:
                        continue
                    premises = [edge.confidence, self.memory.edges[wf_edge_id].confidence, self.memory.edges[inc_edge_id].confidence]
                    conf = graph_confidence.inferred_edge_confidence(premises, depth=3)
                    self.memory.upsert_edge(
                        new_edge_id, edge_type="affects", source_node_id=wf_node_id, target_node_id=output_node_id,
                        status="inferred", confidence=conf, source_registry="graph_inference",
                        source_record_ids=[edge.edge_id, wf_edge_id, inc_edge_id], iteration=iteration,
                    )
                    self._record_inference(new_edge_id, RULE_E_DEPENDENCY_PROPAGATION, input_node_ids=[wf_node_id, state_node_id, output_node_id], input_edge_ids=[edge.edge_id, wf_edge_id, inc_edge_id], confidence=conf, iteration=iteration)
                    created += 1
        return created

    # -- F: output appears_on page AND actor views (that) page -> visible_to --

    def _rule_f(self, iteration: int) -> int:
        created = 0
        for appears_edge in list(self.memory.edges.values()):
            if appears_edge.edge_type != "appears_on" or appears_edge.stale:
                continue
            output_node = self.memory.nodes.get(appears_edge.source_node_id)
            if output_node is None or output_node.node_type not in {"derived_output", "counter", "badge", "chart", "report", "queue", "notification", "alert"}:
                continue
            output_node_id, page_node_id = appears_edge.source_node_id, appears_edge.target_node_id
            view_edges = [e for e in self.memory.incoming_edge_ids(page_node_id) if self.memory.edges[e].edge_type == "views"]
            for view_edge_id in view_edges:
                actor_node_id = self.memory.edges[view_edge_id].source_node_id
                new_edge_id = edge_id("visible_to", output_node_id, actor_node_id)
                if new_edge_id in self.memory.edges and not self.memory.edges[new_edge_id].stale:
                    continue
                premises = [appears_edge.confidence, self.memory.edges[view_edge_id].confidence]
                conf = graph_confidence.inferred_edge_confidence(premises, depth=2)
                self.memory.upsert_edge(
                    new_edge_id, edge_type="visible_to", source_node_id=output_node_id, target_node_id=actor_node_id,
                    status="inferred", confidence=conf, source_registry="graph_inference", source_record_ids=[appears_edge.edge_id, view_edge_id], iteration=iteration,
                )
                self._record_inference(new_edge_id, RULE_F_CONSUMER_VISIBILITY, input_node_ids=[output_node_id, page_node_id, actor_node_id], input_edge_ids=[appears_edge.edge_id, view_edge_id], confidence=conf, iteration=iteration)
                created += 1
        return created

    # -- G: step A performed_by actor A, step B performed_by actor B, A precedes B, A != B -> hands_off_to --

    def _rule_g(self, iteration: int) -> int:
        created = 0
        for precedes_edge in list(self.memory.edges.values()):
            if precedes_edge.edge_type != "precedes" or precedes_edge.stale:
                continue
            step_a, step_b = precedes_edge.source_node_id, precedes_edge.target_node_id
            actor_a_edges = [e for e in self.memory.outgoing_edge_ids(step_a) if self.memory.edges[e].edge_type == "performed_by"]
            actor_b_edges = [e for e in self.memory.outgoing_edge_ids(step_b) if self.memory.edges[e].edge_type == "performed_by"]
            for a_edge_id in actor_a_edges:
                actor_a = self.memory.edges[a_edge_id].target_node_id
                for b_edge_id in actor_b_edges:
                    actor_b = self.memory.edges[b_edge_id].target_node_id
                    if actor_a == actor_b:
                        continue
                    new_edge_id = edge_id("hands_off_to", actor_a, actor_b, discriminator=f"{step_a}:{step_b}")
                    if new_edge_id in self.memory.edges and not self.memory.edges[new_edge_id].stale:
                        continue
                    premises = [precedes_edge.confidence, self.memory.edges[a_edge_id].confidence, self.memory.edges[b_edge_id].confidence]
                    conf = graph_confidence.inferred_edge_confidence(premises, depth=3)
                    self.memory.upsert_edge(
                        new_edge_id, edge_type="hands_off_to", source_node_id=actor_a, target_node_id=actor_b,
                        status="inferred", confidence=conf, source_registry="graph_inference",
                        source_record_ids=[precedes_edge.edge_id, a_edge_id, b_edge_id], qualifiers={"via_step_a": step_a, "via_step_b": step_b}, iteration=iteration,
                    )
                    self._record_inference(new_edge_id, RULE_G_CROSS_ROLE_HANDOFF, input_node_ids=[actor_a, actor_b, step_a, step_b], input_edge_ids=[precedes_edge.edge_id, a_edge_id, b_edge_id], confidence=conf, iteration=iteration)
                    created += 1
        return created
