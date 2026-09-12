"""Live documentation synchronizer — ReportBuilder is the source of truth."""

from __future__ import annotations

from typing import Any

from app.agent.memory import RunMemory
from app.gemma.base import GemmaProvider
from app.reporting.report_builder import REPORT_SECTION_ORDER, ReportBuilder
from app.schemas import FinalReport, ProductOverview
from app.utils.logging import get_logger

logger = get_logger("agent.documenter")


class Documenter:
    """Keep QA documents synchronized with agent memory during the run.

    Deterministic structured data from memory is authoritative. Gemma may only
    polish wording of the executive summary from facts already present — it must
    not invent pages, workflows, bugs, roles, tests, evidence, or business rules.
    """

    def __init__(self, gemma: GemmaProvider | None = None) -> None:
        self.gemma = gemma
        self.builder = ReportBuilder()
        # At most one generate_final_report attempt per Documenter (per run),
        # and only after QA execution has stopped — never from mid-run sync().
        self._final_polish_attempted: bool = False

    async def sync(self, memory: RunMemory) -> dict[str, str]:
        """Deterministic live docs. Does not call generate_final_report."""
        self._ensure_baseline_meta(memory)
        report = self.builder.build(memory)

        memory.doc_sections = dict(report.sections_markdown)
        memory.mermaid_diagrams = list(report.mermaid.values())
        memory.regression_checklist = list(report.regression_checklist)
        if report.user_journeys and not memory.user_journeys:
            memory.user_journeys = list(report.user_journeys)
        if report.product_overview:
            memory.product_overview = report.product_overview
        if report.inferred_business_domain and not memory.domain_hypothesis:
            memory.domain_hypothesis = report.inferred_business_domain
            memory.domain_purpose = report.application_purpose or report.inferred_business_domain

        for title in REPORT_SECTION_ORDER:
            # Bodies only — UI already renders the section title (avoid "## Title" duplication)
            memory.doc_sections.setdefault(title, "_None recorded yet._")

        logger.info("Documentation synchronized (%s sections)", len(memory.doc_sections))
        return memory.doc_sections

    def build_report(self, memory: RunMemory) -> FinalReport:
        """Synchronous structured report from current memory."""
        return self.builder.build(memory)

    async def polish_final_executive(
        self,
        memory: RunMemory,
        report: FinalReport,
    ) -> FinalReport:
        """Optional one-shot executive-summary polish after the final ReportBuilder.build().

        Failure or timeout keeps the deterministic report. Never retries.
        A second call is a no-op so the run cannot pay for generate_final_report twice.
        """
        if self.gemma is None:
            return report
        if self._final_polish_attempted:
            logger.info("Skipping generate_final_report; final polish already attempted")
            return report
        provider_name = str(getattr(self.gemma, "name", "") or "").lower()
        # Gemini polish is the ~17s pause after FINISH before the browser
        # closes (run 55588f75). The deterministic report is already built.
        if any(token in provider_name for token in ("ollama", "openai_compatible", "gemini")):
            logger.info("Skipping generate_final_report; post-run polish is not worth the wait")
            return report
        self._final_polish_attempted = True
        return await self._optional_polish_executive(memory, report)

    def _ensure_baseline_meta(self, memory: RunMemory) -> None:
        # Prefer canonical purpose inference when available
        if memory.app_store:
            memory.app_store.refresh_purpose()
            if memory.app_store.model.purpose_confidence >= 0.45:
                memory.domain_purpose = memory.app_store.model.purpose
                memory.domain_hypothesis = (
                    memory.app_store.model.inferred_domain or memory.app_store.model.purpose
                )
        if memory.product_overview is None:
            memory.product_overview = ProductOverview(
                run_id=memory.run_id,
                product_name=self._guess_name(memory),
                summary=f"Exploratory QA of {memory.start_url}",
                primary_purpose=memory.domain_purpose
                or memory.domain_hypothesis
                or "Under analysis",
                key_features=[p.title for p in memory.pages[:5] if p.title],
            )
        elif (
            memory.product_overview.primary_purpose in {"", "Under analysis", None}
            and memory.domain_purpose
            and memory.domain_purpose != "Under analysis"
        ):
            memory.product_overview.primary_purpose = memory.domain_purpose
        if not memory.domain_hypothesis or memory.domain_hypothesis.startswith(
            "Application under test at"
        ):
            if memory.domain_purpose and memory.domain_purpose != "Under analysis":
                memory.domain_hypothesis = memory.domain_purpose
            elif not memory.domain_hypothesis:
                memory.domain_hypothesis = (
                    f"Application under test at {memory.start_url}. "
                    f"Observed {len(memory.visited_urls)} unique page(s) so far."
                )
        if not memory.regression_checklist:
            memory.regression_checklist = [
                "Critical navigation paths remain reachable",
                "Login succeeds with provided credentials (if applicable)",
                "Primary forms render without console errors",
                "No new network 5xx on core pages",
                "Page titles remain present on explored pages",
                "Required fields reject empty submission",
            ]
        if not memory.user_journeys and memory.pages:
            memory.user_journeys = [" → ".join(p.title or p.url for p in memory.pages[:6])]

    async def _optional_polish_executive(
        self,
        memory: RunMemory,
        report: FinalReport,
    ) -> FinalReport:
        """Ask Gemma to rephrase executive summary only, constrained to facts."""
        if self.gemma is None:
            return report
        facts = {
            "product_name": report.product_overview.product_name if report.product_overview else "",
            "target_url": report.target_url,
            "pages_explored": report.coverage.pages_explored if report.coverage else 0,
            "pages_discovered": report.coverage.pages_discovered if report.coverage else 0,
            "actions": report.coverage.action_budget_used if report.coverage else 0,
            "tests_generated": report.coverage.tests_generated if report.coverage else 0,
            "tests_executed": report.coverage.tests_executed if report.coverage else 0,
            "confirmed_bugs": len(report.confirmed_bugs),
            "suspected_bugs": len(report.suspected_bugs),
            "observed_coverage_pct": report.coverage.observed_coverage_pct if report.coverage else 0,
            "stop_reason": memory.stop_reason or "n/a",
            "modules": [m.name for m in report.modules],
            "constraint": (
                "Rewrite the executive summary in clearer prose using ONLY these facts. "
                "Do not invent pages, workflows, bugs, roles, tests, evidence, or business rules. "
                "Do not claim complete coverage."
            ),
            "current_summary": report.executive_summary,
        }
        try:
            polished = await self.gemma.generate_final_report(facts)
            text = ""
            if isinstance(polished, dict):
                text = str(
                    polished.get("executive_summary")
                    or polished.get("summary")
                    or polished.get("text")
                    or ""
                ).strip()
            if text and self._summary_stays_grounded(text):
                report.executive_summary = text
                report.summary = text
                report.sections_markdown["Executive Summary"] = f"## Executive Summary\n\n{text}"
        except Exception as exc:
            logger.debug("Executive summary polish skipped: %s", type(exc).__name__)
        return report

    def _summary_stays_grounded(self, text: str) -> bool:
        lower = text.lower()
        if "complete coverage" in lower or "100% coverage" in lower:
            return False
        return True

    def _guess_name(self, memory: RunMemory) -> str:
        if memory.pages and memory.pages[0].title:
            return memory.pages[0].title.split("|")[0].split("-")[0].strip() or "Application"
        return "Application"

    def _run_data(self, memory: RunMemory) -> dict[str, Any]:
        return {
            "url": memory.start_url,
            "pages_visited": len(memory.visited_urls),
            "bugs_found": len(memory.bugs),
            "suspected_bugs": len(memory.suspected_bugs),
            "actions": len(memory.actions),
            "modules": [m.model_dump() for m in memory.modules],
            "stop_reason": memory.stop_reason,
            "coverage": memory.coverage().model_dump(),
        }
