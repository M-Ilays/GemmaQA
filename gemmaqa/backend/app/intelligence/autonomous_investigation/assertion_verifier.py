"""Evaluates `ScenarioAssertion`/`ScenarioComparison` records against
whatever has genuinely been observed (before/after `PageState`, the last
`ActionResult`) -- never fabricating a "supported" outcome without a real
runtime signal. Where no reliable signal exists (no structured metric-value
extraction subsystem exists in this codebase, and inventing one is out of
scope for this milestone), the honest answer is `inconclusive`, not a
guess -- see docs/AUTONOMOUS_INVESTIGATION_ENGINE.md's Limitations section.
"""

from __future__ import annotations

from app.intelligence.autonomous_investigation.schemas import AssertionResult

_PERMISSION_OPERATORS = frozenset({"permission_allowed", "permission_denied"})
_TEXT_COMPARABLE_OPERATORS = frozenset(
    {"appears", "disappears", "changes_to", "remains_unchanged", "contains", "excludes", "increases", "decreases", "equals", "not_equals"}
)


def _evaluate_operator(
    operator: str, expected_value: str, *, before_state, after_state, last_result,
) -> tuple[str, str, str]:
    """Returns (outcome, observed_value, explanation)."""
    if operator in _PERMISSION_OPERATORS:
        if last_result is None:
            return "inconclusive", "", "no action result available to verify permission outcome"
        denied = bool(last_result.error) or not last_result.success
        if operator == "permission_denied":
            outcome = "supported" if denied else "contradicted"
        else:
            outcome = "supported" if not denied else "contradicted"
        return outcome, "denied" if denied else "allowed", f"action {'failed' if denied else 'succeeded'}"

    if operator in _TEXT_COMPARABLE_OPERATORS:
        if before_state is None or after_state is None:
            return "inconclusive", "", "no before/after page state available for comparison"
        before_text = before_state.visible_text_summary or ""
        after_text = after_state.visible_text_summary or ""
        changed = before_text != after_text
        if operator == "remains_unchanged":
            outcome = "supported" if not changed else "contradicted"
        elif operator in {"appears", "changes_to", "increases", "decreases", "disappears"}:
            outcome = "supported" if changed else "inconclusive"
        elif operator == "contains":
            outcome = "supported" if expected_value and expected_value in after_text else "inconclusive"
        elif operator == "excludes":
            outcome = "supported" if not expected_value or expected_value not in after_text else "contradicted"
        else:  # equals / not_equals -- no structured value extraction exists; stay honest
            outcome = "inconclusive"
        return (
            outcome, after_text[:200],
            "derived from before/after visible-text comparison (heuristic; not a structured value extraction)",
        )

    return "inconclusive", "", "no reliable runtime signal available to verify this assertion"


def evaluate_assertion(assertion, *, investigation_id, before_state=None, after_state=None, last_result=None) -> AssertionResult:
    outcome, observed, explanation = _evaluate_operator(
        assertion.operator, assertion.expected_value, before_state=before_state, after_state=after_state, last_result=last_result,
    )
    return AssertionResult(
        assertion_result_id=f"assertion-result:{investigation_id}:{assertion.assertion_id}",
        investigation_id=investigation_id, source_assertion_id=assertion.assertion_id, subject_id=assertion.subject_id,
        operator=assertion.operator, outcome=outcome, observed_value=observed, expected_value=assertion.expected_value,
        confidence=assertion.confidence, explanation=explanation,
    )


def evaluate_comparison(comparison, *, investigation_id, before_state=None, after_state=None, last_result=None) -> AssertionResult:
    outcome, observed, explanation = _evaluate_operator(
        comparison.comparison_operator, comparison.expected_delta or "", before_state=before_state, after_state=after_state, last_result=last_result,
    )
    return AssertionResult(
        assertion_result_id=f"assertion-result:{investigation_id}:{comparison.comparison_id}",
        investigation_id=investigation_id, source_assertion_id=comparison.comparison_id, subject_id=comparison.subject_id,
        operator=comparison.comparison_operator, outcome=outcome, observed_value=observed,
        expected_value=comparison.expected_delta or "", confidence=0.5, explanation=explanation,
    )


def summarize(investigation_id: str, scenario_id: str, assertion_results: list[AssertionResult]):
    from app.intelligence.autonomous_investigation.schemas import VerificationResult

    supported = sum(1 for a in assertion_results if a.outcome == "supported")
    contradicted = sum(1 for a in assertion_results if a.outcome == "contradicted")
    inconclusive = sum(1 for a in assertion_results if a.outcome == "inconclusive")
    if contradicted:
        overall = "contradicted"
    elif supported and inconclusive:
        overall = "mixed"
    elif supported:
        overall = "supported"
    else:
        overall = "inconclusive"
    return VerificationResult(
        verification_id=f"verification:{investigation_id}", investigation_id=investigation_id, scenario_id=scenario_id,
        assertion_results=assertion_results, supported_count=supported, contradicted_count=contradicted,
        inconclusive_count=inconclusive, overall_outcome=overall,
    )
