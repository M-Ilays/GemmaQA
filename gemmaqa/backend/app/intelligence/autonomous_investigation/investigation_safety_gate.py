"""Scenario-level safety gate -- a defense-in-depth check run BEFORE
attempting any step of a scenario, never a replacement for the per-action
`ActionValidator` gate the controller already runs on every concrete
`BrowserAction` this engine produces. Both gates always run; this one just
refuses to even ATTEMPT a scenario that is fundamentally unsafe, so a
prohibited scenario never gets as far as generating its first action.
"""

from __future__ import annotations

from app.intelligence.autonomous_investigation.schemas import SafetyGateResult


def check_safety(scenario, candidate=None) -> SafetyGateResult:
    risk_class = scenario.risk_assessment.risk_class if scenario.risk_assessment else "unknown"

    if candidate is not None and candidate.risk_class == "prohibited":
        return SafetyGateResult(allowed=False, reason="strategy candidate risk_class is prohibited", risk_class=candidate.risk_class)

    if risk_class == "prohibited":
        return SafetyGateResult(allowed=False, reason="scenario risk_class is prohibited", risk_class=risk_class)

    if scenario.feasibility_status == "blocked":
        return SafetyGateResult(allowed=False, reason="scenario feasibility_status is blocked", risk_class=risk_class)

    for step in scenario.steps:
        if step.safety_class == "prohibited":
            return SafetyGateResult(
                allowed=False, reason=f"step '{step.step_id}' has a prohibited safety_class", risk_class=risk_class,
            )

    return SafetyGateResult(allowed=True, reason="ok", risk_class=risk_class)
