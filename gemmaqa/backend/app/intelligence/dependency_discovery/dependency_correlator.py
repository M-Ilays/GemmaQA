"""Before-and-after correlation — the ONLY mechanism in this package that can
move a dependency from "candidate"/"inferred" toward "verified". Compares
two scope-compatible observations of the same output around a source
action/transition and classifies the result, WITHOUT immediately deciding
"this is a bug" — that judgment belongs to a QA-strategy milestone, not here
(task section 12: "store correlation evidence without immediately reporting
a bug").

Never compares two observations whose scope differs (see
`scope_dimension_analyzer.scopes_compatible`) — that is exactly the false-
positive-verification failure mode the task calls out explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.intelligence.dependency_discovery.schemas import DependencyExecutionObservation, ScopeDimension
from app.intelligence.dependency_discovery.scope_dimension_analyzer import scopes_compatible

_POSITIVE_DIRECTIONS = {"increase", "add", "create", "activate", "enable"}
_NEGATIVE_DIRECTIONS = {"decrease", "remove", "clear", "deactivate", "disable"}
_ANY_CHANGE_DIRECTIONS = {"replace", "recalculate", "redistribute", "move_between_categories"}


@dataclass
class CorrelationInput:
    predicted_direction: str
    action_succeeded: bool
    before_scope: list[ScopeDimension] = field(default_factory=list)
    after_scope: list[ScopeDimension] = field(default_factory=list)
    before_value: float | None = None
    after_value: float | None = None
    observed_at_iteration: int = 0


class DependencyCorrelator:
    def correlate(self, ci: CorrelationInput) -> DependencyExecutionObservation:
        scope_ok = scopes_compatible(ci.before_scope, ci.after_scope)
        if not scope_ok:
            return DependencyExecutionObservation(
                result="scope_incompatible", before_value=ci.before_value, after_value=ci.after_value,
                expected_direction=ci.predicted_direction, scope_compatible=False, observed_at_iteration=ci.observed_at_iteration,
            )
        if ci.before_value is None or ci.after_value is None:
            return DependencyExecutionObservation(
                result="inconclusive", before_value=ci.before_value, after_value=ci.after_value,
                expected_direction=ci.predicted_direction, scope_compatible=True, observed_at_iteration=ci.observed_at_iteration,
            )

        delta = ci.after_value - ci.before_value
        observed_direction = "increase" if delta > 0 else ("decrease" if delta < 0 else "unknown")

        if not ci.action_succeeded:
            # The action itself failed -- any change observed cannot be
            # confidently attributed to it either way.
            return DependencyExecutionObservation(
                result="inconclusive", before_value=ci.before_value, after_value=ci.after_value,
                expected_direction=ci.predicted_direction, observed_direction=observed_direction,
                scope_compatible=True, observed_at_iteration=ci.observed_at_iteration,
            )

        if ci.predicted_direction == "unknown":
            return DependencyExecutionObservation(
                result="inconclusive", before_value=ci.before_value, after_value=ci.after_value,
                expected_direction=ci.predicted_direction, observed_direction=observed_direction,
                scope_compatible=True, observed_at_iteration=ci.observed_at_iteration,
            )

        if ci.predicted_direction in _ANY_CHANGE_DIRECTIONS:
            result = "supported" if delta != 0 else "unsupported"
            return DependencyExecutionObservation(
                result=result, before_value=ci.before_value, after_value=ci.after_value,
                expected_direction=ci.predicted_direction, observed_direction=observed_direction,
                scope_compatible=True, observed_at_iteration=ci.observed_at_iteration,
            )

        wants_positive = ci.predicted_direction in _POSITIVE_DIRECTIONS
        wants_negative = ci.predicted_direction in _NEGATIVE_DIRECTIONS
        if observed_direction == "unknown":
            result = "unsupported"
        elif (wants_positive and observed_direction == "increase") or (wants_negative and observed_direction == "decrease"):
            result = "supported"
        elif wants_positive or wants_negative:
            result = "contradicted"
        else:
            result = "inconclusive"

        return DependencyExecutionObservation(
            result=result, before_value=ci.before_value, after_value=ci.after_value,
            expected_direction=ci.predicted_direction, observed_direction=observed_direction,
            scope_compatible=True, observed_at_iteration=ci.observed_at_iteration,
        )
