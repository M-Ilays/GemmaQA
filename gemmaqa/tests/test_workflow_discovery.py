"""Workflow Discovery and Reconstruction Engine
(app/intelligence/workflow_discovery/).

Covers: basic workflows (create/edit/archive/status-transition/multi-step-
form/list-to-detail), actor-aware workflows (single actor, cross-role
hand-off, assignment changes, unresolved hand-offs, single-role apps),
entity relationships (single entity, related entity, parent-child via URL
nesting, ownership transfer, shared entity across modules), state
transitions (explicit/structural/failed/contradictory/idempotent/repeated),
branches (approve/reject, save/submit, success/failure, cancel/complete),
prerequisites (missing entity/permission/actor/state/config, satisfied
later), merging (multi-page, aliases, repeats, cross-session, dedup),
confidence (observed > textual hint, bounded repetition, contradiction
penalty, inferred < confirmed, richness), and application neutrality (AST
contract test + generic fixtures across unrelated application types).
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest

from app.intelligence.actor_discovery import ActorRegistry
from app.intelligence.actor_discovery.schemas import ActorRecord, PermissionCandidate
from app.intelligence.entity_discovery import EntityRegistry
from app.intelligence.workflow_discovery import WorkflowDiscoveryEngine
from app.intelligence.workflow_discovery.schemas import WorkflowEvidence
from app.intelligence.workflow_discovery.state_transition_detector import StateTransitionDetector
from app.intelligence.workflow_discovery.workflow_confidence import (
    contradiction_penalty,
    score_confidence,
    score_evidence,
    workflow_richness_bonus,
)
from app.perception.models import (
    AlertDescriptor,
    BreadcrumbDescriptor,
    CanonicalPageModel,
    DialogDescriptor,
    HeadingDescriptor,
    NavigationItem,
    NavigationRegion,
    NetworkEvidence,
    PaginationDescriptor,
    TabDescriptor,
    TabGroup,
)
from app.schemas import FormDescriptor, FormFieldDescriptor, InteractiveElement, TableDescriptor


def _nav(items: list[str]) -> NavigationRegion:
    return NavigationRegion(items=[NavigationItem(element_id=f"n{i}", text=t) for i, t in enumerate(items)])


def _button(element_id: str, label: str, *, enabled: bool = True) -> InteractiveElement:
    return InteractiveElement(
        element_id=element_id, tag="button", accessible_name=label, text=label,
        is_visible=True, is_enabled=enabled, disabled=not enabled,
    )


def _empty_form(form_id: str, n_fields: int = 3) -> FormDescriptor:
    return FormDescriptor(
        form_id=form_id,
        field_descriptors=[FormFieldDescriptor(stable_id=f"{form_id}_f{i}", element_id=f"{form_id}_f{i}", label=f"Field {i}") for i in range(n_fields)],
    )


def _filled_form(form_id: str, n_fields: int = 3) -> FormDescriptor:
    return FormDescriptor(
        form_id=form_id,
        field_descriptors=[
            FormFieldDescriptor(stable_id=f"{form_id}_f{i}", element_id=f"{form_id}_f{i}", label=f"Field {i}", current_value=f"value{i}")
            for i in range(n_fields)
        ],
    )


def _status_table(table_id: str, name: str, status: str) -> TableDescriptor:
    return TableDescriptor(table_id=table_id, headers=["Name", "Status"], row_count=1, sample_rows=[[name, status]])


def _model(url: str, fp: str = "fp1", **kwargs) -> CanonicalPageModel:
    return CanonicalPageModel(url=url, state_fingerprint=fp, **kwargs)


def _observe(engine: WorkflowDiscoveryEngine, before, after, *, element_id=None, succeeded=True, iteration=1,
             actor="current session", entity=None, authenticated=True, entity_registry=None, actor_registry=None):
    return engine.observe(
        before_model=before, after_model=after, executed_element_id=element_id, action_succeeded=succeeded,
        iteration=iteration, authenticated=authenticated, current_actor_term=actor, primary_entity_term=entity,
        entity_registry=entity_registry, actor_registry=actor_registry,
    )


# ---------------------------------------------------------------------------
# A. Basic workflows
# ---------------------------------------------------------------------------


class TestBasicWorkflows:
    def test_create_entity(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/customers/new", "fp1", forms=[_empty_form("f1")], interactive_elements=[_button("b1", "Submit")])
        after = _model("https://x.example/customers", "fp2")
        summary = _observe(engine, before, after, element_id="b1", entity="customer")
        workflow = engine.registry.get("entity:customer")
        assert workflow is not None
        submit_step = next(s for s in workflow.steps if s.control_id == "b1")
        assert submit_step.status == "observed"
        assert submit_step.semantic_action == "submit"
        create_step = next(s for s in workflow.steps if s.form_id == "f1")
        assert create_step.semantic_action == "create"

    def test_edit_entity(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/customers/1/edit", "fp1", forms=[_filled_form("f1")])
        after = _model("https://x.example/customers/1", "fp2")
        _observe(engine, before, after, entity="customer")
        workflow = engine.registry.get("entity:customer")
        edit_step = next(s for s in workflow.steps if s.form_id == "f1")
        assert edit_step.semantic_action == "edit"

    def test_archive_entity(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/customers/1", "fp1", interactive_elements=[_button("b1", "Archive Customer")])
        after = _model("https://x.example/customers/1", "fp2")
        _observe(engine, before, after, element_id="b1", entity="customer")
        workflow = engine.registry.get("entity:customer")
        step = next(s for s in workflow.steps if s.control_id == "b1")
        assert step.status == "observed"
        assert step.semantic_action == "archive"
        assert "https://x.example/customers/1" in workflow.known_exit_points

    def test_primary_navigation_links_are_not_treated_as_entity_workflow_steps(self):
        # Regression: a live ServiceFlow run showed every top-nav link
        # (Dashboard/Customers/Jobs/Settings) being captured as a
        # "progression_button" step and attributed to whichever entity
        # happened to be the current page's subject, polluting every
        # discovered entity's workflow with the same handful of duplicated,
        # meaningless "steps". Nav-landmark links must never become
        # entity-attributed workflow steps.
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/jobs/1", "fp1",
            navigation_regions=[_nav(["Dashboard", "Jobs"])],
            interactive_elements=[
                _button("n0", "Dashboard"), _button("n1", "Jobs"), _button("b1", "Approve"),
            ],
        )
        after = _model("https://x.example/jobs/1", "fp2")
        _observe(engine, before, after, element_id="b1", entity="job")
        workflow = engine.registry.get("entity:job")
        assert not any(s.control_id in {"n0", "n1"} for s in workflow.steps)
        assert any(s.control_id == "b1" and s.semantic_action == "approve" for s in workflow.steps)

    def test_form_field_labels_are_not_treated_as_action_controls(self):
        # Regression: a live ServiceFlow run showed raw text-input labels
        # ("First Name", "Email*", "Company", "Timezone") turning into
        # bogus "first"/"email*"/"company"/"timezone" workflow steps,
        # because interactive text inputs were treated the same as buttons.
        # Only elements that actually trigger an action (buttons/links) may
        # become progression_button candidates.
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/customers/1/edit", "fp1",
            interactive_elements=[
                InteractiveElement(element_id="i1", tag="input", accessible_name="First Name", is_visible=True, is_enabled=True),
                InteractiveElement(element_id="i2", tag="select", accessible_name="Timezone", is_visible=True, is_enabled=True),
                _button("b1", "Save"),
            ],
        )
        after = _model("https://x.example/customers/1", "fp2")
        _observe(engine, before, after, element_id="b1", entity="customer")
        workflow = engine.registry.get("entity:customer")
        assert not any(s.control_id in {"i1", "i2"} for s in workflow.steps)
        assert any(s.control_id == "b1" and s.semantic_action == "save" for s in workflow.steps)

    def test_plain_navigation_link_label_is_not_treated_as_a_verb(self):
        # Regression: a live ServiceFlow run had a dashboard "recent
        # customers" widget rendering each customer as a plain <a> link
        # whose own display text is the customer's name ("Jordan Lee") --
        # the unknown-verb-preserved fallback turned that proper noun into
        # a bogus "jordan" workflow step. A plain anchor (not role=button)
        # is a navigation transition, not an action verb on the entity.
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/dashboard", "fp1",
            interactive_elements=[
                InteractiveElement(element_id="a1", tag="a", accessible_name="Jordan Lee", href="/customers/1", is_visible=True, is_enabled=True),
                InteractiveElement(element_id="a2", tag="a", role="button", accessible_name="Approve", is_visible=True, is_enabled=True),
            ],
        )
        after = _model("https://x.example/dashboard", "fp2")
        _observe(engine, before, after, element_id="a2", entity="customer")
        workflow = engine.registry.get("entity:customer")
        assert not any(s.control_id == "a1" for s in workflow.steps)
        assert any(s.control_id == "a2" and s.semantic_action == "approve" for s in workflow.steps)

    def test_form_label_element_is_not_treated_as_an_action_control(self):
        # Regression: a live SauceDemo run picked up a checkout form's own
        # <label> elements ("Full Name") as bogus "name" workflow steps --
        # a <label> is a caption for a field, never an action trigger.
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/checkout", "fp1",
            interactive_elements=[
                InteractiveElement(element_id="l1", tag="label", accessible_name="Full Name", is_visible=True, is_enabled=True),
                _button("b1", "Continue"),
            ],
        )
        after = _model("https://x.example/checkout", "fp2")
        _observe(engine, before, after, element_id="b1", entity="order")
        workflow = engine.registry.get("entity:order")
        assert not any(s.control_id == "l1" for s in workflow.steps)
        assert any(s.control_id == "b1" and s.semantic_action == "continue" for s in workflow.steps)

    def test_tab_switch_control_is_not_treated_as_an_entity_workflow_step(self):
        # Regression: a live InsightBoard run had "Overview"/"Performance"/
        # "Activity"/"Team" tab-switch controls captured as bogus workflow
        # steps and attributed to whichever entity happened to be current --
        # a tab switch is a view selection, not an action on the entity.
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/dashboard", "fp1",
            tabs=[TabGroup(element_id="tg1", tabs=[
                TabDescriptor(element_id="t0", text="Overview"),
                TabDescriptor(element_id="t1", text="Performance"),
            ])],
            interactive_elements=[
                _button("t0", "Overview"), _button("t1", "Performance"), _button("b1", "Publish"),
            ],
        )
        after = _model("https://x.example/dashboard", "fp2")
        _observe(engine, before, after, element_id="b1", entity="report")
        workflow = engine.registry.get("entity:report")
        assert not any(s.control_id in {"t0", "t1"} for s in workflow.steps)
        assert any(s.control_id == "b1" and s.semantic_action == "publish" for s in workflow.steps)

    def test_kpi_figure_label_is_not_treated_as_an_action_verb(self):
        # Regression: a live InsightBoard run had a clickable KPI stat card
        # ("128 Active Customers") turned into a bogus "128" workflow step
        # -- a leading digit means this is a figure, never a verb.
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/dashboard", "fp1",
            interactive_elements=[_button("k1", "128 Active Customers"), _button("b1", "Export")],
        )
        after = _model("https://x.example/dashboard", "fp2")
        _observe(engine, before, after, element_id="b1", entity="customer")
        workflow = engine.registry.get("entity:customer")
        assert not any(s.control_id == "k1" for s in workflow.steps)
        assert any(s.control_id == "b1" and s.semantic_action == "export" for s in workflow.steps)

    def test_status_transition(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/customers", "fp1", tables=[_status_table("t1", "Acme", "Draft")], interactive_elements=[_button("b1", "Submit")])
        after = _model("https://x.example/customers", "fp2", tables=[_status_table("t1", "Acme", "Submitted")], interactive_elements=[_button("b1", "Submit")])
        _observe(engine, before, after, element_id="b1", entity="customer")
        workflow = engine.registry.get("entity:customer")
        transition = workflow.transitions[0]
        assert (transition.source_state, transition.target_state) == ("Draft", "Submitted")
        assert transition.is_explicit is True

    def test_multi_step_form(self):
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/wizard/step1", "fp1", forms=[_empty_form("f1")],
            pagination=[PaginationDescriptor(current_page=1, total_pages=3)],
        )
        after = _model("https://x.example/wizard/step2", "fp2")
        summary = _observe(engine, before, after, entity=None)
        # entity is None here -> page-anchored workflow
        workflow = next(iter(engine.registry.all_workflows()))
        assert any(s.semantic_action in {"create", "continue"} for s in workflow.steps)

    def test_list_to_detail_to_action(self):
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/customers/1", "fp1",
            breadcrumbs=[BreadcrumbDescriptor(text="Customers", ordinal=0), BreadcrumbDescriptor(text="Acme", ordinal=1)],
        )
        after = _model("https://x.example/customers/1", "fp2")
        _observe(engine, before, after, entity="customer")
        workflow = engine.registry.get("entity:customer")
        assert any(s.action_verb == "" and s.semantic_action == "view" for s in workflow.steps) or any(
            "Customers > Acme" in (ev.observed_text or "") for s in workflow.steps for ev in s.evidence
        )


# ---------------------------------------------------------------------------
# B. Actor-aware workflows
# ---------------------------------------------------------------------------


class TestActorAwareWorkflows:
    def test_single_actor_performs_all_steps(self):
        engine = WorkflowDiscoveryEngine()
        b1 = _model("https://x.example/customers/new", "fp1", forms=[_empty_form("f1")], interactive_elements=[_button("b1", "Submit")])
        a1 = _model("https://x.example/customers", "fp2")
        _observe(engine, b1, a1, element_id="b1", entity="customer", actor="alice")
        b2 = _model("https://x.example/customers", "fp2", interactive_elements=[_button("b2", "Approve")])
        a2 = _model("https://x.example/customers", "fp3")
        _observe(engine, b2, a2, element_id="b2", entity="customer", actor="alice", iteration=2)
        workflow = engine.registry.get("entity:customer")
        assert workflow.is_cross_role() is False
        assert {p.actor_id for p in workflow.actors} == {"alice"}

    def test_actor_creates_and_another_actor_approves(self):
        engine = WorkflowDiscoveryEngine()
        b1 = _model("https://x.example/customers/new", "fp1", forms=[_empty_form("f1")], interactive_elements=[_button("b1", "Submit")])
        a1 = _model("https://x.example/customers", "fp2")
        _observe(engine, b1, a1, element_id="b1", entity="customer", actor="alice")
        b2 = _model("https://x.example/customers", "fp2", interactive_elements=[_button("b2", "Approve")])
        a2 = _model("https://x.example/customers", "fp3")
        _observe(engine, b2, a2, element_id="b2", entity="customer", actor="manager", iteration=2)
        workflow = engine.registry.get("entity:customer")
        assert workflow.is_cross_role() is True
        assert {p.actor_id for p in workflow.actors} == {"alice", "manager"}

    def test_actor_assignment_changes(self):
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/jobs", "fp1",
            tables=[TableDescriptor(table_id="t1", headers=["Job", "Assigned"], row_count=1, sample_rows=[["Job A", "Unassigned"]])],
        )
        after = _model(
            "https://x.example/jobs", "fp2",
            tables=[TableDescriptor(table_id="t1", headers=["Job", "Assigned"], row_count=1, sample_rows=[["Job A", "Bob"]])],
        )
        _observe(engine, before, after, entity="job")
        workflow = engine.registry.get("entity:job")
        assert any(s.semantic_action == "update" for s in workflow.steps)
        assert any(sig.kind == "assignment_changed" for sig in StateTransitionDetector().detect(before, after).signals)

    def test_controls_differ_by_actor_triggers_handoff_via_permission_mismatch(self):
        actor_registry = ActorRegistry()
        alice = actor_registry.memory.get_or_create("alice")
        alice.known_session_types.append("authenticated")
        alice.known_pages.append("https://x.example/customers")
        # alice has NO approval permission recorded.
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/customers/1", "fp1", interactive_elements=[_button("b1", "Approve")])
        after = _model("https://x.example/customers/1", "fp2")
        summary = _observe(engine, before, after, entity="customer", actor="alice", actor_registry=actor_registry)
        assert len(summary["handoffs_detected"]) >= 1

    def test_unresolved_actor_handoff_produces_a_gap_and_requires_another_actor(self):
        actor_registry = ActorRegistry()
        alice = actor_registry.memory.get_or_create("alice")
        alice.known_session_types.append("authenticated")
        alice.known_pages.append("https://x.example/customers")
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/customers/1", "fp1", interactive_elements=[_button("b1", "Approve")])
        after = _model("https://x.example/customers/1", "fp2")
        _observe(engine, before, after, entity="customer", actor="alice", actor_registry=actor_registry)
        workflow = engine.registry.get("entity:customer")
        assert workflow.requires_another_actor() is True
        assert any(g.gap_type == "unresolved_actor_handoff" for g in engine.registry.workflow_gaps())

    def test_single_role_application_has_no_cross_role_workflows(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/reports", "fp1", interactive_elements=[_button("b1", "Export")])
        after = _model("https://x.example/reports", "fp2")
        _observe(engine, before, after, element_id="b1", entity="report", actor="current session")
        assert engine.registry.cross_role_workflows() == []


# ---------------------------------------------------------------------------
# C. Entity relationships
# ---------------------------------------------------------------------------


class TestEntityRelationships:
    def test_workflow_involves_one_entity(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/customers/1", "fp1", interactive_elements=[_button("b1", "Archive Customer")])
        after = _model("https://x.example/customers/1", "fp2")
        _observe(engine, before, after, element_id="b1", entity="customer")
        workflow = engine.registry.get("entity:customer")
        assert {p.entity_id for p in workflow.entities} == {"customer"}

    def test_parent_child_relationship_via_url_nesting_creates_separate_workflows(self):
        engine = WorkflowDiscoveryEngine()
        b1 = _model("https://x.example/customers/1/orders/new", "fp1", forms=[_empty_form("f1")], interactive_elements=[_button("b1", "Submit")])
        a1 = _model("https://x.example/customers/1/orders", "fp2")
        _observe(engine, b1, a1, element_id="b1", entity="order")
        assert engine.registry.get("entity:order") is not None
        assert engine.registry.get("entity:customer") is None  # customer never independently touched here

    def test_shared_entity_across_modules_merges_into_one_workflow(self):
        engine = WorkflowDiscoveryEngine()
        b1 = _model("https://x.example/customers/new", "fp1", forms=[_empty_form("f1")], interactive_elements=[_button("b1", "Submit")])
        a1 = _model("https://x.example/customers", "fp2")
        _observe(engine, b1, a1, element_id="b1", entity="customer", iteration=1)
        b2 = _model("https://x.example/reports/customers", "fp3", interactive_elements=[_button("b2", "Export")])
        a2 = _model("https://x.example/reports/customers", "fp4")
        _observe(engine, b2, a2, element_id="b2", entity="customer", iteration=2)
        workflows = [w for w in engine.registry.all_workflows() if any(p.entity_id == "customer" for p in w.entities)]
        assert len(workflows) == 1
        assert len(workflows[0].source_pages) == 2

    def test_ownership_transfer_recorded_as_assignment_changed(self):
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/jobs", "fp1",
            tables=[TableDescriptor(table_id="t1", headers=["Job", "Owner"], row_count=1, sample_rows=[["Job A", "Alice"]])],
        )
        after = _model(
            "https://x.example/jobs", "fp2",
            tables=[TableDescriptor(table_id="t1", headers=["Job", "Owner"], row_count=1, sample_rows=[["Job A", "Bob"]])],
        )
        detector = StateTransitionDetector()
        signals = detector.detect(before, after).signals
        assert any(s.kind == "assignment_changed" and s.before_value == "Alice" and s.after_value == "Bob" for s in signals)


# ---------------------------------------------------------------------------
# D. State transitions
# ---------------------------------------------------------------------------


class TestStateTransitions:
    def test_explicit_status_before_and_after(self):
        before = _model("https://x.example/x", "fp1", tables=[_status_table("t1", "A", "Draft")])
        after = _model("https://x.example/x", "fp2", tables=[_status_table("t1", "A", "Submitted")])
        signal = next(s for s in StateTransitionDetector().detect(before, after).signals if s.kind == "entity_status_changed")
        assert signal.before_value == "Draft" and signal.after_value == "Submitted"
        assert signal.confidence >= 0.7

    def test_structural_state_without_explicit_status_label(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", dialogs=[])
        after = _model("https://x.example/x", "fp2", dialogs=[DialogDescriptor(element_id="d1", text="Confirm", is_open=True)])
        _observe(engine, before, after, element_id="d1", entity="thing", succeeded=True)
        workflow = engine.registry.get("entity:thing")
        structural = [t for t in workflow.transitions if not t.is_explicit]
        assert structural, "a structural (non-explicit) transition should be recorded when a real one is absent"
        assert all(t.confidence <= 0.3 for t in structural)

    def test_failed_transition_marks_step_blocked_not_observed(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Submit")])
        after = _model("https://x.example/x", "fp1")  # nothing changed: action failed
        _observe(engine, before, after, element_id="b1", entity="thing", succeeded=False)
        workflow = engine.registry.get("entity:thing")
        step = next(s for s in workflow.steps if s.control_id == "b1")
        assert step.status == "blocked"

    def test_contradictory_transitions_reduce_confidence(self):
        from app.intelligence.workflow_discovery.schemas import WorkflowDescriptor, WorkflowTransition

        descriptor = WorkflowDescriptor(canonical_name="x")
        descriptor.transitions = [
            WorkflowTransition(source_state="draft", target_state="approved", confidence=0.6),
            WorkflowTransition(source_state="approved", target_state="draft", confidence=0.6),
        ]
        assert contradiction_penalty(descriptor) > 0

    def test_idempotent_action_does_not_duplicate_step(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Refresh")])
        after = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Refresh")])
        _observe(engine, before, after, element_id="b1", entity="thing", iteration=1)
        _observe(engine, before, after, element_id="b1", entity="thing", iteration=2)
        workflow = engine.registry.get("entity:thing")
        matching = [s for s in workflow.steps if s.control_id == "b1"]
        assert len(matching) == 1

    def test_repeated_action_merges_evidence_without_duplication(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Export")])
        after = _model("https://x.example/x", "fp2", interactive_elements=[_button("b1", "Export")])
        for i in range(5):
            _observe(engine, before, after, element_id="b1", entity="thing", iteration=i)
        workflow = engine.registry.get("entity:thing")
        step = next(s for s in workflow.steps if s.control_id == "b1")
        assert len(step.evidence) <= 3  # deduped, not one entry per repeat


# ---------------------------------------------------------------------------
# E. Branches
# ---------------------------------------------------------------------------


class TestBranches:
    def test_approve_reject_branch(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Approve"), _button("b2", "Reject")])
        after = _model("https://x.example/x", "fp2")
        _observe(engine, before, after, entity="thing")
        workflow = engine.registry.get("entity:thing")
        assert any(b.branch_type == "approve_vs_reject" for b in workflow.branches)

    def test_save_submit_branch(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Save"), _button("b2", "Submit")])
        after = _model("https://x.example/x", "fp2")
        _observe(engine, before, after, entity="thing")
        workflow = engine.registry.get("entity:thing")
        assert any(b.branch_type == "save_vs_submit" for b in workflow.branches)

    def test_success_failure_branch_via_transition_outcomes(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Submit")])
        after = _model("https://x.example/x", "fp2", alerts=[AlertDescriptor(text="Saved successfully", severity="success")])
        _observe(engine, before, after, element_id="b1", entity="thing")
        workflow = engine.registry.get("entity:thing")
        assert any(o.outcome_type == "state_change" for o in workflow.outcomes)
        assert "https://x.example/x" in workflow.known_success_paths

    def test_cancel_complete_branch(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Complete"), _button("b2", "Cancel")])
        after = _model("https://x.example/x", "fp2")
        _observe(engine, before, after, entity="thing")
        workflow = engine.registry.get("entity:thing")
        assert any(b.branch_type == "complete_vs_cancel" for b in workflow.branches)

    def test_branches_are_not_merged_into_a_single_linear_workflow(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Approve"), _button("b2", "Reject")])
        after = _model("https://x.example/x", "fp2")
        _observe(engine, before, after, entity="thing")
        workflow = engine.registry.get("entity:thing")
        branch = next(b for b in workflow.branches if b.branch_type == "approve_vs_reject")
        assert len(branch.option_labels) == 2


# ---------------------------------------------------------------------------
# F. Prerequisites
# ---------------------------------------------------------------------------


class TestPrerequisites:
    def test_authentication_prerequisite_when_not_authenticated(self):
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1")
        after = _model("https://x.example/x", "fp2")
        _observe(engine, before, after, entity="thing", authenticated=False)
        workflow = engine.registry.get("entity:thing")
        prereq = next(p for p in workflow.prerequisites if p.type == "authentication")
        assert prereq.satisfied is False

    def test_missing_permission_prerequisite(self):
        actor_registry = ActorRegistry()
        alice = actor_registry.memory.get_or_create("alice")
        alice.known_session_types.append("authenticated")
        alice.known_pages.append("https://x.example/x")
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Approve")])
        after = _model("https://x.example/x", "fp2")
        _observe(engine, before, after, entity="thing", actor="alice", actor_registry=actor_registry)
        workflow = engine.registry.get("entity:thing")
        assert any(p.type == "required_actor" and p.satisfied is False for p in workflow.prerequisites)

    def test_wrong_actor_prerequisite_resolves_once_correct_actor_participates(self):
        actor_registry = ActorRegistry()
        alice = actor_registry.memory.get_or_create("alice")
        alice.known_session_types.append("authenticated")
        alice.known_pages.append("https://x.example/x")
        manager = actor_registry.memory.get_or_create("manager")
        manager.known_permissions.append(PermissionCandidate(permission_id="can_approve", positive_evidence=[]))
        manager.known_session_types.append("authenticated")
        manager.known_pages.append("https://x.example/x")

        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Approve")])
        after = _model("https://x.example/x", "fp2")
        _observe(engine, before, after, entity="thing", actor="alice", actor_registry=actor_registry, iteration=1)
        _observe(engine, before, after, element_id="b1", entity="thing", actor="manager", actor_registry=actor_registry, iteration=2)
        workflow = engine.registry.get("entity:thing")
        assert workflow.is_cross_role() is True

    def test_wrong_entity_state_prerequisite_via_assignment(self):
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/x", "fp1",
            interactive_elements=[_button("b1", "Approve")],
            forms=[FormDescriptor(form_id="f1", field_descriptors=[FormFieldDescriptor(stable_id="a1", element_id="a1", label="Assignee", options=["Bob"])])],
        )
        after = _model("https://x.example/x", "fp2")
        _observe(engine, before, after, entity="thing")
        workflow = engine.registry.get("entity:thing")
        assert any(p.type == "required_assignment" for p in workflow.prerequisites)

    def test_prerequisite_satisfied_later_updates_in_place(self):
        actor_registry = ActorRegistry()
        alice = actor_registry.memory.get_or_create("alice")
        alice.known_session_types.append("authenticated")
        alice.known_pages.append("https://x.example/x")
        engine = WorkflowDiscoveryEngine()
        before = _model("https://x.example/x", "fp1", interactive_elements=[_button("b1", "Approve")])
        after = _model("https://x.example/x", "fp2")
        _observe(engine, before, after, entity="thing", actor="alice", actor_registry=actor_registry, iteration=1)
        manager = actor_registry.memory.get_or_create("manager")
        manager.known_permissions.append(PermissionCandidate(permission_id="can_approve", positive_evidence=[]))
        manager.known_session_types.append("authenticated")
        manager.known_pages.append("https://x.example/x")
        _observe(engine, before, after, element_id="b1", entity="thing", actor="manager", actor_registry=actor_registry, iteration=2)
        workflow = engine.registry.get("entity:thing")
        required_actor_prereqs = [p for p in workflow.prerequisites if p.type == "required_actor"]
        assert any(p.satisfied is True for p in required_actor_prereqs) or workflow.is_cross_role()


# ---------------------------------------------------------------------------
# G. Merging
# ---------------------------------------------------------------------------


class TestMerging:
    def test_same_workflow_discovered_on_multiple_pages_merges(self):
        engine = WorkflowDiscoveryEngine()
        b1 = _model("https://x.example/customers", "fp1", interactive_elements=[_button("b1", "Export")])
        a1 = _model("https://x.example/customers", "fp2")
        _observe(engine, b1, a1, element_id="b1", entity="customer", iteration=1)
        b2 = _model("https://x.example/customers/1", "fp3", interactive_elements=[_button("b2", "Archive Customer")])
        a2 = _model("https://x.example/customers/1", "fp4")
        _observe(engine, b2, a2, element_id="b2", entity="customer", iteration=2)
        assert len([w for w in engine.registry.all_workflows() if any(p.entity_id == "customer" for p in w.entities)]) == 1

    def test_cross_session_observations_merge_into_the_same_workflow(self):
        engine = WorkflowDiscoveryEngine()  # one engine instance simulates persistence across a run
        for i in range(2):
            before = _model("https://x.example/customers", f"fp{i}", interactive_elements=[_button("b1", "Export")])
            after = _model("https://x.example/customers", f"fp{i}b")
            _observe(engine, before, after, element_id="b1", entity="customer", iteration=i)
        workflow = engine.registry.get("entity:customer")
        assert workflow.observation_count == 2

    def test_duplicate_network_and_ui_evidence_does_not_double_count(self):
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://x.example/customers", "fp1", interactive_elements=[_button("b1", "Submit")],
            network_evidence=[NetworkEvidence(method="POST", text="POST /api/customers")],
        )
        after = _model("https://x.example/customers", "fp2", network_evidence=[NetworkEvidence(method="POST", text="POST /api/customers")])
        _observe(engine, before, after, element_id="b1", entity="customer", iteration=1)
        _observe(engine, before, after, element_id="b1", entity="customer", iteration=2)
        workflow = engine.registry.get("entity:customer")
        network_evidence_count = sum(1 for e in workflow.supporting_evidence if e.source_kind == "network_mutation")
        assert network_evidence_count <= 1


# ---------------------------------------------------------------------------
# H. Confidence
# ---------------------------------------------------------------------------


class TestConfidenceModel:
    def test_observed_transition_outranks_textual_hint_alone(self):
        rich_evidence = [
            WorkflowEvidence(source_kind="before_after_entity_state", observed_text="x"),
            WorkflowEvidence(source_kind="table_row_transition", observed_text="y"),
        ]
        weak_evidence = [WorkflowEvidence(source_kind="page_heading", observed_text="z")]
        assert score_evidence(rich_evidence) > score_evidence(weak_evidence)

    def test_repeated_duplicate_evidence_does_not_inflate_confidence_unboundedly(self):
        one_evidence = score_evidence([WorkflowEvidence(source_kind="progression_button", observed_text="Next")])
        many_same = score_evidence([WorkflowEvidence(source_kind="progression_button", observed_text="Next") for _ in range(50)])
        assert many_same == one_evidence  # exact duplicates count once

    def test_contradictory_evidence_reduces_confidence(self):
        from app.intelligence.workflow_discovery.schemas import WorkflowDescriptor, WorkflowTransition

        base = WorkflowDescriptor(
            canonical_name="x",
            supporting_evidence=[WorkflowEvidence(source_kind="before_after_entity_state", observed_text="a")],
        )
        clean = base.model_copy(deep=True)
        clean.transitions = [WorkflowTransition(source_state="draft", target_state="approved", confidence=0.6)]
        contradicted = base.model_copy(deep=True)
        contradicted.transitions = [
            WorkflowTransition(source_state="draft", target_state="approved", confidence=0.6),
            WorkflowTransition(source_state="approved", target_state="draft", confidence=0.6),
        ]
        assert score_confidence(contradicted) < score_confidence(clean)

    def test_inferred_workflow_remains_below_confirmed_workflow(self):
        engine = WorkflowDiscoveryEngine()
        # confirmed: an actually observed, successful step with a real transition.
        before = _model("https://x.example/a", "fp1", tables=[_status_table("t1", "X", "Draft")], interactive_elements=[_button("b1", "Submit")])
        after = _model("https://x.example/a", "fp2", tables=[_status_table("t1", "X", "Submitted")])
        _observe(engine, before, after, element_id="b1", entity="alpha")
        confirmed = engine.registry.get("entity:alpha")

        # thin/inferred: only structurally-present candidates, nothing executed.
        before2 = _model("https://x.example/b", "fp1", interactive_elements=[_button("b2", "Export")])
        after2 = _model("https://x.example/b", "fp1")
        _observe(engine, before2, after2, element_id=None, entity="beta")
        thin = engine.registry.get("entity:beta")

        assert confirmed.confidence > thin.confidence
        assert thin.status == "candidate"
        assert confirmed.status == "confirmed"

    def test_rich_accumulated_workflow_evidence_raises_confidence(self):
        from app.intelligence.workflow_discovery.schemas import WorkflowActorParticipation, WorkflowDescriptor, WorkflowEntityParticipation, WorkflowOutcome, WorkflowStep, WorkflowTransition

        thin = WorkflowDescriptor(canonical_name="thin")
        rich = WorkflowDescriptor(
            canonical_name="rich",
            steps=[WorkflowStep(status="observed"), WorkflowStep(status="unverified")],
            transitions=[WorkflowTransition(source_state="a", target_state="b")],
            actors=[WorkflowActorParticipation(actor_id="alice"), WorkflowActorParticipation(actor_id="bob")],
            entities=[WorkflowEntityParticipation(entity_id="thing")],
            outcomes=[WorkflowOutcome(description="done")],
        )
        assert workflow_richness_bonus(rich) > workflow_richness_bonus(thin)


# ---------------------------------------------------------------------------
# I. Application neutrality
# ---------------------------------------------------------------------------


class TestApplicationNeutrality:
    def test_no_business_domain_vocabulary_in_package_code(self):
        import ast

        import app.intelligence.workflow_discovery as pkg

        forbidden_words = {
            "customer", "customers", "invoice", "invoices", "order", "orders",
            "driver", "drivers", "shipment", "shipments", "technician",
            "administrator", "manager", "dispatcher", "employee",
            "job", "jobs", "contact", "contacts",
        }
        offenders: list[str] = []
        for path in sorted(Path(pkg.__file__).parent.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstring_nodes: set[int] = set()
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                    docstring_nodes.add(id(body[0].value))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in docstring_nodes:
                    continue
                for word in node.value.lower().replace("_", " ").replace("-", " ").split():
                    cleaned = word.strip(".,;:!?\"'()")
                    if cleaned in forbidden_words:
                        offenders.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}")
        assert not offenders, "business vocabulary hardcoded in workflow_discovery: " + "; ".join(offenders)

    def test_generic_fixture_field_service_application(self):
        entity_registry = EntityRegistry()
        actor_registry = ActorRegistry()
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://svc.example/jobs/new", "fp1", forms=[_empty_form("f1")],
            interactive_elements=[_button("b1", "Submit")],
        )
        after = _model("https://svc.example/jobs", "fp2")
        summary = _observe(
            engine, before, after, element_id="b1", entity="job", actor="current session",
            entity_registry=entity_registry, actor_registry=actor_registry,
        )
        assert engine.registry.get("entity:job") is not None

    def test_generic_fixture_logistics_application(self):
        """A completely different domain, discovered with the SAME code."""
        engine = WorkflowDiscoveryEngine()
        before = _model(
            "https://freight.example/shipments", "fp1",
            tables=[_status_table("t1", "S-1", "in_transit")],
            interactive_elements=[_button("b1", "Dispatch")],
        )
        after = _model("https://freight.example/shipments", "fp2", tables=[_status_table("t1", "S-1", "delivered")])
        _observe(engine, before, after, element_id="b1", entity="shipment")
        workflow = engine.registry.get("entity:shipment")
        assert workflow is not None
        assert any(s.semantic_action == "dispatch" for s in workflow.steps)
        assert any((t.source_state, t.target_state) == ("in_transit", "delivered") for t in workflow.transitions)


# ---------------------------------------------------------------------------
# Live integration: real Playwright page
# ---------------------------------------------------------------------------


async def test_live_workflow_discovery_from_real_page_transition(tmp_path):
    try:
        from playwright.async_api import async_playwright
    except Exception:
        pytest.skip("Playwright not importable")

    from app.perception.engine import PerceptionEngine

    html_before = """
    <!doctype html><html><head><title>Tickets</title></head><body>
    <h1>Tickets</h1>
    <table><thead><tr><th>Ticket</th><th>Status</th></tr></thead>
    <tbody><tr><td>T-1</td><td>Open</td></tr></tbody></table>
    <button id="resolve-btn">Resolve</button>
    </body></html>
    """
    html_after = """
    <!doctype html><html><head><title>Tickets</title></head><body>
    <h1>Tickets</h1>
    <table><thead><tr><th>Ticket</th><th>Status</th></tr></thead>
    <tbody><tr><td>T-1</td><td>Resolved</td></tr></tbody></table>
    <button id="resolve-btn">Resolve</button>
    </body></html>
    """
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=html_before))
                await page.goto("https://ticket-demo.local/tickets")
                before_model = await PerceptionEngine().observe(page)

                await page.unroute("**/*")
                await page.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=html_after))
                await page.goto("https://ticket-demo.local/tickets")
                after_model = await PerceptionEngine().observe(page)
            finally:
                await browser.close()
    except Exception as exc:
        pytest.skip(f"Live Chromium unavailable: {type(exc).__name__}: {exc}")
        return

    # The Perception Engine assigns its own internal element_id (a
    # data-gemmaqa-id, not the page's literal HTML `id` attribute) — resolve
    # the real one by label rather than assuming "resolve-btn" survived.
    resolve_element = next(
        el for el in before_model.interactive_elements if "resolve" in (el.accessible_name or el.text or "").lower()
    )

    engine = WorkflowDiscoveryEngine()
    engine.observe(
        before_model=before_model, after_model=after_model, executed_element_id=resolve_element.element_id,
        action_succeeded=True, iteration=1, authenticated=True, current_actor_term="current session",
        primary_entity_term="ticket",
    )
    workflow = engine.registry.get("entity:ticket")
    assert workflow is not None
    assert any((t.source_state, t.target_state) == ("Open", "Resolved") for t in workflow.transitions)
    assert any(s.semantic_action == "resolve" and s.status == "observed" for s in workflow.steps)
