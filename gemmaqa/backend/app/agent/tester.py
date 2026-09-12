"""Safe exploratory test proposal and recording with scenario_key dedup."""

from __future__ import annotations

from app.agent.test_data import values_for_field
from app.application.store import form_fingerprint, scenario_key
from app.application.url_normalize import normalize_url
from app.gemma.base import GemmaProvider
from app.schemas import PageState, TestExecution, TestScenario
from app.utils.ids import new_id
from app.utils.logging import get_logger

logger = get_logger("agent.tester")


class Tester:
    """Propose and track safe exploratory test scenarios."""

    def __init__(self, gemma: GemmaProvider | None = None) -> None:
        self.gemma = gemma
        self.scenarios: list[TestScenario] = []
        self.executions: list[TestExecution] = []
        self._scenario_keys: set[str] = set()
        # Canonical page keys that already received a generate_test_scenarios
        # attempt this run. Re-observe of the same page must not pay for a
        # second Ollama call (Contact List login was observed twice; the
        # second call added nothing and timed out).
        self._pages_with_gemma_scenarios: set[str] = set()

    def _page_key(self, page_state: PageState, app_store=None) -> str:
        canonical = normalize_url(page_state.url) or page_state.url
        if app_store:
            page = app_store.model.page_by_url(canonical)
            if page:
                return page.id
        return canonical

    async def propose_for_page(
        self,
        page_state: PageState,
        run_id: str,
        *,
        allow_controlled_writes: bool = False,
        app_store=None,
        skip_llm: bool = False,
    ) -> list[TestScenario]:
        created: list[TestScenario] = []
        page_id = self._page_key(page_state, app_store)

        for item in self._deterministic_specs(page_state):
            key = scenario_key(
                page_id=page_id,
                form_fingerprint=item.get("form_fp", ""),
                field_key=item.get("field_key", ""),
                category=item["category"],
                data_class=item.get("data_class", "generic"),
            )
            if key in self._scenario_keys:
                continue
            if app_store and any(s.scenario_key == key for s in app_store.model.scenarios):
                self._scenario_keys.add(key)
                continue
            self._scenario_keys.add(key)
            sc = TestScenario(
                test_id=new_id(),
                title=item["title"],
                description=item.get("description", ""),
                category=item["category"],
                priority=item.get("priority", "medium"),
                steps=list(item.get("steps") or []),
                expected_results=list(item.get("expected") or []),
            )
            created.append(sc)
            self.scenarios.append(sc)
            self.executions.append(
                TestExecution(
                    execution_id=new_id(),
                    test_id=sc.test_id,
                    run_id=run_id,
                    status="not_run",
                    notes=f"Generated for {page_state.url}; not executed unless a run step performs it.",
                )
            )
            if app_store:
                app_store.add_scenario(
                    title=sc.title,
                    category=sc.category,
                    page_id=page_id,
                    form_fp=item.get("form_fp", ""),
                    field_key=item.get("field_key", ""),
                    data_class=item.get("data_class", "generic"),
                    description=sc.description,
                    steps=sc.steps,
                    expected=sc.expected_results,
                )

        if skip_llm:
            logger.info(
                "Skipping generate_test_scenarios on %s (live loop must not block on Gemma)",
                page_id,
            )
        elif self.gemma is not None and page_id in self._pages_with_gemma_scenarios:
            logger.info(
                "Skipping generate_test_scenarios; scenarios already proposed for %s",
                page_id,
            )
        elif self.gemma is not None:
            self._pages_with_gemma_scenarios.add(page_id)
            try:
                proposals = await self.gemma.generate_test_scenarios(
                    page_state,
                    {
                        "run_id": run_id,
                        "safe_only": True,
                        "forbidden": [
                            "sql injection",
                            "auth bypass",
                            "dos",
                            "data theft",
                            "destructive",
                        ],
                    },
                )
                for raw in proposals:
                    title = str(raw.get("title", "")).lower()
                    if any(
                        bad in title
                        for bad in ("injection", "bypass", "dos", "flood", "steal", "exploit")
                    ):
                        continue
                    # Skip AI duplicates of deterministic required-field / smoke scenarios
                    if any(
                        p in title
                        for p in (
                            "inspect required",
                            "required field",
                            "page title present",
                            "empty value on el_",
                        )
                    ):
                        continue
                    cat = str(raw.get("category", "exploratory"))
                    field_key = title[:40]
                    key = scenario_key(
                        page_id=page_id,
                        form_fingerprint="",
                        field_key=field_key,
                        category=cat,
                        data_class="ai",
                    )
                    if key in self._scenario_keys:
                        continue
                    if app_store and any(s.scenario_key == key for s in app_store.model.scenarios):
                        self._scenario_keys.add(key)
                        continue
                    self._scenario_keys.add(key)
                    sc = TestScenario(
                        test_id=new_id(),
                        title=str(raw.get("title", "Untitled scenario")),
                        description=str(raw.get("description", "")),
                        category=cat,
                        priority=str(raw.get("priority", "medium")),
                        preconditions=list(raw.get("preconditions") or []),
                        steps=list(raw.get("steps") or []),
                        expected_results=list(raw.get("expected_results") or []),
                    )
                    created.append(sc)
                    self.scenarios.append(sc)
                    self.executions.append(
                        TestExecution(
                            execution_id=new_id(),
                            test_id=sc.test_id,
                            run_id=run_id,
                            status="not_run",
                            notes=f"Generated for {page_state.url}",
                        )
                    )
                    if app_store:
                        app_store.add_scenario(
                            title=sc.title,
                            category=sc.category,
                            page_id=page_id,
                            field_key=field_key,
                            data_class="ai",
                            description=sc.description,
                            steps=sc.steps,
                            expected=sc.expected_results,
                        )
            except Exception as exc:
                logger.warning("Gemma test proposal failed: %s", type(exc).__name__)

        logger.info("Proposed %s new scenarios for %s", len(created), page_state.url)
        return created

    def _field_display_name(self, field) -> str:
        for attr in ("label", "name", "placeholder", "accessible_name"):
            val = getattr(field, attr, None)
            if val and str(val).strip() and not str(val).lower().startswith("el_"):
                return str(val).strip()
        ftype = (getattr(field, "field_type", None) or "field").strip()
        return f"{ftype} field"

    def _deterministic_specs(self, page_state: PageState) -> list[dict]:
        """One scenario per page+form+field+category+data_class combination."""
        from urllib.parse import urlparse

        path = urlparse(page_state.url).path or "/"
        page_label = (page_state.title or "").strip() or path
        if path and path != "/" and page_label != path:
            smoke_label = f"{page_label} ({path})"
        else:
            smoke_label = page_label
        out: list[dict] = [
            {
                "title": f"Smoke: page title present on {smoke_label}",
                "category": "smoke",
                "data_class": "smoke",
                "field_key": "page-title",
                "steps": ["Open page", "Confirm title is non-empty"],
                "expected": ["Title is displayed"],
            }
        ]
        for form in page_state.forms:
            fp = form_fingerprint(form)
            required = [f for f in form.fields if f.required]
            if required:
                req_names = ", ".join(self._field_display_name(f) for f in required[:4])
                out.append(
                    {
                        "title": f"Required field validation ({req_names})",
                        "category": "negative",
                        "form_fp": fp,
                        "field_key": "required-group",
                        "data_class": "empty",
                        "steps": [
                            "Locate form",
                            "Leave required fields empty",
                            "Attempt submit",
                        ],
                        "expected": ["Validation prevents submission or shows errors"],
                    }
                )
            for field in form.fields:
                display = self._field_display_name(field)
                fkey = (field.name or field.label or field.element_id or display).lower()
                # Exactly one representative value class per field (prefer empty for required)
                values = values_for_field(field.field_type, field.label)
                chosen = None
                if field.required:
                    chosen = next((v for v in values if v.category == "empty"), None)
                if chosen is None and values:
                    # Prefer empty, else first deterministic value
                    chosen = next((v for v in values if v.category == "empty"), values[0])
                if chosen is None:
                    continue
                out.append(
                    {
                        "title": f"{chosen.description} on {display}",
                        "category": (
                            "boundary"
                            if "boundary" in chosen.category or "long" in chosen.category
                            else "form"
                        ),
                        "form_fp": fp,
                        "field_key": fkey,
                        "data_class": chosen.category,
                        "steps": [
                            f"Fill field with {chosen.category} value",
                            "Observe validation",
                        ],
                        "expected": ["Application handles input safely"],
                    }
                )
        if page_state.search_fields:
            out.append(
                {
                    "title": "Search with no results",
                    "category": "exploratory",
                    "data_class": "search-empty",
                    "field_key": "search",
                    "steps": ["Enter nonsense query QA_TEST_NO_RESULTS_ZZZ", "Submit search"],
                    "expected": ["Empty state shown without crash"],
                }
            )
        if page_state.pagination_controls:
            out.append(
                {
                    "title": "Pagination edge case",
                    "category": "exploratory",
                    "data_class": "pagination",
                    "field_key": "pagination",
                    "steps": ["Navigate pagination controls", "Observe list stability"],
                    "expected": ["Page changes without console/network 500s"],
                }
            )
        return out
