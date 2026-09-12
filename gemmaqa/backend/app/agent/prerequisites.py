"""Deterministic prerequisite tracking.

Some goals only make sense once other state exists — a checkout form only makes
sense once something is in the cart, an authenticated-area goal only makes sense
once auth succeeded, an order-confirmation goal only makes sense once checkout has
actually been submitted. Rather than let those candidates repeatedly fail (or worse,
silently disappear), a goal whose prerequisites aren't yet met is marked "blocked"
with a truthful reason and re-checked every iteration — once satisfied, it's
reactivated (flipped back to "deferred") so the planner can pick it up again.

Rules are plain, inspectable functions over RunMemory — never an LLM guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from app.agent.memory import RunMemory


@dataclass(frozen=True)
class PrerequisiteRule:
    key: str
    description: str
    check: Callable[["RunMemory"], bool]


def _is_authenticated(memory: "RunMemory") -> bool:
    return bool(memory.authenticated)


def _has_completed_safe_write(memory: "RunMemory") -> bool:
    """At least one safe test-data-creation or form-workflow submission has actually
    succeeded this run (e.g. a product added to cart, a test customer created) — the
    generic, app-agnostic stand-in for "checkout requires >= 1 cart item"."""
    return memory.safe_writes_completed > 0


DEFAULT_PREREQUISITE_RULES: dict[str, PrerequisiteRule] = {
    rule.key: rule
    for rule in (
        PrerequisiteRule(
            key="authenticated",
            description="The run must be authenticated.",
            check=_is_authenticated,
        ),
        PrerequisiteRule(
            key="has_completed_safe_write",
            description=(
                "At least one safe test-data-creation action (e.g. add to cart, "
                "create test customer) must have already succeeded."
            ),
            check=_has_completed_safe_write,
        ),
    )
}


def evaluate_prerequisites(
    keys: list[str], memory: "RunMemory"
) -> tuple[bool, list[str]]:
    """Return (all_satisfied, unsatisfied_keys). Unknown keys are treated as
    satisfied — an unrecognized prerequisite must never block a goal forever."""
    missing = []
    for key in keys:
        rule = DEFAULT_PREREQUISITE_RULES.get(key)
        if rule is None:
            continue
        if not rule.check(memory):
            missing.append(key)
    return (not missing, missing)
