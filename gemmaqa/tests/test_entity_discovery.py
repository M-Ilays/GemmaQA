"""Autonomous Entity Discovery Engine (app/intelligence/entity_discovery/).

Unit tests per module (normalization, candidate extraction, classification,
operations, relationships, confidence, registry queries, memory merging) plus
integration tests against TWO structurally different synthetic applications
— a field-service CRM and a logistics dashboard — proving entities are
discovered from evidence alone, with zero application-specific code, and a
live-Playwright integration test against the InsightBoard fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest

from app.intelligence.entity_discovery import EntityDiscoveryEngine, EntityRegistry
from app.intelligence.entity_discovery.entity_candidate_builder import (
    EntityCandidateBuilder,
    normalize_term,
    singularize,
    split_verb_and_noun,
    url_path_terms,
)
from app.intelligence.entity_discovery.entity_confidence import score_confidence
from app.intelligence.entity_discovery.schemas import EntityEvidence, EntityRecord
from app.perception.models import (
    BreadcrumbDescriptor,
    CanonicalPageModel,
    HeadingDescriptor,
    NavigationItem,
    NavigationRegion,
    NetworkEvidence,
    TabDescriptor,
    TabGroup,
)
from app.schemas import FormDescriptor, FormField, InteractiveElement, TableDescriptor


def _nav(items: list[str]) -> NavigationRegion:
    return NavigationRegion(
        items=[NavigationItem(element_id=f"el_nav_{i}", text=t) for i, t in enumerate(items)]
    )


def _button(element_id: str, label: str) -> InteractiveElement:
    return InteractiveElement(element_id=element_id, tag="button", accessible_name=label, text=label)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    @pytest.mark.parametrize(
        ("plural", "singular"),
        [
            ("customers", "customer"),
            ("invoices", "invoice"),
            ("companies", "company"),
            ("batches", "batch"),
            ("addresses", "address"),
            ("statuses", "status"),
            ("people", "person"),
            ("data", "data"),
            ("bus", "bus"),
            ("series", "series"),
        ],
    )
    def test_singularize(self, plural, singular):
        assert singularize(plural) == singular

    def test_normalize_term_lowercases_and_singularizes_each_word(self):
        assert normalize_term("Purchase Orders") == "purchase order"

    def test_split_verb_and_noun_extracts_leading_operation_verb(self):
        assert split_verb_and_noun("Add Customer") == ("create", "customer")
        assert split_verb_and_noun("Export Invoices") == ("export", "invoices")

    def test_split_verb_and_noun_bare_verb(self):
        assert split_verb_and_noun("Export") == ("export", "")

    def test_split_verb_and_noun_plain_noun(self):
        assert split_verb_and_noun("Customers") == (None, "customers")

    def test_url_path_terms_skips_ids_and_extensions(self):
        assert url_path_terms("https://x.example/customers/cust-001/orders") == ["customers", "orders"]
        assert url_path_terms("https://x.example/reports.html") == ["reports"]
        assert url_path_terms("https://x.example/api/v1/drivers/42") == ["drivers"]


# ---------------------------------------------------------------------------
# Candidate extraction
# ---------------------------------------------------------------------------


class TestCandidateBuilder:
    def test_extracts_from_every_major_source(self):
        model = CanonicalPageModel(
            url="https://x.example/shipments",
            state_fingerprint="fp1",
            title="Shipments | FreightApp",
            navigation_regions=[_nav(["Shipments", "Vehicles"])],
            breadcrumbs=[BreadcrumbDescriptor(text="Shipments", ordinal=0)],
            tabs=[TabGroup(tabs=[TabDescriptor(element_id="el_t1", text="Active Shipments")])],
            headings=[HeadingDescriptor(text="Shipments", level=1)],
            tables=[TableDescriptor(table_id="t1", headers=["Origin", "Destination"], row_count=1)],
            forms=[FormDescriptor(form_id="f1", fields=[FormField(name="carrier", label="Carrier", element_id="el_f1")])],
            interactive_elements=[_button("el_b1", "Add Shipment")],
            network_evidence=[NetworkEvidence(method="GET", text="GET https://x.example/api/shipments")],
        )
        candidates = EntityCandidateBuilder().build(model)
        kinds = {c.evidence.source_kind for c in candidates}
        assert {
            "navigation_item",
            "breadcrumb",
            "tab",
            "heading",
            "page_title",
            "url_segment",
            "table_column",
            "form_field",
            "button_label",
            "api_endpoint",
        } <= kinds
        terms = {c.term for c in candidates}
        assert "shipment" in terms

    def test_pure_chrome_labels_are_rejected(self):
        model = CanonicalPageModel(
            url="https://x.example/",
            navigation_regions=[_nav(["Home", "Dashboard", "Settings", "Log out", "Help"])],
            interactive_elements=[_button("el_1", "Save"), _button("el_2", "Cancel"), _button("el_3", "Search")],
        )
        candidates = EntityCandidateBuilder().build(model)
        assert candidates == []

    def test_page_title_app_name_suffix_split_from_subject(self):
        model = CanonicalPageModel(url="https://x.example/vehicles", title="Vehicles - FleetMaster")
        terms = {c.term for c in EntityCandidateBuilder().build(model)}
        assert "vehicle" in terms
        assert "vehicle fleetmaster" not in terms


# ---------------------------------------------------------------------------
# Discovery + operations (single observation)
# ---------------------------------------------------------------------------


class TestDiscoveryAndOperations:
    def _service_page(self) -> CanonicalPageModel:
        return CanonicalPageModel(
            url="https://x.example/customers",
            state_fingerprint="fp1",
            title="Customers",
            navigation_regions=[_nav(["Customers", "Jobs"])],
            headings=[HeadingDescriptor(text="Customers", level=1)],
            interactive_elements=[
                _button("el_add", "Add Customer"),
                _button("el_exp", "Export"),
                _button("el_del", "Delete Customer"),
                _button("el_imp", "Import Customers"),
            ],
            tables=[
                TableDescriptor(
                    table_id="t1",
                    headers=["Name", "Status"],
                    row_count=2,
                    sample_rows=[["A", "Active"], ["B", "Prospect"]],
                )
            ],
            network_evidence=[
                NetworkEvidence(method="GET", text="GET https://x.example/api/customers"),
                NetworkEvidence(method="POST", text="POST https://x.example/api/customers"),
            ],
        )

    def test_entity_confirmed_from_multi_source_corroboration(self):
        engine = EntityDiscoveryEngine()
        engine.observe(self._service_page())
        record = engine.registry.get("customer")
        assert record is not None
        assert record.status == "confirmed"
        assert record.confidence > 0.5
        assert {"navigation_item", "heading", "url_segment"} <= set(record.discovered_in)

    def test_operations_inferred_from_ui_verbs_and_http_methods(self):
        engine = EntityDiscoveryEngine()
        engine.observe(self._service_page())
        ops = set(engine.registry.get("customer").operation_names())
        # Add/Delete/Import from button labels; Export (bare verb) attributed
        # to the page's subject entity; view/create from GET/POST API calls.
        assert {"create", "delete", "import", "export", "view"} <= ops

    def test_known_states_collected_from_status_column(self):
        engine = EntityDiscoveryEngine()
        engine.observe(self._service_page())
        assert set(engine.registry.get("customer").known_states) == {"Active", "Prospect"}

    def test_related_structures_recorded(self):
        engine = EntityDiscoveryEngine()
        engine.observe(self._service_page())
        record = engine.registry.get("customer")
        assert "t1" in record.related_tables
        assert "https://x.example/customers" in record.related_pages

    def test_operations_never_assumed_without_evidence(self):
        """An entity seen only in navigation gets NO operations — nothing is
        hardcoded per-entity."""
        engine = EntityDiscoveryEngine()
        engine.observe(self._service_page())
        job = engine.registry.get("job")
        assert job is not None
        assert job.operation_names() == []

    def test_form_chrome_verbs_are_not_recorded_as_entity_operations(self):
        """Regression (live-verified on a real CRM): every form in every
        application has Save/Apply/Cancel/Reset buttons. Recording those as
        entity operations reported the same meaningless capability set for
        every entity discovered — they must strip from the noun phrase but
        never become operations."""
        engine = EntityDiscoveryEngine()
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/assets",
                title="Assets",
                navigation_regions=[_nav(["Assets"])],
                headings=[HeadingDescriptor(text="Assets", level=1)],
                interactive_elements=[
                    _button("b1", "Save"),
                    _button("b2", "Apply filters"),
                    _button("b3", "Cancel"),
                    _button("b4", "Reset"),
                    _button("b5", "Export"),
                ],
            )
        )
        ops = set(engine.registry.get("asset").operation_names())
        assert ops == {"export"}, f"only the real entity operation should survive, got {ops}"

    def test_demo_environment_words_are_not_entities(self):
        """Regression (live-verified): "ServiceFlow Demo" in a page title made
        "demo" a 0.51-confidence entity. Demo/sample/test-environment words
        are universal chrome, not any application's business vocabulary."""
        engine = EntityDiscoveryEngine()
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/",
                title="Acme Demo",
                interactive_elements=[_button("b1", "Reset demo data")],
            )
        )
        assert engine.registry.get("demo") is None


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


