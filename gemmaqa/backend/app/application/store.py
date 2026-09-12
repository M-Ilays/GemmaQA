"""Mutating store for the canonical ApplicationModel."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from app.application.models import (
    AppEvidence,
    AppForm,
    AppFormField,
    AppModule,
    AppNavigationEdge,
    ApplicationModel,
    AppPage,
    AppTestScenario,
    ExplorationStatus,
    ExternalReference,
    ScenarioExecutionStatus,
    ScenarioGenerationStatus,
)
from app.application.purpose import infer_application_purpose
from app.agent.explorer import strip_known_extension
from app.agent.form_lifecycle import NOT_TESTED_REASONS, FormLifecycle
from app.application.url_normalize import (
    is_external_doc_or_social,
    normalize_url,
    normalized_path,
    origin_of,
    same_origin,
)
from app.schemas import FormDescriptor, ModuleRecord, PageState
from app.utils.ids import new_id

# Alias groups → one canonical authentication module
_AUTH_ALIASES = {
    "auth",
    "authentication",
    "login",
    "sign up",
    "signup",
    "sign-up",
    "register",
    "registration",
    "add user",
    "adduser",
}


def _stable_hash(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _humanize(segment: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", strip_known_extension(segment or ""))
    if not cleaned:
        return ""
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", cleaned)
    return spaced.replace("-", " ").replace("_", " ").strip().title()


def canonical_module_key(name: str, path: str = "") -> tuple[str, str]:
    """
    Return (canonical_key, display_name).
    Merge overlapping auth labels into authentication.
    """
    raw = (name or "").strip()
    path_l = (path or "").lower()
    name_l = raw.lower()

    if (
        name_l in _AUTH_ALIASES
        or any(a in name_l for a in ("login", "sign up", "signup", "register", "auth"))
        or any(p in path_l for p in ("/login", "/adduser", "/signup", "/register", "/signin"))
    ):
        return "authentication", "Authentication"

    if not raw:
        seg = path.strip("/").split("/")[0] if path.strip("/") else "home"
        raw = _humanize(seg) or "Home"
    key = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "general"
    return key, raw.title() if raw.islower() else raw


def scenario_key(
    *,
    page_id: str,
    form_fingerprint: str,
    field_key: str,
    category: str,
    data_class: str,
) -> str:
    return _stable_hash(page_id, form_fingerprint, field_key, category, data_class)


def form_fingerprint(form: FormDescriptor | dict[str, Any]) -> str:
    if isinstance(form, FormDescriptor):
        parts = [
            form.method or "",
            form.action or "",
            "|".join(sorted(f.name or f.label or f.element_id or "" for f in form.fields)),
        ]
    else:
        fields = form.get("fields") or []
        parts = [
            str(form.get("method") or ""),
            str(form.get("action") or ""),
            "|".join(
                sorted(
                    str(f.get("name") or f.get("label") or f.get("element_id") or "")
                    for f in fields
                    if isinstance(f, dict)
                )
            ),
        ]
    return _stable_hash(*parts)


class ApplicationStore:
    """Owns and mutates the ApplicationModel for one run."""

    def __init__(self, run_id: str, root_url: str) -> None:
        origin = origin_of(root_url)
        self.model = ApplicationModel(
            run_id=run_id,
            root_url=normalize_url(root_url) or root_url,
            allowed_origin=origin,
            application_name=self._guess_name(root_url),
        )

    @staticmethod
    def _guess_name(url: str) -> str:
        # Derive a friendly name from the hostname alone — no per-target special
        # casing. A hardcoded hostname match here (e.g. for one specific demo
        # target) would be exactly the kind of app-specific assumption that has no
        # business in generic core code; the report/UI can show the real observed
        # page title where a nicer name is needed.
        host = (urlparse(url).hostname or "").replace("www.", "")
        if not host:
            return "Unknown Application"
        if host.replace(".", "").isdigit():
            return host
        return host.split(".")[0].replace("-", " ").title()

    def upsert_page(self, page_state: PageState, *, explored: bool = False) -> AppPage | None:
        """
        Shared page-upsert: one canonical Page per (run_id, canonical_url).

        Only browser observations create/update Page records.
        A simple revisit increments visit_count and does not mark explored
        unless explored=True (meaningful inspection/action).
        """
        return self.observe_page(page_state, explored=explored)

    def observe_page(self, page_state: PageState, *, explored: bool = False) -> AppPage | None:
        """Upsert a same-origin page; external URLs are not application pages."""
        self.prune_stub_pages()
        canonical = normalize_url(page_state.url, base_url=self.model.root_url)
        if not canonical:
            return None

        if not same_origin(canonical, self.model.allowed_origin):
            self._record_external(
                url=canonical,
                label=page_state.title or canonical,
                source_page_id=None,
            )
            return None

        now = datetime.utcnow()
        existing = self.model.page_by_url(canonical)
        heading = page_state.headings[0] if page_state.headings else ""
        page_type = (
            page_state.classification.page_type if page_state.classification else "unknown"
        )
        module_guess = (
            page_state.classification.module_guess if page_state.classification else ""
        )
        path = normalized_path(canonical)
        mod = self.ensure_module(module_guess or heading, path=path, source="route")

        # Visiting removes link-only candidate status
        self.model.candidate_urls = [u for u in self.model.candidate_urls if u != canonical]

        if existing:
            # Revisit: increment visit_count; do not auto-mark explored
            existing.visit_count = 1 if existing.visit_count < 1 else existing.visit_count + 1
            existing.last_seen_at = now
            if page_state.title:
                existing.title = page_state.title
            if heading:
                existing.heading = heading
            if page_type and page_type != "unknown":
                existing.page_type = page_type
            elif not existing.page_type:
                existing.page_type = page_type or "unknown"
            if page_state.state_fingerprint:
                existing.fingerprint = page_state.state_fingerprint
            if explored:
                existing.exploration_status = ExplorationStatus.EXPLORED
            if mod and existing.module_id != mod.id:
                self._unlink_page_from_modules(existing.id)
                existing.module_id = mod.id
                if existing.id not in mod.page_ids:
                    mod.page_ids.append(existing.id)
            elif mod and existing.id not in mod.page_ids:
                mod.page_ids.append(existing.id)
            page = existing
        else:
            page = AppPage(
                id=new_id(),
                run_id=self.model.run_id,
                module_id=mod.id if mod else None,
                canonical_url=canonical,
                normalized_path=path,
                title=page_state.title or "",
                heading=heading,
                page_type=page_type,
                fingerprint=page_state.state_fingerprint or "",
                first_seen_at=now,
                last_seen_at=now,
                visit_count=1,
                exploration_status=(
                    ExplorationStatus.EXPLORED if explored else ExplorationStatus.DISCOVERED
                ),
                screenshot_evidence_id=None,
            )
            self.model.pages.append(page)
            if mod and page.id not in mod.page_ids:
                mod.page_ids.append(page.id)

        # Forms on this page — capturing field structure counts as inspection
        for form in page_state.forms or []:
            self.upsert_form(page, form, inspected=True)

        # Candidate links → same-origin unexplored tracked as candidates; external here
        for el in page_state.interactive_elements or []:
            href = el.href or ""
            if not href or href.startswith("#") or href.lower().startswith("javascript:"):
                continue
            abs_url = normalize_url(href, base_url=canonical)
            if not abs_url:
                continue
            if not same_origin(abs_url, self.model.allowed_origin):
                label = el.accessible_name or el.text or el.visible_text or abs_url
                cat = "docs" if is_external_doc_or_social(abs_url) else "external"
                self._record_external(
                    url=abs_url, label=label or "", source_page_id=page.id, category=cat
                )
            else:
                self.register_candidate_url(abs_url)

        self.consolidate_modules()
        self._touch()
        self.refresh_purpose()
        return page

    def mark_explored(self, canonical_url: str) -> None:
        page = self.model.page_by_url(
            normalize_url(canonical_url, base_url=self.model.root_url) or canonical_url
        )
        if page and page.visit_count > 0:
            page.exploration_status = ExplorationStatus.EXPLORED
            page.last_seen_at = datetime.utcnow()
            self._touch()

    def get_page(self, url: str) -> AppPage | None:
        """Look up an existing canonical Page — never creates stub pages."""
        canonical = normalize_url(url, base_url=self.model.root_url)
        if not canonical:
            return None
        page = self.model.page_by_url(canonical)
        if page and page.visit_count > 0:
            return page
        return None

    def ensure_page(self, url: str, *, title: str = "") -> AppPage | None:
        """
        Resolve a canonical page for navigation references.

        Does NOT create independent page objects from navigation alone —
        only returns pages previously created by observe_page / upsert.
        Unvisited same-origin URLs are tracked as candidate_urls instead.
        """
        canonical = normalize_url(url, base_url=self.model.root_url)
        if not canonical or not same_origin(canonical, self.model.allowed_origin):
            return None
        page = self.get_page(canonical)
        if page:
            return page
        # Do not invent Page records from edges/screenshots/links
        self.register_candidate_url(canonical)
        return None

    def prune_stub_pages(self) -> None:
        """Remove any visit_count==0 stubs so inventory/coverage stay aligned."""
        stubs = {p.id for p in self.model.pages if p.visit_count <= 0}
        if not stubs:
            return
        self.model.pages = [p for p in self.model.pages if p.visit_count > 0]
        self.model.navigation_edges = [
            e
            for e in self.model.navigation_edges
            if e.from_page_id not in stubs and e.to_page_id not in stubs
        ]
        for m in self.model.modules:
            m.page_ids = [pid for pid in m.page_ids if pid not in stubs]
        self._touch()

    def ensure_module(
        self, name: str, *, path: str = "", source: str = "route", purpose: str = ""
    ) -> AppModule:
        key, display = canonical_module_key(name, path)
        existing = self.model.module_by_key(key)
        if existing:
            alias = (name or "").strip()
            if alias and alias.lower() not in {a.lower() for a in existing.aliases} and alias.lower() != existing.name.lower():
                existing.aliases.append(alias)
            if purpose and len(purpose) > len(existing.purpose or ""):
                existing.purpose = purpose
            return existing
        mod = AppModule(
            id=new_id(),
            run_id=self.model.run_id,
            canonical_key=key,
            name=display,
            purpose=purpose or f"{display} area of the application",
            source=source,
            confidence=0.75 if source in {"nav", "route"} else 0.55,
            aliases=[name] if name and name.lower() != display.lower() else [],
        )
        self.model.modules.append(mod)
        return mod

    def _unlink_page_from_modules(self, page_id: str) -> None:
        for m in self.model.modules:
            if page_id in m.page_ids:
                m.page_ids = [p for p in m.page_ids if p != page_id]

    def record_navigation(
        self,
        *,
        from_url: str,
        to_url: str,
        action_type: str,
        action_label: str,
        element_id: str | None,
        reason: str = "",
    ) -> AppNavigationEdge | None:
        src_c = normalize_url(from_url, base_url=self.model.root_url)
        dst_c = normalize_url(to_url, base_url=self.model.root_url)
        if not src_c or not dst_c or src_c == dst_c:
            return None
        if not same_origin(src_c, self.model.allowed_origin):
            return None
        if not same_origin(dst_c, self.model.allowed_origin):
            src = self.model.page_by_url(src_c)
            self._record_external(
                url=dst_c,
                label=action_label or dst_c,
                source_page_id=src.id if src else None,
                category="external",
            )
            return None

        src = self.ensure_page(src_c)
        dst = self.ensure_page(dst_c)
        if not src or not dst:
            return None

        label = (action_label or "").strip() or action_type
        now = datetime.utcnow()
        for edge in self.model.navigation_edges:
            if (
                edge.from_page_id == src.id
                and edge.to_page_id == dst.id
                and edge.action_type == action_type
                and edge.action_label.lower() == label.lower()
            ):
                edge.occurrence_count += 1
                edge.last_seen_at = now
                if reason:
                    edge.reason = reason
                if element_id and not edge.element_id:
                    edge.element_id = element_id
                self._touch()
                return edge

        edge = AppNavigationEdge(
            id=new_id(),
            run_id=self.model.run_id,
            from_page_id=src.id,
            to_page_id=dst.id,
            action_type=action_type,
            action_label=label,
            element_id=element_id,
            reason=reason,
            occurrence_count=1,
            first_seen_at=now,
            last_seen_at=now,
        )
        self.model.navigation_edges.append(edge)
        self._touch()
        return edge

    def mark_form_inspected(self, form_ref: str) -> None:
        """Mark form inspected by AppForm id, form_name, or fingerprint fragment."""
        ref = (form_ref or "").strip().lower()
        if not ref:
            return
        for form in self.model.forms:
            if form.id == form_ref or form.form_name.lower() == ref or form.fingerprint.startswith(ref):
                form.inspected = True
                if form.lifecycle_state in {"discovered", ""}:
                    form.lifecycle_state = "positive_submission_available"
                self._touch()
                return
            if form.form_name == form_ref:
                form.inspected = True
                if form.lifecycle_state in {"discovered", ""}:
                    form.lifecycle_state = "positive_submission_available"
                self._touch()
                return

    def mark_form_tested(self, *, page_url: str | None = None, element_id: str | None = None) -> None:
        page = self.model.page_by_url(normalize_url(page_url or "", base_url=self.model.root_url) or "") if page_url else None
        for form in self.model.forms:
            if page and form.page_id != page.id:
                continue
            if element_id:
                if any(element_id in (f.stable_field_key, f.id, f.label) for f in form.fields):
                    form.tested = True
                    form.inspected = True
                    self._touch()
                    return
            elif page:
                form.tested = True
                form.inspected = True
                self._touch()
                return

    def _find_form(self, form_ref: str) -> AppForm | None:
        ref = (form_ref or "").strip().lower()
        if not ref:
            return None
        for form in self.model.forms:
            if form.id == form_ref or form.form_name == form_ref:
                return form
            if form.form_name.lower() == ref or form.fingerprint.startswith(ref):
                return form
        return None

    def mark_form_classified(
        self,
        form_ref: str,
        *,
        intent: str,
        confidence: float = 0.0,
        target_entity: str | None = None,
        operation: str | None = None,
        evidence: list[str] | None = None,
        alternatives: list[str] | None = None,
    ) -> None:
        """Records `app.perception.form_intent_classifier`'s classification
        onto the matching `AppForm` — the CLASSIFIED lifecycle step, always
        reached before INSPECTED. Best-effort: a form the classifier saw but
        the ApplicationStore hasn't upserted yet (a same-iteration race) is
        silently skipped, matching `mark_form_inspected`'s discipline."""
        form = self._find_form(form_ref)
        if form is None:
            return
        form.intent = intent
        form.intent_confidence = confidence
        form.target_entity_hypothesis = target_entity
        form.operation_hypothesis = operation
        form.intent_evidence = list(evidence or [])
        form.intent_alternatives = list(alternatives or [])
        if form.lifecycle_state in {"", FormLifecycle.DISCOVERED.value}:
            form.lifecycle_state = FormLifecycle.CLASSIFIED.value
        self._touch()

    def mark_form_candidate_for_testing(self, form_ref: str) -> None:
        """A form judged worth attempting — advances past CLASSIFIED/
        INSPECTED without yet having been submitted."""
        form = self._find_form(form_ref)
        if form is None:
            return
        if form.lifecycle_state in {"", FormLifecycle.DISCOVERED.value, FormLifecycle.CLASSIFIED.value, "positive_submission_available"}:
            form.lifecycle_state = FormLifecycle.CANDIDATE_FOR_TESTING.value
        form.not_tested_reason = None
        self._touch()

    def mark_form_not_tested(self, form_ref: str, *, reason: str) -> None:
        """Records WHY a form never advanced to CANDIDATE_FOR_TESTING/TESTED
        — `reason` must be one of `NOT_TESTED_REASONS`, so "0 forms tested"
        is always explainable rather than a silent gap."""
        if reason not in NOT_TESTED_REASONS:
            raise ValueError(f"Invalid not_tested_reason: {reason!r} (expected one of {sorted(NOT_TESTED_REASONS)})")
        form = self._find_form(form_ref)
        if form is None:
            return
        form.not_tested_reason = reason
        self._touch()

    def mark_form_verified(self, form_ref: str) -> None:
        form = self._find_form(form_ref)
        if form is None:
            return
        form.lifecycle_state = FormLifecycle.SUCCEEDED.value
        form.not_tested_reason = None
        self._touch()

    def mark_form_blocked(self, form_ref: str, *, reason: str = "other") -> None:
        form = self._find_form(form_ref)
        if form is None:
            return
        form.lifecycle_state = FormLifecycle.BLOCKED.value
        if reason in NOT_TESTED_REASONS:
            form.not_tested_reason = reason
        self._touch()

    def mark_form_failed(self, form_ref: str) -> None:
        form = self._find_form(form_ref)
        if form is None:
            return
        form.lifecycle_state = FormLifecycle.FAILED.value
        self._touch()

    def upsert_form(
        self, page: AppPage, form: FormDescriptor, *, inspected: bool = False
    ) -> AppForm:
        fp = form_fingerprint(form)
        for existing in self.model.forms:
            if existing.page_id == page.id and existing.fingerprint == fp:
                if inspected:
                    existing.inspected = True
                return existing
        fields: list[AppFormField] = []
        form_id = new_id()
        for field in form.fields:
            key = (field.name or field.label or field.element_id or "field").strip().lower()
            key = re.sub(r"[^a-z0-9_-]+", "-", key)
            fields.append(
                AppFormField(
                    id=new_id(),
                    form_id=form_id,
                    stable_field_key=key,
                    label=field.label or field.name or key,
                    input_type=field.field_type or "text",
                    required=bool(field.required),
                )
            )
        app_form = AppForm(
            id=form_id,
            run_id=self.model.run_id,
            page_id=page.id,
            fingerprint=fp,
            form_name=getattr(form, "name", None) or form.form_id or "form",
            method=form.method or "",
            action=form.action or "",
            inspected=inspected,
            fields=fields,
        )
        self.model.forms.append(app_form)
        self._touch()
        return app_form

    def add_scenario(
        self,
        *,
        title: str,
        category: str,
        page_id: str | None,
        form_id: str | None = None,
        field_id: str | None = None,
        field_key: str = "",
        form_fp: str = "",
        data_class: str = "generic",
        description: str = "",
        steps: list[str] | None = None,
        expected: list[str] | None = None,
        execution_status: ScenarioExecutionStatus = ScenarioExecutionStatus.NOT_RUN,
    ) -> AppTestScenario | None:
        key = scenario_key(
            page_id=page_id or "",
            form_fingerprint=form_fp,
            field_key=field_key or field_id or "",
            category=category,
            data_class=data_class,
        )
        for sc in self.model.scenarios:
            if sc.scenario_key == key:
                return None  # duplicate
        sc = AppTestScenario(
            id=new_id(),
            run_id=self.model.run_id,
            page_id=page_id,
            form_id=form_id,
            field_id=field_id,
            scenario_key=key,
            title=title,
            category=category,
            description=description,
            steps=list(steps or []),
            expected_results=list(expected or []),
            generation_status=ScenarioGenerationStatus.GENERATED,
            execution_status=execution_status,
        )
        self.model.scenarios.append(sc)
        self._touch()
        return sc

    _INVESTIGATION_STATUS_MAP = {
        "queued": ScenarioExecutionStatus.SCHEDULED,
        "validating": ScenarioExecutionStatus.SCHEDULED,
        "executing": ScenarioExecutionStatus.EXECUTING,
        "verifying": ScenarioExecutionStatus.EXECUTING,
        "passed": ScenarioExecutionStatus.PASSED,
        "completed": ScenarioExecutionStatus.PASSED,
        "failed": ScenarioExecutionStatus.FAILED,
        "contradicted": ScenarioExecutionStatus.CONTRADICTED,
        "inconclusive": ScenarioExecutionStatus.INCONCLUSIVE,
        "blocked": ScenarioExecutionStatus.BLOCKED,
        "skipped": ScenarioExecutionStatus.SKIPPED,
        "cleanup_pending": ScenarioExecutionStatus.PASSED,
        "cleaned_up": ScenarioExecutionStatus.PASSED,
    }

    def record_investigation_scenario_result(
        self,
        *,
        scenario_id: str,
        candidate_id: str = "",
        title: str = "",
        category: str = "investigation",
        steps: list[str] | None = None,
        scenario_status: str,
        notes: str = "",
    ) -> AppTestScenario:
        """The bridge between AutonomousInvestigationEngine's scenario execution
        (app.intelligence.scenario_planning `InvestigationScenario.status`) and
        the report/coverage-facing `AppTestScenario` model — WITHOUT this,
        an autonomously-executed scenario updates its own internal registry
        but the final report keeps showing it as `not_run` forever, since
        `RunMemory.coverage()`/`ReportBuilder` only ever read `AppTestScenario`
        records. Upserts by a stable key so re-observing the SAME scenario
        (e.g. executing -> passed) updates the existing row rather than
        duplicating it."""
        key = f"investigation:{scenario_id}"
        execution_status = self._INVESTIGATION_STATUS_MAP.get(scenario_status, ScenarioExecutionStatus.NOT_RUN)
        for sc in self.model.scenarios:
            if sc.scenario_key == key:
                sc.execution_status = execution_status
                sc.generation_status = ScenarioGenerationStatus.SCHEDULED
                if notes:
                    sc.notes = notes
                self._touch()
                return sc
        sc = AppTestScenario(
            id=new_id(),
            run_id=self.model.run_id,
            scenario_key=key,
            title=title or scenario_id,
            category=category,
            description=f"Autonomous investigation candidate {candidate_id}" if candidate_id else "",
            steps=list(steps or []),
            generation_status=ScenarioGenerationStatus.SCHEDULED,
            execution_status=execution_status,
            notes=notes,
        )
        self.model.scenarios.append(sc)
        self._touch()
        return sc

    def add_evidence(
        self,
        *,
        evidence_id: str,
        kind: str,
        relative_path: str,
        description: str = "",
        absolute_path: str | None = None,
    ) -> AppEvidence:
        public_url = f"/api/runs/{self.model.run_id}/evidence/file/{relative_path.lstrip('/')}"
        item = AppEvidence(
            id=evidence_id,
            run_id=self.model.run_id,
            kind=kind,
            relative_path=relative_path.replace("\\", "/"),
            public_url=public_url,
            description=description,
            absolute_path=absolute_path,
        )
        # Dedup by relative path
        for existing in self.model.evidence:
            if existing.relative_path == item.relative_path:
                return existing
        self.model.evidence.append(item)
        self._touch()
        return item

    def record_external_link(
        self,
        url: str,
        *,
        label: str = "",
        category: str = "external",
        source_page_id: str | None = None,
    ) -> None:
        self._record_external(
            url=url, label=label, source_page_id=source_page_id, category=category
        )

    def _record_external(
        self,
        *,
        url: str,
        label: str,
        source_page_id: str | None,
        category: str = "external",
    ) -> None:
        canonical = normalize_url(url) or url
        for ref in self.model.external_references:
            if ref.url == canonical:
                return
        self.model.external_references.append(
            ExternalReference(
                id=new_id(),
                run_id=self.model.run_id,
                source_page_id=source_page_id,
                url=canonical,
                label=label,
                category=category,
            )
        )

    def refresh_purpose(self) -> None:
        purpose, confidence, evidence = infer_application_purpose(self.model)
        if confidence >= self.model.purpose_confidence:
            self.model.purpose = purpose
            self.model.purpose_confidence = confidence
            self.model.purpose_evidence = evidence
            if purpose and confidence >= 0.55:
                self.model.inferred_domain = self.model.inferred_domain or self._domain_from_purpose(
                    purpose
                )

    @staticmethod
    def _domain_from_purpose(purpose: str) -> str:
        lower = purpose.lower()
        if "contact" in lower:
            return "Contact / CRM"
        if "auth" in lower or "login" in lower:
            return "Authentication"
        return "Web application"

    def consolidate_modules(self) -> None:
        """Deduplicate module page_ids and drop unvisited stubs from counts."""
        valid_visited = {p.id for p in self.model.pages if p.visit_count > 0}
        for m in self.model.modules:
            seen: list[str] = []
            for pid in m.page_ids:
                if pid in valid_visited and pid not in seen:
                    seen.append(pid)
            m.page_ids = seen
        # Drop empty non-auth modules with no pages — except synthetic hierarchy
        # parents (source="url_hierarchy"), which legitimately have no pages of
        # their own, only children.
        self.model.modules = [
            m
            for m in self.model.modules
            if m.page_ids or m.canonical_key == "authentication" or m.source == "url_hierarchy"
        ]
        self.infer_module_hierarchy()
        # A synthetic parent whose children all got pruned this pass (e.g. their
        # pages dropped below visit_count>0) is now genuinely empty — remove it too.
        child_counts: dict[str, int] = {}
        for m in self.model.modules:
            if m.parent_module_id:
                child_counts[m.parent_module_id] = child_counts.get(m.parent_module_id, 0) + 1
        self.model.modules = [
            m
            for m in self.model.modules
            if m.source != "url_hierarchy" or child_counts.get(m.id, 0) > 0
        ]

    def infer_module_hierarchy(self) -> None:
        """Activate AppModule.parent_module_id from URL hierarchy — a generic
        structural signal, never a hardcoded per-target-app name. Modules whose
        canonical_key shares a leading hyphen-token (e.g. "checkout-step-one" and
        "checkout-step-two" both start with "checkout") are siblings under a common
        parent: an existing module whose own key equals that shared token becomes
        the parent directly (e.g. "inventory" parents "inventory-item"); otherwise a
        synthetic parent module is created for the shared token (e.g. "checkout"
        parents "checkout-step-one"/"checkout-step-two"/"checkout-complete").
        """
        by_key = {m.canonical_key: m for m in self.model.modules}
        groups: dict[str, list[AppModule]] = {}
        for m in self.model.modules:
            if m.parent_module_id:
                continue  # already placed (e.g. by a prior call) — don't reshuffle
            token = m.canonical_key.split("-", 1)[0]
            if not token or token == m.canonical_key:
                continue  # no hierarchy signal — this key has no hyphenated sibling
            groups.setdefault(token, []).append(m)

        for token, members in groups.items():
            parent = by_key.get(token)
            if parent is not None:
                for m in members:
                    if m.id != parent.id:
                        m.parent_module_id = parent.id
                continue
            if len(members) < 2:
                continue  # a lone module sharing no key with a sibling isn't a group
            synthetic = AppModule(
                id=new_id(),
                run_id=self.model.run_id,
                # Keyed by the bare token (not e.g. "group-checkout") so a LATER
                # call's by_key.get(token) lookup finds this same synthetic parent
                # instead of creating a second one for a newly-discovered sibling.
                canonical_key=token,
                name=_humanize(token) or token.title(),
                purpose=f"Grouping of related {_humanize(token) or token} pages.",
                source="url_hierarchy",
                confidence=0.5,
            )
            self.model.modules.append(synthetic)
            by_key[synthetic.canonical_key] = synthetic
            for m in members:
                m.parent_module_id = synthetic.id

    def sync_legacy_modules(self) -> list[ModuleRecord]:
        """Project canonical modules into ModuleRecord for existing APIs."""
        self.consolidate_modules()
        out: list[ModuleRecord] = []
        for m in self.model.modules:
            urls = []
            page_ids: list[str] = []
            for pid in m.page_ids:
                page = self.model.page_by_id(pid)
                if page and page.visit_count > 0:
                    urls.append(page.canonical_url)
                    page_ids.append(pid)
            if not page_ids:
                continue
            out.append(
                ModuleRecord(
                    module_id=m.id,
                    name=m.name,
                    description=m.purpose,
                    entry_urls=urls,
                    page_ids=page_ids,
                )
            )
        return out

    def register_candidate_url(self, url: str) -> str | None:
        """Track a same-origin link target without creating a Page inventory row."""
        canonical = normalize_url(url, base_url=self.model.root_url)
        if not canonical or not same_origin(canonical, self.model.allowed_origin):
            return None
        if any(p.canonical_url == canonical and p.visit_count > 0 for p in self.model.pages):
            return None
        if canonical not in self.model.candidate_urls:
            self.model.candidate_urls.append(canonical)
            self._touch()
        return canonical

    def register_discovered_url(self, url: str, *, title: str = "") -> AppPage | None:
        """Backward-compatible alias — candidates only (no inventory inflation)."""
        self.register_candidate_url(url)
        return self.model.page_by_url(normalize_url(url, base_url=self.model.root_url) or "")

    def filter_unexplored(self, candidates: list[str]) -> list[str]:
        """Same-origin URLs not yet visited."""
        visited = {p.canonical_url for p in self.model.pages if p.visit_count > 0}
        pooled = list(candidates) + list(self.model.candidate_urls)
        out: list[str] = []
        for u in pooled:
            c = normalize_url(u, base_url=self.model.root_url)
            if not c or c in visited:
                continue
            if not same_origin(c, self.model.allowed_origin):
                continue
            if c not in out:
                out.append(c)
        return out

    def _touch(self) -> None:
        self.model.updated_at = datetime.utcnow()
