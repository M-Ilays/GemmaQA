"""Bug detection via deterministic signals + Gemma analysis."""

from __future__ import annotations

import re
from typing import Any

from app.gemma.base import GemmaProvider
from app.gemma.parser import bug_analysis_to_defect
from app.schemas import (
    ActionResult,
    BugAnalysisResult,
    BugClassification,
    Defect,
    DefectSeverity,
    PageState,
)
from app.utils.ids import new_id
from app.utils.logging import get_logger

logger = get_logger("agent.bugs")

# Expected client/network noise from empty or invalid auth attempts — not product bugs.
_AUTH_PATH_HINTS = (
    "/users",
    "/login",
    "/auth",
    "/register",
    "/signup",
    "/sign-up",
    "/signin",
    "/sign-in",
    "/session",
    "/token",
    "/adduser",
)


def is_benign_client_noise(message: str) -> bool:
    """True for expected 4xx client/validation noise that should not become defects.

    A rejected form POST (`400 POST .../contacts`) is the application doing
    its job. Asking Gemma to re-analyse it costs ~60s and does not change
    the next test step.
    """
    m = (message or "").lower()
    if not m:
        return False
    if "401" in m or "unauthorized" in m:
        return True
    if "403" in m or "forbidden" in m:
        return True
    if "404" in m or "not found" in m:
        return True
    if "400" in m or "bad request" in m:
        return True
    if "err_aborted" in m and any(h in m for h in _AUTH_PATH_HINTS):
        return True
    if "failed to load resource" in m and any(
        code in m for code in ("400", "401", "403", "404")
    ):
        return True
    return False