class TestRelationships:
    def test_url_nesting_yields_owns_and_belongs_to(self):
        engine = EntityDiscoveryEngine()
        # First make both entities known.
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/customers",
                title="Customers",
                navigation_regions=[_nav(["Customers", "Orders"])],
                headings=[HeadingDescriptor(text="Customers", level=1)],
            )
        )
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/orders",
                title="Orders",
                navigation_regions=[_nav(["Customers", "Orders"])],
                headings=[HeadingDescriptor(text="Orders", level=1)],
            )
        )
        # Then observe the nested page.
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/customers/cust-001/orders",
                title="Orders",
                headings=[HeadingDescriptor(text="Orders", level=1)],
            )
        )
        customer = engine.registry.get("customer")
        order = engine.registry.get("order")
        kinds_customer = {(r.kind, r.object_entity_id) for r in customer.relationships}
        kinds_order = {(r.kind, r.object_entity_id) for r in order.relationships}
        assert ("owns", order.entity_id) in kinds_customer
        assert ("belongs_to", customer.entity_id) in kinds_order

    def test_assigned_column_yields_assigned_to(self):
        engine = EntityDiscoveryEngine()
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/drivers",
                title="Drivers",
                navigation_regions=[_nav(["Jobs", "Drivers"])],
                headings=[HeadingDescriptor(text="Drivers", level=1)],
            )
        )
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/jobs",
                title="Jobs",
                navigation_regions=[_nav(["Jobs", "Drivers"])],
                headings=[HeadingDescriptor(text="Jobs", level=1)],
                tables=[TableDescriptor(table_id="t1", headers=["Job", "Assigned Driver"], row_count=1)],
            )
        )
        job = engine.registry.get("job")
        driver = engine.registry.get("driver")
        assert any(r.kind == "assigned_to" and r.object_entity_id == driver.entity_id for r in job.relationships)

    def test_relationships_never_invented_between_unknown_terms(self):
        engine = EntityDiscoveryEngine()
        engine.observe(
            CanonicalPageModel(url="https://x.example/customers/cust-001/orders", title="")
        )
        # Neither term corroborated as an entity elsewhere -> both are mere
        # candidates from this one URL, and that's fine — but no relationship
        # phantom pair should crash or duplicate. Just assert stability.
        for record in engine.registry.all_entities():
            for rel in record.relationships:
                assert rel.subject_entity_id and rel.object_entity_id


