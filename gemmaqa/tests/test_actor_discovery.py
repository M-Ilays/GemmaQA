"""Autonomous Actor Discovery Engine (app/intelligence/actor_discovery/).

Covers: hint-based page/role classification, candidate extraction (role
dropdowns, role tables incl. multi-role cells, invite/approval contexts,
assignment targets), permission inference (visible/disabled controls, page
reachability, network 401/403, access-denied text), memory merging
(including the real "session actor stuck at candidate/confidence-0" bug this
build found and fixed), role relationships (co-assignment, permission-
superset hierarchy), actor difference analysis, registry queries (the full
known/unknown/unverified/requiring-exploration/missing-permissions/missing-
dashboard Planner API), two structurally-different synthetic multi-role
applications proving zero application-specific code, an AST vocabulary
contract test, and one live Playwright integration test.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest

from app.intelligence.actor_discovery import ActorDiscoveryEngine, ActorRegistry
from app.intelligence.actor_discovery.actor_candidate_builder import (
    ActorCandidateBuilder,
    role_modifier_flags,
)
from app.intelligence.actor_discovery.actor_memory import score_confidence, status_for
from app.intelligence.actor_discovery.permission_discovery import PermissionDiscovery
from app.intelligence.actor_discovery.role_relationships import RoleRelationshipBuilder, compare_actors
from app.intelligence.actor_discovery.schemas import ActorEvidence, ActorRecord
from app.intelligence.entity_discovery import EntityDiscoveryEngine
from app.perception.models import (
    AlertDescriptor,
    CanonicalPageModel,
    HeadingDescriptor,
    NavigationItem,
    NavigationRegion,
    NetworkEvidence,
)
from app.schemas import FormDescriptor, FormFieldDescriptor, InteractiveElement, TableDescriptor


def _nav(items: list[str]) -> NavigationRegion:
    return NavigationRegion(items=[NavigationItem(element_id=f"n{i}", text=t) for i, t in enumerate(items)])


def _button(element_id: str, label: str, *, enabled: bool = True) -> InteractiveElement:
    return InteractiveElement(
        element_id=element_id, tag="button", accessible_name=label, text=label,
        is_visible=True, is_enabled=enabled, disabled=not enabled,
    )


def _role_form(field_id: str, label: str, options: list[str]) -> FormDescriptor:
    return FormDescriptor(
        form_id=f"form_{field_id}",
        field_descriptors=[
            FormFieldDescriptor(stable_id=field_id, element_id=field_id, label=label, field_type="select", options=options)
        ],
    )


# ---------------------------------------------------------------------------
# Unit: hint matching / role modifiers / permission ids
# ---------------------------------------------------------------------------


class TestUnit:
    def test_role_modifier_flags_detected_from_observed_text(self):
        assert role_modifier_flags("Guest User") == {"is_guest": True, "is_system": False, "is_default": False, "is_temporary": False}
        assert role_modifier_flags("System Admin")["is_system"] is True
        assert role_modifier_flags("Default Role")["is_default"] is True
        assert role_modifier_flags("Temporary Access")["is_temporary"] is True
        assert role_modifier_flags("Manager") == {"is_guest": False, "is_system": False, "is_default": False, "is_temporary": False}

    def test_page_context_classification(self):
        model = CanonicalPageModel(url="https://x.example/settings/roles", title="Manage Roles")
        context = ActorCandidateBuilder.classify_page_context(model)
        assert context["is_roles_page"] is True
        assert context["is_settings_page"] is True

    def test_access_denied_detection_from_visible_text(self):
        model = CanonicalPageModel(
            url="https://x.example/admin",
            alerts=[AlertDescriptor(text="You do not have permission to view this page", severity="error")],
        )
        assert ActorCandidateBuilder.detect_access_denied(model) is True

    def test_access_denied_not_flagged_on_ordinary_page(self):
        model = CanonicalPageModel(url="https://x.example/", headings=[HeadingDescriptor(text="Welcome", level=1)])
        assert ActorCandidateBuilder.detect_access_denied(model) is False

    def test_placeholder_dropdown_options_are_rejected(self):
        model = CanonicalPageModel(
            url="https://x.example/users/new",
            forms=[_role_form("f1", "Role", ["-- Select a role --", "Admin", "None"])],
        )
        candidates = ActorCandidateBuilder().build(model)
        assert {c.term for c in candidates} == {"admin"}


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------


class TestCandidateExtraction:
    def test_role_dropdown_options_extracted(self):
        model = CanonicalPageModel(
            url="https://x.example/users/new",
            forms=[_role_form("f1", "User Role", ["Admin", "Manager", "Viewer"])],
        )
        terms = {c.term for c in ActorCandidateBuilder().build(model)}
        assert {"admin", "manager", "viewer"} <= terms
        assert all(c.evidence.source_kind == "role_dropdown_option" for c in ActorCandidateBuilder().build(model))

    def test_role_table_column_extracted_including_multi_role_cell(self):
        model = CanonicalPageModel(
            url="https://x.example/settings/users",
            tables=[TableDescriptor(table_id="t1", headers=["Name", "Role"], row_count=2,
                                     sample_rows=[["Alice", "Admin"], ["Bob", "Manager, Support"]])],
        )
        terms = {c.term for c in ActorCandidateBuilder().build(model)}
        assert {"admin", "manager", "support"} <= terms

    def test_non_role_table_yields_no_candidates(self):
        model = CanonicalPageModel(
            url="https://x.example/customers",
            tables=[TableDescriptor(table_id="t1", headers=["Name", "Email"], row_count=1, sample_rows=[["A", "a@x.com"]])],
        )
        assert ActorCandidateBuilder().build(model) == []

    def test_role_ish_navigation_item_extracted(self):
        model = CanonicalPageModel(url="https://x.example/", navigation_regions=[_nav(["Dashboard", "Manage Roles"])])
        terms = {c.term for c in ActorCandidateBuilder().build(model)}
        assert "manage role" in terms

    def test_plain_navigation_not_treated_as_role_evidence(self):
        model = CanonicalPageModel(url="https://x.example/", navigation_regions=[_nav(["Dashboard", "Customers", "Jobs"])])
        assert ActorCandidateBuilder().build(model) == []

    def test_invite_and_approval_context_detection(self):
        invite_model = CanonicalPageModel(url="https://x.example/users/invite", title="Invite a new user")
        assert ActorCandidateBuilder.is_invite_or_creation_context(invite_model) is True
        approval_model = CanonicalPageModel(
            url="https://x.example/requests/1",
            interactive_elements=[_button("b1", "Approve")],
        )
        assert ActorCandidateBuilder.is_approval_context(approval_model) is True
        plain_model = CanonicalPageModel(url="https://x.example/customers", title="Customers")
        assert ActorCandidateBuilder.is_invite_or_creation_context(plain_model) is False
        assert ActorCandidateBuilder.is_approval_context(plain_model) is False

    def test_assignment_targets_extracted(self):
        model = CanonicalPageModel(
            url="https://x.example/jobs/1",
            forms=[_role_form("f1", "Assign to", ["Driver", "Technician"])],
        )
        terms = {c.term for c in ActorCandidateBuilder().assignment_targets(model)}
        assert terms == {"driver", "technician"}


# ---------------------------------------------------------------------------
# Permission discovery
# ---------------------------------------------------------------------------


class TestPermissionDiscovery:
    def test_visible_enabled_control_is_positive_evidence(self):
        model = CanonicalPageModel(url="https://x.example/customers", interactive_elements=[_button("b1", "Export")])
        found = PermissionDiscovery().discover(model)
        assert "can_export" in found
        assert found["can_export"].granted is True

    def test_disabled_control_is_negative_evidence(self):
        model = CanonicalPageModel(
            url="https://x.example/customers",
            interactive_elements=[_button("b1", "Delete Customer", enabled=False)],
        )
        found = PermissionDiscovery().discover(model)
        assert "can_delete_customer" in found
        assert found["can_delete_customer"].granted is False

    def test_form_chrome_verbs_do_not_become_permissions(self):
        model = CanonicalPageModel(url="https://x.example/", interactive_elements=[_button("b1", "Save"), _button("b2", "Cancel")])
        assert PermissionDiscovery().discover(model) == {}

    def test_reachable_settings_page_grants_access_permission(self):
        model = CanonicalPageModel(url="https://x.example/settings", title="Settings")
        found = PermissionDiscovery().discover(model)
        assert found["can_access_settings"].granted is True

    def test_reachable_user_management_page_grants_manage_users(self):
        model = CanonicalPageModel(url="https://x.example/settings/users", title="User Management")
        found = PermissionDiscovery().discover(model)
        assert found["can_manage_users"].granted is True

    def test_network_403_is_negative_permission_evidence(self):
        model = CanonicalPageModel(
            url="https://x.example/customers",
            network_evidence=[
                NetworkEvidence(method="DELETE", http_status=403, text="DELETE https://x.example/api/customers/1", failed=True)
            ],
        )
        found = PermissionDiscovery().discover(model)
        assert "can_delete_customer" in found
        assert found["can_delete_customer"].granted is False
        assert found["can_delete_customer"].negative_evidence[0].source_kind == "network_403"

    def test_network_401_is_negative_permission_evidence(self):
        model = CanonicalPageModel(
            url="https://x.example/reports",
            network_evidence=[NetworkEvidence(method="GET", http_status=401, text="GET https://x.example/api/reports")],
        )
        found = PermissionDiscovery().discover(model)
        assert found["can_view_report"].granted is False

    def test_successful_network_calls_are_not_permission_evidence(self):
        model = CanonicalPageModel(
            url="https://x.example/customers",
            network_evidence=[NetworkEvidence(method="GET", http_status=200, text="GET https://x.example/api/customers")],
        )
        assert PermissionDiscovery().discover(model) == {}

    def test_access_denied_text_is_negative_evidence_for_page_topic(self):
        model = CanonicalPageModel(
            url="https://x.example/admin/reports",
            alerts=[AlertDescriptor(text="Access denied", severity="error")],
        )
        found = PermissionDiscovery().discover(model)
        assert any(pid.startswith("can_access_") and not cand.granted for pid, cand in found.items())

    def test_visible_and_disabled_evidence_together_yield_net_standing(self):
        """Confirmed on one page (visible+enabled), then denied elsewhere
        (disabled) -> more positive than negative -> still net granted."""
        pd = PermissionDiscovery()
        model_a = CanonicalPageModel(url="https://x.example/a", interactive_elements=[_button("b1", "Approve")])
        found_a = pd.discover(model_a)
        assert found_a["can_approve"].granted is True


# ---------------------------------------------------------------------------
# Memory: the real bug this build found + general merge behavior
# ---------------------------------------------------------------------------


class TestMemory:
    def test_session_actor_reaches_confirmed_without_any_named_role_evidence(self):
        """Regression: the "current session" placeholder actor — used for
        every application that never displays its own role name anywhere
        (the common case, live-verified) — was stuck at status=candidate,
        confidence=0.0 forever, because confidence/status were computed only
        from named-role evidence (`supporting_evidence`), which a pure
        session bucket never receives. Session-level evidence (pages,
        permissions, dashboards) must ALSO count."""
        engine = ActorDiscoveryEngine()
        model = CanonicalPageModel(
            url="https://x.example/dashboard",
            title="Dashboard",
            headings=[HeadingDescriptor(text="Dashboard", level=1)],
            interactive_elements=[_button("b1", "Approve Request")],
        )
        engine.observe(model, iteration=1, authenticated=True, login_method="login")
        record = engine.registry.get("current session")
        assert record is not None
        assert record.confidence > 0.0
        assert record.status == "confirmed"

    def test_authenticated_session_with_no_permissions_or_dashboards_is_incomplete(self):
        engine = ActorDiscoveryEngine()
        model = CanonicalPageModel(url="https://x.example/", title="Home")
        engine.observe(model, iteration=1, authenticated=True, login_method="login")
        assert engine.registry.get("current session").status == "incomplete"

    def test_anonymous_session_is_a_legitimate_actor_too(self):
        """An anonymous/guest visitor is a real actor (the task's own "guest
        role" concept) — live-verified on a public, unauthenticated demo
        dashboard. It must reach the SAME statuses an authenticated session
        would, driven by session richness, not be capped merely for lacking
        a login."""
        engine = ActorDiscoveryEngine()
        thin = CanonicalPageModel(url="https://x.example/", title="Home")
        engine.observe(thin, iteration=1, authenticated=False)
        assert engine.registry.get("current session").status == "incomplete"

        rich = CanonicalPageModel(
            url="https://x.example/reports", title="Reports",
            interactive_elements=[_button("b1", "Export")],
        )
        engine.observe(rich, iteration=2, authenticated=False)
        record = engine.registry.get("current session")
        assert record.status == "confirmed"
        assert "anonymous" in record.known_session_types

    def test_repeat_observation_does_not_duplicate_evidence_or_pages(self):
        engine = ActorDiscoveryEngine()
        model = CanonicalPageModel(url="https://x.example/dashboard", title="Dashboard")
        engine.observe(model, iteration=1, authenticated=True)
        engine.observe(model, iteration=2, authenticated=True)
        record = engine.registry.get("current session")
        assert record.known_pages == ["https://x.example/dashboard"]

    def test_named_role_evidence_requires_two_distinct_kinds_to_be_unverified(self):
        model = CanonicalPageModel(
            url="https://x.example/settings/users",
            tables=[TableDescriptor(table_id="t1", headers=["Name", "Role"], row_count=1, sample_rows=[["A", "Admin"]])],
        )
        engine = ActorDiscoveryEngine()
        engine.observe(model, iteration=1)
        admin = engine.registry.get("admin")
        assert admin.status == "candidate"  # only one source kind so far

        model2 = CanonicalPageModel(url="https://x.example/", navigation_regions=[_nav(["Admin"])])
        engine.observe(model2, iteration=2)
        assert engine.registry.get("admin").status == "unverified"

    def test_evidence_never_lost_across_observations(self):
        engine = ActorDiscoveryEngine()
        model1 = CanonicalPageModel(url="https://x.example/a", forms=[_role_form("f1", "Role", ["Manager"])])
        model2 = CanonicalPageModel(url="https://x.example/b", forms=[_role_form("f2", "Role", ["Manager"])])
        engine.observe(model1, iteration=1)
        engine.observe(model2, iteration=2)
        record = engine.registry.get("manager")
        assert len(record.supporting_evidence) == 2

    def test_bare_role_name_in_navigation_only_counted_once_already_known(self):
        """A nav item that IS a role name but carries no role-ish hint word
        of its own ("Manager", unlike "Manage Roles") is only recognized once
        that term is ALREADY known from stronger evidence elsewhere — it must
        not be silently dropped once cross-referencing makes it eligible."""
        engine = ActorDiscoveryEngine()
        engine.observe(CanonicalPageModel(url="https://x.example/a", forms=[_role_form("f1", "Role", ["Manager"])]), iteration=1)
        engine.observe(CanonicalPageModel(url="https://x.example/b", navigation_regions=[_nav(["Manager"])]), iteration=2)
        record = engine.registry.get("manager")
        assert {ev.source_kind for ev in record.supporting_evidence} == {"role_dropdown_option", "navigation_item"}


    def test_session_confidence_reflects_richness_not_just_evidence_kind_count(self):
        """Regression: a "current session" actor with real permissions,
        dashboards, and known entities still reported ~0.1 confidence live
        (only "session_info" ever counts as a distinct evidence KIND for a
        session bucket) — understating how much was actually learned."""
        engine = ActorDiscoveryEngine()
        engine.observe(
            CanonicalPageModel(url="https://x.example/dash", title="Dashboard",
                                interactive_elements=[_button("b1", "Approve")]),
            iteration=1, authenticated=True,
        )
        thin_confidence = engine.registry.get("current session").confidence

        entity_engine = EntityDiscoveryEngine()
        entity_engine.observe(
            CanonicalPageModel(url="https://x.example/customers", title="Customers",
                                navigation_regions=[_nav(["Customers"])],
                                headings=[HeadingDescriptor(text="Customers", level=1)])
        )
        engine.observe(
            CanonicalPageModel(url="https://x.example/customers", title="Customers"),
            iteration=2, authenticated=True, entity_registry=entity_engine.registry,
        )
        richer_confidence = engine.registry.get("current session").confidence
        assert richer_confidence > thin_confidence
        assert richer_confidence >= 0.3


class TestConfidence:
    def test_distinct_sources_beat_repeats(self):
        many_one_kind = [ActorEvidence(source_kind="visible_text", observed_text=f"x{i}") for i in range(10)]
        few_distinct = [
            ActorEvidence(source_kind="role_dropdown_option", observed_text="x"),
            ActorEvidence(source_kind="role_table_cell", observed_text="x"),
        ]
        assert score_confidence(few_distinct) > score_confidence(many_one_kind)


# ---------------------------------------------------------------------------
# Role relationships: co-assignment + hierarchy
# ---------------------------------------------------------------------------


class TestRoleRelationships:
    def test_co_assignment_from_same_table_cell(self):
        engine = ActorDiscoveryEngine()
        model = CanonicalPageModel(
            url="https://x.example/settings/users",
            tables=[TableDescriptor(table_id="t1", headers=["Name", "Role"], row_count=1, sample_rows=[["Bob", "Manager, Support"]])],
        )
        engine.observe(model, iteration=1)
        manager = engine.registry.get("manager")
        support = engine.registry.get("support")
        assert any(r.kind == "co_assigned_with" and r.object_actor_id == support.actor_id for r in manager.relationships)
        assert any(r.kind == "co_assigned_with" and r.object_actor_id == manager.actor_id for r in support.relationships)

    def test_hierarchy_inferred_from_permission_superset(self):
        a = ActorRecord(canonical_name="admin")
        b = ActorRecord(canonical_name="viewer")
        from app.intelligence.actor_discovery.schemas import PermissionCandidate

        a.known_permissions = [
            PermissionCandidate(permission_id="can_view_customer", positive_evidence=[ActorEvidence(source_kind="visible_control", observed_text="x")]),
            PermissionCandidate(permission_id="can_delete_customer", positive_evidence=[ActorEvidence(source_kind="visible_control", observed_text="x")]),
        ]
        b.known_permissions = [
            PermissionCandidate(permission_id="can_view_customer", positive_evidence=[ActorEvidence(source_kind="visible_control", observed_text="x")]),
            PermissionCandidate(permission_id="can_export", negative_evidence=[ActorEvidence(source_kind="disabled_control", observed_text="x")]),
        ]
        findings = RoleRelationshipBuilder().infer_hierarchy([a, b])
        kinds = {(f.subject_term, f.kind, f.object_term) for f in findings}
        assert ("admin", "parent_of", "viewer") in kinds
        assert ("viewer", "inherits_from", "admin") in kinds

    def test_no_hierarchy_inferred_without_enough_permission_evidence(self):
        a = ActorRecord(canonical_name="admin")
        b = ActorRecord(canonical_name="viewer")
        assert RoleRelationshipBuilder().infer_hierarchy([a, b]) == []

    def test_equal_permission_sets_yield_no_hierarchy(self):
        from app.intelligence.actor_discovery.schemas import PermissionCandidate

        a = ActorRecord(canonical_name="editor")
        b = ActorRecord(canonical_name="contributor")
        same_perms = [
            PermissionCandidate(permission_id="can_edit_document", positive_evidence=[ActorEvidence(source_kind="visible_control", observed_text="x")]),
            PermissionCandidate(permission_id="can_view_document", positive_evidence=[ActorEvidence(source_kind="visible_control", observed_text="x")]),
        ]
        a.known_permissions = list(same_perms)
        b.known_permissions = list(same_perms)
        assert RoleRelationshipBuilder().infer_hierarchy([a, b]) == []


# ---------------------------------------------------------------------------
# Actor difference analysis
# ---------------------------------------------------------------------------


class TestActorDifferenceAnalysis:
    def _actor(self, name, *, nav=(), dashboards=(), pages=(), perms=(), entities=(), visibility=()) -> ActorRecord:
        from app.intelligence.actor_discovery.schemas import PermissionCandidate

        record = ActorRecord(canonical_name=name)
        record.known_navigation = list(nav)
        record.known_dashboards = list(dashboards)
        record.known_pages = list(pages)
        record.known_entities = list(entities)
        record.known_visibility = list(visibility)
        record.known_permissions = [
            PermissionCandidate(permission_id=p, positive_evidence=[ActorEvidence(source_kind="visible_control", observed_text="x")])
            for p in perms
        ]
        return record

    def test_single_role_application_has_no_differences_to_report(self):
        """Single-role app: only ever one known actor -> nothing to compare."""
        registry = ActorRegistry()
        registry.memory.records["solo"] = self._actor("solo", nav=["Dashboard"])
        assert registry.all_pairwise_differences() == []

    def test_multi_role_navigation_and_dashboard_differences_detected(self):
        admin = self._actor(
            "admin", nav=["Dashboard", "Users", "Reports"], dashboards=["https://x.example/admin-dashboard"],
            perms=["can_manage_users"],
        )
        viewer = self._actor("viewer", nav=["Dashboard", "Reports"], dashboards=["https://x.example/viewer-dashboard"])
        diff = compare_actors(admin, viewer)
        assert diff.navigation_only_in_a == {"Users"}
        assert diff.dashboards_only_in_a == {"https://x.example/admin-dashboard"}
        assert diff.dashboards_only_in_b == {"https://x.example/viewer-dashboard"}
        assert diff.permissions_only_in_a == {"can_manage_users"}
        assert diff.has_any_difference() is True

    def test_identical_actors_show_no_difference(self):
        a = self._actor("editor", nav=["Dashboard"], perms=["can_edit_document"])
        b = self._actor("contributor", nav=["Dashboard"], perms=["can_edit_document"])
        assert compare_actors(a, b).has_any_difference() is False


# ---------------------------------------------------------------------------
# Registry queries (Planner API)
# ---------------------------------------------------------------------------


class TestRegistryQueries:
    def test_full_partition_across_all_six_query_methods(self):
        engine = ActorDiscoveryEngine()
        # "current session": authenticated, gets permissions+dashboard -> confirmed.
        engine.observe(
            CanonicalPageModel(url="https://x.example/dashboard", title="Dashboard",
                                interactive_elements=[_button("b1", "Approve")]),
            iteration=1, authenticated=True,
        )
        # "admin": corroborated by 2 distinct source kinds -> unverified.
        engine.observe(
            CanonicalPageModel(url="https://x.example/settings/users",
                                tables=[TableDescriptor(table_id="t1", headers=["Name", "Role"], row_count=1, sample_rows=[["A", "Admin"]])]),
            iteration=2,
        )
        engine.observe(CanonicalPageModel(url="https://x.example/", navigation_regions=[_nav(["Admin"])]), iteration=3)
        # "guest": only one source kind -> candidate.
        engine.observe(
            CanonicalPageModel(url="https://x.example/invite",
                                forms=[_role_form("f1", "Role", ["Guest"])]),
            iteration=4,
        )
        registry = engine.registry
        assert {a.canonical_name for a in registry.known_actors()} == {"current session"}
        assert {a.canonical_name for a in registry.unverified_actors()} == {"admin"}
        assert {a.canonical_name for a in registry.unknown_actors()} == {"guest"}
        exploring = {a.canonical_name for a in registry.actors_requiring_exploration()}
        assert exploring == {"admin", "guest"}

    def test_missing_permissions_and_missing_dashboard_queries(self):
        engine = ActorDiscoveryEngine()
        engine.observe(CanonicalPageModel(url="https://x.example/", title="Home"), iteration=1, authenticated=True)
        record = engine.registry.get("current session")
        record.status = "incomplete"  # force into known_actors() without permissions/dashboards
        assert record in engine.registry.actors_missing_permissions()
        assert record in engine.registry.actors_missing_dashboard_understanding()

    def test_run_memory_query_api_degrades_without_registry(self):
        from app.agent.memory import RunMemory
        from app.utils.ids import new_id

        memory = RunMemory(run_id=new_id(), start_url="https://x.example/")
        assert memory.known_actors() == []
        assert memory.unknown_actors() == []
        assert memory.unverified_actors() == []
        assert memory.actors_requiring_exploration() == []
        assert memory.actors_missing_permissions() == []
        assert memory.actors_missing_dashboard_understanding() == []

    def test_run_memory_query_api_passes_through_and_appears_in_snapshot(self):
        from app.agent.memory import RunMemory
        from app.utils.ids import new_id

        engine = ActorDiscoveryEngine()
        engine.observe(
            CanonicalPageModel(url="https://x.example/dashboard", interactive_elements=[_button("b1", "Approve")]),
            iteration=1, authenticated=True,
        )
        memory = RunMemory(run_id=new_id(), start_url="https://x.example/")
        memory.actor_registry = engine.registry
        assert {a.canonical_name for a in memory.known_actors()} == {"current session"}
        snap = memory.memory_snapshot()
        assert "current session" in snap["actors"]["known"]


# ---------------------------------------------------------------------------
# Session infrastructure (no auto-switching — storage/lookup only)
# ---------------------------------------------------------------------------


class TestSessionInfrastructure:
    def test_session_captures_landing_page_dashboard_and_permissions(self):
        engine = ActorDiscoveryEngine()
        engine.observe(
            CanonicalPageModel(url="https://x.example/welcome", title="Welcome"),
            iteration=1, authenticated=True, login_method="login",
        )
        engine.observe(
            CanonicalPageModel(url="https://x.example/dashboard", title="Dashboard",
                                interactive_elements=[_button("b1", "Approve")]),
            iteration=2, authenticated=True, login_method="login",
        )
        session = engine.registry.session_for("current session")
        assert session is not None
        assert session.login_method == "login"
        assert session.landing_page == "https://x.example/welcome"
        assert session.dashboard_url == "https://x.example/dashboard"
        assert "can_approve" in session.known_permission_ids
        assert session.last_explored_url == "https://x.example/dashboard"

    def test_multiple_actor_sessions_tracked_independently(self):
        """Infrastructure for returning to any known actor later — no
        automatic switching is implemented (nor tested) here, only that
        distinct actor sessions don't clobber each other."""
        registry = ActorRegistry()
        memory = registry.memory
        record_a = memory.get_or_create("admin")
        record_a.known_session_types.append("authenticated")
        record_a.known_pages.append("https://x.example/a-dash")
        from app.intelligence.actor_discovery.schemas import ActorSession

        record_a.session = ActorSession(actor_id=record_a.actor_id, landing_page="https://x.example/a-home")
        record_b = memory.get_or_create("viewer")
        record_b.session = ActorSession(actor_id=record_b.actor_id, landing_page="https://x.example/b-home")

        sessions = registry.all_sessions()
        assert sessions["admin"].landing_page == "https://x.example/a-home"
        assert sessions["viewer"].landing_page == "https://x.example/b-home"


# ---------------------------------------------------------------------------
# Integration: two structurally different applications, zero app-specific code
# ---------------------------------------------------------------------------


class TestMultiApplicationIntegration:
    def test_field_service_app_with_role_dropdown_and_permission_page(self):
        engine = ActorDiscoveryEngine()
        engine.observe(
            CanonicalPageModel(
                url="https://svc.example/dashboard", title="Dashboard",
                interactive_elements=[_button("b1", "Approve Job"), _button("b2", "Delete Customer", enabled=False)],
            ),
            iteration=1, authenticated=True, login_method="login",
        )
        engine.observe(
            CanonicalPageModel(
                url="https://svc.example/settings/users/new", title="Invite User",
                forms=[_role_form("f1", "User Role", ["Administrator", "Field Technician", "Dispatcher"])],
            ),
            iteration=2,
        )
        registry = engine.registry
        assert {"administrator", "field technician", "dispatcher"} <= {a.canonical_name for a in registry.unknown_actors()}
        session = registry.get("current session")
        assert session.status == "confirmed"
        assert "can_approve_job" in session.granted_permission_ids()
        assert "can_delete_customer" not in session.granted_permission_ids()

    def test_logistics_app_with_401_and_different_dashboards(self):
        """A completely different domain, discovered with the SAME code."""
        engine = ActorDiscoveryEngine()
        engine.observe(
            CanonicalPageModel(
                url="https://freight.example/ops-dashboard", title="Operations Dashboard",
                network_evidence=[NetworkEvidence(method="DELETE", http_status=401, text="DELETE https://freight.example/api/shipments/1")],
            ),
            iteration=1, authenticated=True, login_method="sso",
        )
        session = engine.registry.get("current session")
        assert session.known_dashboards == ["https://freight.example/ops-dashboard"]
        assert session.session.login_method == "sso"
        assert any(not p.granted for p in session.known_permissions if "shipment" in p.permission_id)

    def test_no_application_specific_vocabulary_in_package_code(self):
        """Same discipline as entity_discovery's contract test — no business
        role name is ever a real string-literal constant in this package's
        code (docstrings/comments excluded)."""
        import ast

        import app.intelligence.actor_discovery as pkg

        # NB: deliberately excludes "admin"/"administration" — those are
        # legitimate generic PAGE-TOPIC hint words (an "administration area"),
        # the same structural role entity_discovery's own UI_STOPWORDS gives
        # "admin". The contract this test enforces is "no ROLE NAME is
        # hardcoded", not "the word admin can never appear".
        role_words = {
            "administrator", "manager", "customer", "driver",
            "dispatcher", "employee", "technician", "viewer", "editor",
            "supervisor", "operator", "agent", "clerk", "owner",
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
                    if word.strip(".,;:!?\"'()") in role_words:
                        offenders.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}")
        assert not offenders, "application-specific role vocabulary in actor_discovery code: " + "; ".join(offenders)


# ---------------------------------------------------------------------------
# Live integration: real Playwright page -> PerceptionEngine -> ActorDiscovery
# ---------------------------------------------------------------------------


async def test_live_actor_discovery_from_real_page(tmp_path):
    try:
        from playwright.async_api import async_playwright
    except Exception:
        pytest.skip("Playwright not importable")

    from app.perception.engine import PerceptionEngine

    html = """
    <!doctype html><html><head><title>Invite a teammate</title></head><body>
    <h1>Invite a teammate</h1>
    <form>
      <label for="role">Role</label>
      <select id="role" name="role">
        <option value="">Select a role</option>
        <option>Admin</option>
        <option>Editor</option>
        <option>Viewer</option>
      </select>
    </form>
    <button disabled>Delete Workspace</button>
    <button>Export Data</button>
    </body></html>
    """
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=html))
                await page.goto("https://actor-demo.local/invite")
                model = await PerceptionEngine().observe(page)
            finally:
                await browser.close()
    except Exception as exc:
        pytest.skip(f"Live Chromium unavailable: {type(exc).__name__}: {exc}")
        return

    engine = ActorDiscoveryEngine()
    summary = engine.observe(model, iteration=1, authenticated=True)
    registry = engine.registry
    role_terms = {a.canonical_name for a in registry.unknown_actors()}
    assert {"admin", "editor", "viewer"} <= role_terms
    session = registry.get("current session")
    assert "can_export_data" in session.granted_permission_ids()
    assert "can_delete_workspace" not in session.granted_permission_ids()
    assert summary["session_actor"] == "current session"