class BugAnalyzer:
    """Evaluate page observations and action outcomes for defects."""

    def __init__(self, gemma: GemmaProvider | None = None) -> None:
        self.gemma = gemma
        self.gemma_bug_analysis_calls = 0
        self.skipped_gemma_bug_analyses = 0
        self._gemma_observation_signatures: set[tuple[str, ...]] = set()

    @staticmethod
    def _meaningful_console(page_state: PageState) -> list[str]:
        return [e for e in (page_state.console_errors or []) if not is_benign_client_noise(e)]

    @staticmethod
    def _meaningful_network(page_state: PageState) -> list[str]:
        return [e for e in (page_state.network_failures or []) if not is_benign_client_noise(e)]

    @staticmethod
    def _is_form_surface(page_state: PageState | None) -> bool:
        if page_state is None:
            return False
        if page_state.forms:
            return True
        ptype = page_state.classification.page_type if page_state.classification else ""
        return ptype in {"create_form", "edit_form", "authentication"}

    @staticmethod
    def _observation_signature(
        page_state: PageState,
        *,
        real_console: list[str],
        real_network: list[str],
    ) -> tuple[str, ...]:
        ptype = page_state.classification.page_type if page_state.classification else ""
        return (
            page_state.url or "",
            ptype,
            "|".join(real_console),
            "|".join(real_network),
            "|".join(page_state.dialogs or []),
            "|".join(page_state.alerts or []),
        )

    @staticmethod
    def has_hard_bug_signals(
        *,
        analyses: list[BugAnalysisResult],
        real_console: list[str],
        real_network: list[str],
        dialogs: list[str] | None,
        alerts: list[str] | None,
        page_state: PageState | None = None,
    ) -> bool:
        """True when Gemma can add value beyond what detectors already recorded.

        Console / network / dialogs are hard. A form that is showing validation
        copy is not — the workflow already recovers the field, and asking Gemma
        after every fill costs ~60s while the same alert stays on screen.
        Soft deterministic findings (error_page classification, empty-required)
        are recorded without a model call.
        """
        if real_console or real_network or dialogs:
            return True
        if alerts and not BugAnalyzer._is_form_surface(page_state):
            return True
        return False

    async def analyze_page(
        self,
        page_state: PageState,
        run_id: str,
        *,
        action_result: ActionResult | None = None,
        before_state: PageState | None = None,
    ) -> list[Defect]:
        defects: list[Defect] = []
        analyses: list[BugAnalysisResult] = []

        # Deterministic high-confidence signals
        analyses.extend(self.deterministic_signals(page_state, action_result, before_state))

        # Gemma analysis (never upgrades unclear cases to confirmed without evidence)
        if self.gemma is not None:
            real_console = self._meaningful_console(page_state)
            real_network = self._meaningful_network(page_state)
            signature = self._observation_signature(
                page_state, real_console=real_console, real_network=real_network
            )
            if not self.has_hard_bug_signals(
                analyses=analyses,
                real_console=real_console,
                real_network=real_network,
                dialogs=page_state.dialogs,
                alerts=page_state.alerts,
                page_state=page_state,
            ):
                logger.info("Skipping analyze_potential_bug; no hard bug/error signals")
                self.skipped_gemma_bug_analyses += 1
            elif signature in self._gemma_observation_signatures:
                logger.info("Skipping analyze_potential_bug; same signals already analyzed")
                self.skipped_gemma_bug_analyses += 1
            else:
                observation: dict[str, Any] = {
                    "url": page_state.url,
                    "title": page_state.title,
                    "console_errors": real_console,
                    "network_failures": real_network,
                    "dialogs": page_state.dialogs,
                    "alerts": page_state.alerts,
                    "page_type": page_state.classification.page_type if page_state.classification else None,
                }
                try:
                    self.gemma_bug_analysis_calls += 1
                    self._gemma_observation_signatures.add(signature)
                    ai = await self.gemma.analyze_potential_bug(
                        observation,
                        {"run_id": run_id, "deterministic_count": len(analyses)},
                    )
                    # Downgrade confirmed without hard evidence
                    if ai.classification == BugClassification.CONFIRMED_BUG and not (
                        real_console or real_network or analyses
                    ):
                        ai.classification = BugClassification.SUSPECTED_BUG
                        ai.possible_root_cause = (
                            (ai.possible_root_cause or "")
                            + " (Hypothesis only — insufficient deterministic evidence for confirmation)"
                        ).strip()
                    analyses.append(ai)
                except Exception as exc:
                    logger.warning("Gemma bug analysis failed: %s", type(exc).__name__)

        for analysis in analyses:
            defect = bug_analysis_to_defect(analysis, run_id, page_url=page_state.url)
            if defect:
                # Map classification into tags/status
                if analysis.classification == BugClassification.CONFIRMED_BUG:
                    defect.status = defect.status  # open
                defects.append(defect)
                logger.info(
                    "Finding [%s]: %s",
                    analysis.classification.value,
                    analysis.title or defect.title,
                )

        # Extra heuristic: empty title
        if not (page_state.title or "").strip():
            defects.append(
                Defect(
                    bug_id=new_id(),
                    run_id=run_id,
                    title="Missing page title",
                    description="The page has an empty document title.",
                    severity=DefectSeverity.MINOR,
                    steps_to_reproduce=[f"Navigate to {page_state.url}", "Inspect document.title"],
                    expected="Meaningful page title",
                    actual="Empty title",
                    page_url=page_state.url,
                    tags=["observation", "accessibility"],
                )
            )

        return self._dedupe(defects)

    def deterministic_signals(
        self,
        page_state: PageState,
        action_result: ActionResult | None = None,
        before_state: PageState | None = None,
    ) -> list[BugAnalysisResult]:
        findings: list[BugAnalysisResult] = []

        for err in page_state.network_failures:
            if is_benign_client_noise(err):
                continue
            if " 500 " in f" {err} " or err.startswith("500") or " 5" in err[:5]:
                findings.append(
                    BugAnalysisResult(
                        classification=BugClassification.CONFIRMED_BUG,
                        title="HTTP 500 response observed",
                        module="api",
                        severity="critical",
                        priority="urgent",
                        steps=["Perform action that triggers request", "Inspect network log"],
                        expected_result="Server responds with success or handled error",
                        actual_result=err,
                        business_impact="Core functionality may be broken",
                        possible_root_cause="Hypothesis only: unhandled server exception",
                        confidence=0.95,
                    )
                )
            elif any(code in err for code in (" 502 ", " 503 ", " 504 ")):
                findings.append(
                    BugAnalysisResult(
                        classification=BugClassification.CONFIRMED_BUG,
                        title="HTTP 5xx gateway/server error",
                        module="api",
                        severity="high",
                        priority="high",
                        steps=["Trigger network request", "Inspect status"],
                        expected_result="Healthy response",
                        actual_result=err,
                        confidence=0.9,
                        possible_root_cause="Hypothesis only: upstream dependency failure",
                    )
                )

        for msg in page_state.console_errors:
            if is_benign_client_noise(msg):
                continue
            findings.append(
                BugAnalysisResult(
                    classification=BugClassification.CONFIRMED_BUG
                    if "uncaught" in msg.lower() or "exception" in msg.lower()
                    else BugClassification.SUSPECTED_BUG,
                    title="JavaScript console error",
                    module="frontend",
                    severity="high",
                    priority="high",
                    steps=["Navigate to page", "Open console"],
                    expected_result="No uncaught exceptions",
                    actual_result=msg[:300],
                    possible_root_cause="Hypothesis only: client-side runtime error",
                    confidence=0.85,
                )
            )

        # Broken page load / error page
        if page_state.classification and page_state.classification.page_type == "error_page":
            findings.append(
                BugAnalysisResult(
                    classification=BugClassification.SUSPECTED_BUG,
                    title="Error page rendered during exploration",
                    module="navigation",
                    severity="high",
                    priority="high",
                    steps=["Navigate via explored control"],
                    expected_result="Valid application page",
                    actual_result=page_state.title or page_state.url,
                    confidence=0.7,
                    possible_root_cause="Hypothesis only: broken route or missing resource",
                )
            )

        # Required field accepted empty (heuristic): filled empty then navigated with success toast without validation
        if action_result and action_result.action.action.value == "fill":
            meta = action_result.action.metadata or {}
            if meta.get("value_category") == "empty" and action_result.success:
                # Look for missing validation alerts after empty fill + later submit absence
                if not page_state.alerts and "required" in (action_result.action.reason or "").lower():
                    findings.append(
                        BugAnalysisResult(
                            classification=BugClassification.SUSPECTED_BUG,
                            title="Required field may accept empty value",
                            module="forms",
                            severity="medium",
                            priority="medium",
                            steps=[
                                "Clear required field",
                                "Observe validation messaging",
                            ],
                            expected_result="Validation error for empty required field",
                            actual_result="No validation alert observed after empty fill",
                            confidence=0.55,
                            possible_root_cause="Hypothesis only: missing client-side required check",
                        )
                    )

        if action_result and (action_result.action.metadata or {}).get("value_category") == "invalid_email":
            if action_result.success and not page_state.alerts:
                findings.append(
                    BugAnalysisResult(
                        classification=BugClassification.SUSPECTED_BUG,
                        title="Invalid email may be accepted",
                        module="forms",
                        severity="medium",
                        priority="medium",
                        steps=["Enter invalid email", "Blur/submit field"],
                        expected_result="Format validation error",
                        actual_result="No validation feedback observed",
                        confidence=0.5,
                        possible_root_cause="Hypothesis only: missing email format validation",
                    )
                )

        # Submit with no response after retries (executor marks failure / same fingerprint)
        if action_result and not action_result.success:
            if "timeout" in (action_result.error or "").lower() or "no response" in (
                action_result.message or ""
            ).lower():
                findings.append(
                    BugAnalysisResult(
                        classification=BugClassification.SUSPECTED_BUG,
                        title="Submit action returned no timely response",
                        module="forms",
                        severity="high",
                        priority="high",
                        steps=["Submit form", "Wait for response"],
                        expected_result="UI feedback or navigation",
                        actual_result=action_result.error or action_result.message,
                        confidence=0.65,
                        possible_root_cause="Hypothesis only: hung request or missing handler",
                    )
                )

        # Success message but expected data absent (toast/alert success without table growth)
        successish = [
            t
            for t in (page_state.toasts + page_state.alerts)
            if any(k in t.lower() for k in ("success", "saved", "created", "updated"))
        ]
        if successish and before_state and before_state.tables and page_state.tables:
            before_rows = sum(t.row_count for t in before_state.tables)
            after_rows = sum(t.row_count for t in page_state.tables)
            if after_rows < before_rows + 0:  # no growth when create expected
                if "created" in " ".join(successish).lower():
                    findings.append(
                        BugAnalysisResult(
                            classification=BugClassification.SUSPECTED_BUG,
                            title="Success message shown but expected data absent",
                            module="forms",
                            severity="high",
                            priority="high",
                            steps=["Create record", "Observe success toast", "Check list/table"],
                            expected_result="New row appears in table",
                            actual_result=f"Toast={successish[0][:80]}; rows before={before_rows} after={after_rows}",
                            confidence=0.6,
                            possible_root_cause="Hypothesis only: optimistic UI without persistence",
                        )
                    )

        # Summary metric is 0 while the same page shows a populated related list/table
        inconsistency = self._metric_list_inconsistency(page_state)
        if inconsistency:
            findings.append(inconsistency)

        return findings

    def _metric_list_inconsistency(self, page_state: PageState) -> BugAnalysisResult | None:
        """Detect dashboard-style count=0 while related records are visibly listed."""
        text = " ".join(
            [
                page_state.visible_text_summary or "",
                " ".join(page_state.headings or []),
                " ".join(page_state.navigation_items or []),
            ]
        )
        compact = re.sub(r"\s+", " ", text).strip()
        if not compact:
            return None

        zero_metric = re.search(
            r"\b(customers?|users?|records?|items?|orders?)\b[^a-z0-9]{0,40}\b0\b",
            compact,
            re.IGNORECASE,
        )
        if not zero_metric:
            return None

        metric = zero_metric.group(1).lower()
        table_rows = sum(t.row_count for t in page_state.tables)
        list_links = [
            el
            for el in page_state.interactive_elements
            if el.tag in {"a", "button"}
            and el.href
            and any(k in (el.href or "").lower() for k in ("/customer", "/user", "/record", "/order", "/item"))
        ]
        recent_section = bool(
            re.search(r"recent\s+(customers?|users?|records?|orders?)", compact, re.IGNORECASE)
        )

        if table_rows <= 0 and len(list_links) < 2 and not (
            recent_section and len(list_links) >= 1
        ):
            return None

        return BugAnalysisResult(
            classification=BugClassification.CONFIRMED_BUG,
            title=f"Summary {metric} count is 0 while related records are listed",
            module="dashboard",
            severity="high",
            priority="high",
            steps=[
                f"Open page {page_state.url}",
                f"Compare {metric} summary metric with listed records",
            ],
            expected_result=f"Summary {metric} count matches visible related records",
            actual_result=(
                f"Metric shows 0 but related list/table appears populated "
                f"(table_rows={table_rows}, related_links={len(list_links)})"
            ),
            business_impact="Users may distrust dashboard metrics or miss real data volume",
            possible_root_cause="Hypothesis only: summary API field out of sync with list data",
            confidence=0.88,
        )

    def _dedupe(self, defects: list[Defect]) -> list[Defect]:
        seen: set[tuple[str, str | None]] = set()
        out: list[Defect] = []
        for d in defects:
            key = (d.title, d.page_url)
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
        return out