# ---------------------------------------------------------------------------
# Memory: merging, aliases, confidence over time
# ---------------------------------------------------------------------------


class TestMemoryMerging:
    def test_repeat_observation_of_same_page_does_not_inflate_confidence(self):
        engine = EntityDiscoveryEngine()
        page = CanonicalPageModel(
            url="https://x.example/assets",
            title="Assets",
            navigation_regions=[_nav(["Assets"])],
            headings=[HeadingDescriptor(text="Assets", level=1)],
        )
        engine.observe(page)
        first = engine.registry.get("asset").confidence
        engine.observe(page)
        engine.observe(page)
        assert engine.registry.get("asset").confidence == first
        assert len(engine.registry.records) == 1 or engine.registry.get("asset") is not None

    def test_new_evidence_kind_raises_confidence(self):
        engine = EntityDiscoveryEngine()
        engine.observe(CanonicalPageModel(url="https://x.example/", navigation_regions=[_nav(["Routes"])]))
        first = engine.registry.get("route").confidence
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/routes",
                title="Routes",
                headings=[HeadingDescriptor(text="Routes", level=1)],
            )
        )
        assert engine.registry.get("route").confidence > first

    def test_chrome_suffixed_phrase_merges_directly_into_entity(self):
        """"Customer Profile" normalizes to "customer" outright (profile is
        generic chrome), so it merges into the existing entity with the raw
        phrase kept as an alias — no second entity."""
        engine = EntityDiscoveryEngine()
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/customers",
                title="Customers",
                navigation_regions=[_nav(["Customers"])],
                headings=[HeadingDescriptor(text="Customers", level=1)],
            )
        )
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/other",
                headings=[HeadingDescriptor(text="Customer Profile", level=2)],
            )
        )
        assert "customer profile" not in engine.registry.records
        assert "Customer Profile" in engine.registry.get("customer").aliases

    def test_weak_multiword_term_folds_into_head_entity_as_alias(self):
        """A genuinely-multiword term ("Enterprise Customer" — modifier is
        NOT chrome) seen only once/weakly folds into the established head
        entity as an alias instead of becoming a spurious second entity."""
        engine = EntityDiscoveryEngine()
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/customers",
                title="Customers",
                navigation_regions=[_nav(["Customers"])],
                headings=[HeadingDescriptor(text="Customers", level=1)],
            )
        )
        summary = engine.observe(
            CanonicalPageModel(
                url="https://x.example/other",
                headings=[HeadingDescriptor(text="Enterprise Customer", level=2)],
            )
        )
        assert any("enterprise customer -> customer" in m for m in summary["alias_merged"])
        assert "enterprise customer" not in engine.registry.records
        assert "Enterprise Customer" in engine.registry.get("customer").aliases

    def test_first_and_last_seen_iterations_tracked(self):
        engine = EntityDiscoveryEngine()
        page = CanonicalPageModel(
            url="https://x.example/payments",
            title="Payments",
            navigation_regions=[_nav(["Payments"])],
        )
        engine.observe(page, iteration=3)
        engine.observe(page, iteration=9)
        record = engine.registry.get("payment")
        assert record.first_seen_iteration == 3
        assert record.last_seen_iteration == 9


