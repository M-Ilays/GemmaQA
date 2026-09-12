"""Scope and dimension analysis — every visible output is implicitly scoped
(current actor, tenant, date range, active filter/tab, ...), and two
observations must never be compared/correlated unless their scopes are
compatible. This module extracts scope from a `CanonicalPageModel` +
`DerivedOutputDescriptor` pair, and exposes the compatibility check the
correlator relies on to avoid false verification.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.intelligence.dependency_discovery.schemas import (
    ActorScope,
    DependencyEvidence,
    DerivedOutputDescriptor,
    FilterScope,
    ScopeDimension,
    TemporalScope,
    TenantScope,
)

if TYPE_CHECKING:
    from app.perception.models import CanonicalPageModel

_RELATIVE_TIME_HINTS = ("today", "this week", "this month", "this year", "last 7 days", "last 30 days")
_FILTER_LABEL_HINTS = ("filter", "search", "status", "category", "type")
_TENANT_HINTS = ("organisation", "organization", "workspace", "account", "company", "tenant")


def _evidence(source_kind: str, text: str, *, page_url: str, fingerprint: str) -> DependencyEvidence:
    return DependencyEvidence(source_kind=source_kind, observed_text=(text or "")[:160], page_url=page_url, state_fingerprint=fingerprint)


class ScopeDimensionAnalyzer:
    def analyze(
        self, model: "CanonicalPageModel", output: DerivedOutputDescriptor
    ) -> tuple[list[ScopeDimension], TemporalScope | None, ActorScope | None, TenantScope | None, FilterScope | None]:
        url, fp = model.url, model.state_fingerprint or ""
        dims: list[ScopeDimension] = []

        for group in model.tabs or []:
            if not group.selected_tab_id:
                continue
            selected = next((t for t in group.tabs if t.element_id == group.selected_tab_id), None)
            if selected is not None and (selected.text or selected.accessible_name):
                dims.append(
                    ScopeDimension(
                        dimension_type="selected_tab", value=selected.text or selected.accessible_name or "",
                        evidence=[_evidence("interactive_element", f"selected tab: {selected.text}", page_url=url, fingerprint=fp)],
                    )
                )

        if model.pagination:
            pg = model.pagination[0]
            if pg.current_page is not None:
                dims.append(
                    ScopeDimension(
                        dimension_type="pagination", value=f"page {pg.current_page} of {pg.total_pages or '?'}",
                        evidence=[_evidence("interactive_element", "pagination state", page_url=url, fingerprint=fp)],
                    )
                )

        filter_scope = self._filter_scope(model, url, fp)
        if filter_scope is not None:
            dims.append(
                ScopeDimension(
                    dimension_type="filter", value=f"{filter_scope.filter_label}={filter_scope.filter_value}",
                    evidence=list(filter_scope.evidence),
                )
            )

        temporal_scope = self._temporal_scope(model, url, fp)
        if temporal_scope is not None:
            dims.append(
                ScopeDimension(dimension_type="date_range", value=temporal_scope.label, evidence=list(temporal_scope.evidence))
            )

        actor_scope = None
        if output.current_actor_term:
            actor_scope = ActorScope(actor_term=output.current_actor_term, evidence=[_evidence("actor_registry", f"current actor: {output.current_actor_term}", page_url=url, fingerprint=fp)])
            dims.append(ScopeDimension(dimension_type="actor", value=output.current_actor_term, evidence=list(actor_scope.evidence)))

        tenant_scope = self._tenant_scope(model, output, url, fp)
        if tenant_scope is not None and tenant_scope.tenant_term:
            dims.append(ScopeDimension(dimension_type="tenant", value=tenant_scope.tenant_term, evidence=list(tenant_scope.evidence)))

        return dims, temporal_scope, actor_scope, tenant_scope, filter_scope

    @staticmethod
    def _filter_scope(model, url, fp) -> FilterScope | None:
        for form in model.forms or []:
            for field_ in form.field_descriptors or []:
                label = (field_.label or field_.accessible_name or "").lower()
                if any(h in label for h in _FILTER_LABEL_HINTS) and field_.current_value:
                    return FilterScope(
                        filter_label=field_.label or field_.accessible_name or "", filter_value=field_.current_value,
                        evidence=[_evidence("interactive_element", f"filter {field_.label}={field_.current_value}", page_url=url, fingerprint=fp)],
                    )
        return None

    @staticmethod
    def _temporal_scope(model, url, fp) -> TemporalScope | None:
        haystack = " ".join(
            filter(None, [model.title, *(h.text or "" for h in (model.headings or [])[:3]), *(t.text or "" for t in (model.text_blocks or [])[:5])])
        ).lower()
        for hint in _RELATIVE_TIME_HINTS:
            if hint in haystack:
                return TemporalScope(label=hint, is_relative=True, evidence=[_evidence("heading", f"relative time hint: {hint}", page_url=url, fingerprint=fp)])
        return None

    @staticmethod
    def _tenant_scope(model, output, url, fp) -> TenantScope | None:
        if output.tenant_term:
            return TenantScope(tenant_term=output.tenant_term, evidence=[_evidence("application_memory", f"known tenant: {output.tenant_term}", page_url=url, fingerprint=fp)])
        for b in model.breadcrumbs or []:
            text = (b.text or "").lower()
            if any(h in text for h in _TENANT_HINTS):
                return TenantScope(tenant_term=b.text, evidence=[_evidence("heading", f"breadcrumb tenant hint: {b.text}", page_url=url, fingerprint=fp)])
        return None


def scopes_compatible(scope_a: list[ScopeDimension], scope_b: list[ScopeDimension]) -> bool:
    """Two scope-dimension lists are compatible ONLY if every dimension TYPE
    present in both carries the SAME value. A dimension type present in only
    one list is not itself a conflict (unknown scope is not the same as
    conflicting scope) — but ANY shared type with a differing value makes
    the two observations non-comparable."""
    by_type_a: dict[str, str] = {d.dimension_type: d.value for d in scope_a}
    by_type_b: dict[str, str] = {d.dimension_type: d.value for d in scope_b}
    for dim_type, value_a in by_type_a.items():
        value_b = by_type_b.get(dim_type)
        if value_b is not None and value_b != value_a:
            return False
    return True