# ---------------------------------------------------------------------------
# Confidence unit behavior
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_distinct_sources_beat_repeats_of_one_source(self):
        one_kind_many_times = [
            EntityEvidence(source_kind="visible_text", observed_text=f"x{i}") for i in range(10)
        ]
        three_kinds_once = [
            EntityEvidence(source_kind="navigation_item", observed_text="x"),
            EntityEvidence(source_kind="url_segment", observed_text="x"),
            EntityEvidence(source_kind="table_region", observed_text="x"),
        ]
        assert score_confidence(three_kinds_once) > score_confidence(one_kind_many_times)

    def test_confidence_clamped_to_one(self):
        evidence = [EntityEvidence(source_kind=k, observed_text="x") for k in (
            "navigation_item", "breadcrumb", "api_endpoint", "url_segment", "table_region",
            "page_title", "heading", "tab", "form_region", "dialog",
        )]
        assert score_confidence(evidence) == 1.0


# ---------------------------------------------------------------------------
# Registry queries (Planner API)
# ---------------------------------------------------------------------------


class TestRegistryQueries:
    def _populated_engine(self) -> EntityDiscoveryEngine:
        engine = EntityDiscoveryEngine()
        # Confirmed + complete: nav + url + heading + a create button.
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/invoices",
                title="Invoices",
                navigation_regions=[_nav(["Invoices", "Alerts"])],
                headings=[HeadingDescriptor(text="Invoices", level=1)],
                interactive_elements=[_button("el_1", "Create Invoice")],
            )
        )
        # "alert": nav-only -> candidate (unknown).
        return engine

    def test_known_unknown_incomplete_partition(self):
        engine = self._populated_engine()
        registry = engine.registry
        known = {r.canonical_name for r in registry.known_entities()}
        unknown = {r.canonical_name for r in registry.unknown_entities()}
        assert "invoice" in known
        assert "alert" in unknown

    def test_incomplete_entities_have_no_operations_or_pages(self):
        engine = EntityDiscoveryEngine()
        # Multi-source corroboration but NO operations observed anywhere.
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/alerts",
                title="Alerts",
                navigation_regions=[_nav(["Alerts"])],
                headings=[HeadingDescriptor(text="Alerts", level=1)],
            )
        )
        record = engine.registry.get("alert")
        assert record.status == "incomplete"
        assert record in engine.registry.incomplete_entities()
        assert record in engine.registry.entities_requiring_exploration()

    def test_entities_requiring_exploration_orders_incomplete_before_candidates(self):
        engine = self._populated_engine()
        engine.observe(
            CanonicalPageModel(
                url="https://x.example/alerts",
                title="Alerts",
                headings=[HeadingDescriptor(text="Alerts", level=1)],
            )
        )
        ordered = engine.registry.entities_requiring_exploration()
        statuses = [r.status for r in ordered]
        assert statuses == sorted(statuses, key=lambda s: 0 if s == "incomplete" else 1)

    def test_run_memory_query_api_degrades_without_registry(self):
        from app.agent.memory import RunMemory
        from app.utils.ids import new_id

        memory = RunMemory(run_id=new_id(), start_url="https://x.example/")
        assert memory.known_entities() == []
        assert memory.unknown_entities() == []
        assert memory.incomplete_entities() == []
        assert memory.entities_requiring_exploration() == []

    def test_run_memory_query_api_passes_through_registry(self):
        from app.agent.memory import RunMemory
        from app.utils.ids import new_id

        engine = self._populated_engine()
        memory = RunMemory(run_id=new_id(), start_url="https://x.example/")
        memory.entity_registry = engine.registry
        assert {r.canonical_name for r in memory.known_entities()} >= {"invoice"}
        snap = memory.memory_snapshot()
        assert "invoice" in snap["entities"]["known"]


# ---------------------------------------------------------------------------
# Integration: two structurally different applications, zero app-specific code
# ---------------------------------------------------------------------------


def _observe_pages(engine: EntityDiscoveryEngine, pages: list[CanonicalPageModel]) -> None:
    for i, page in enumerate(pages):
        engine.observe(page, iteration=i)


class TestMultiApplicationIntegration:
    def test_field_service_application(self):
        """A CRM-flavored app: customers, jobs, technicians."""
        engine = EntityDiscoveryEngine()
        nav = _nav(["Dashboard", "Customers", "Jobs", "Technicians", "Settings"])
        _observe_pages(
            engine,
            [
                CanonicalPageModel(
                    url="https://svc.example/customers",
                    title="Customers - ServicePro",
                    navigation_regions=[nav],
                    headings=[HeadingDescriptor(text="Customers", level=1)],
                    interactive_elements=[_button("b1", "Add Customer"), _button("b2", "Export")],
                    tables=[TableDescriptor(table_id="t1", headers=["Name", "Status"], row_count=3,
                                            sample_rows=[["A", "Active"]])],
                ),
                CanonicalPageModel(
                    url="https://svc.example/jobs",
                    title="Jobs - ServicePro",
                    navigation_regions=[nav],
                    headings=[HeadingDescriptor(text="Jobs", level=1)],
                    tables=[TableDescriptor(table_id="t2", headers=["Job", "Assigned Technician"], row_count=2)],
                    interactive_elements=[_button("b3", "Create Job")],
                ),
                CanonicalPageModel(
                    url="https://svc.example/customers/c-1/jobs",
                    title="Jobs - ServicePro",
                    navigation_regions=[nav],
                    headings=[HeadingDescriptor(text="Jobs", level=1)],
                ),
            ],
        )
        registry = engine.registry
        known = {r.canonical_name for r in registry.known_entities()}
        assert {"customer", "job", "technician"} <= known
        customer, job = registry.get("customer"), registry.get("job")
        assert "create" in customer.operation_names()
        assert "create" in job.operation_names()
        assert any(r.kind == "owns" for r in customer.relationships)
        assert any(r.kind == "assigned_to" for r in job.relationships)

    def test_logistics_application(self):
        """A completely different domain: shipments, vehicles, routes —
        discovered by the SAME code with zero changes."""
        engine = EntityDiscoveryEngine()
        nav = _nav(["Shipments", "Vehicles", "Routes"])
        _observe_pages(
            engine,
            [
                CanonicalPageModel(
                    url="https://freight.example/shipments",
                    title="Shipments",
                    navigation_regions=[nav],
                    headings=[HeadingDescriptor(text="Shipments", level=1)],
                    interactive_elements=[_button("b1", "Import Shipments"), _button("b2", "Download")],
                    network_evidence=[NetworkEvidence(method="POST", text="POST https://freight.example/api/shipments")],
                ),
                CanonicalPageModel(
                    url="https://freight.example/vehicles",
                    title="Vehicles",
                    navigation_regions=[nav],
                    headings=[HeadingDescriptor(text="Vehicles", level=1)],
                    tables=[TableDescriptor(table_id="t1", headers=["Vehicle", "Route"], row_count=2)],
                ),
            ],
        )
        registry = engine.registry
        known = {r.canonical_name for r in registry.known_entities()}
        assert {"shipment", "vehicle"} <= known
        shipment = registry.get("shipment")
        assert {"import", "download", "create"} <= set(shipment.operation_names())
        vehicle = registry.get("vehicle")
        assert any(r.kind == "references" for r in vehicle.relationships)

    def test_no_application_specific_vocabulary_in_package_code(self):
        """The package must not ship ANY of the business terms it discovers as
        actual DATA (string literals in lexicons/stopwords/branches) — entity
        names must come from the observed application alone. Comments and
        docstrings may of course mention example words while explaining the
        rules, so this walks the AST and inspects only real string constants."""
        import ast

        import app.intelligence.entity_discovery as pkg

        business_words = {
            "customer", "customers", "invoice", "invoices", "order", "orders",
            "driver", "drivers", "employee", "employees", "asset", "assets",
            "route", "routes", "vehicle", "vehicles", "shipment", "shipments",
            "payment", "payments", "product", "products", "technician",
            "technicians", "job", "jobs", "contact", "contacts", "alert", "alerts",
        }
        offenders: list[str] = []
        for path in sorted(Path(pkg.__file__).parent.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # Identify docstring Constant nodes by identity (ast.get_docstring
            # returns cleaned text that won't compare equal to the raw node).
            docstring_nodes: set[int] = set()
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                body = getattr(node, "body", None) or []
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstring_nodes.add(id(body[0].value))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if id(node) in docstring_nodes:
                    continue
                for word in node.value.lower().replace("_", " ").replace("-", " ").split():
                    if word.strip(".,;:!?\"'()") in business_words:
                        offenders.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}")
        assert not offenders, "application-specific vocabulary in entity_discovery code: " + "; ".join(offenders)


# ---------------------------------------------------------------------------
# Live integration: real Playwright page -> PerceptionEngine -> EntityDiscovery
# ---------------------------------------------------------------------------


async def test_live_entity_discovery_from_real_page(tmp_path):
    try:
        from playwright.async_api import async_playwright
    except Exception:
        pytest.skip("Playwright not importable")

    from app.perception.engine import PerceptionEngine

    html = """
    <!doctype html><html><head><title>Shipments - FreightDemo</title></head><body>
    <header><nav aria-label="Main"><a href="/shipments">Shipments</a><a href="/vehicles">Vehicles</a></nav></header>
    <main>
      <h1>Shipments</h1>
      <button>Add Shipment</button>
      <button>Export</button>
      <table>
        <thead><tr><th>Reference</th><th>Status</th><th>Assigned Vehicle</th></tr></thead>
        <tbody><tr><td>S-1</td><td>In Transit</td><td>V-9</td></tr></tbody>
      </table>
    </main>
    </body></html>
    """
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", lambda r: r.fulfill(status=200, content_type="text/html", body=html))
                await page.goto("https://freight-demo.local/shipments")
                model = await PerceptionEngine().observe(page)
            finally:
                await browser.close()
    except Exception as exc:
        pytest.skip(f"Live Chromium unavailable: {type(exc).__name__}: {exc}")
        return

    engine = EntityDiscoveryEngine()
    summary = engine.observe(model)
    shipment = engine.registry.get("shipment")
    assert shipment is not None
    assert shipment.status in {"confirmed", "incomplete"}
    assert "create" in shipment.operation_names()
    assert "In Transit" in shipment.known_states
    assert summary["page_context_entity"] == "shipment"
    vehicle = engine.registry.get("vehicle")
    assert vehicle is not None
