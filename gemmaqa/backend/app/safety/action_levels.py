"""Action safety levels: read_only, safe_write, prohibited."""

from __future__ import annotations

from enum import Enum

from app.schemas import ActionType


class ActionLevel(str, Enum):
    READ_ONLY = "read_only"
    AUTHENTICATION_WRITE = "authentication_write"
    SAFE_WRITE = "safe_write"
    PROHIBITED = "prohibited"


# Actions that never mutate application data by themselves
READ_ONLY_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.OPEN_URL,
        ActionType.GO_BACK,
        ActionType.REFRESH,
        ActionType.WAIT,
        ActionType.HOVER,
        ActionType.INSPECT_FORM,
        ActionType.INSPECT_TABLE,
        ActionType.TAKE_SCREENSHOT,
        ActionType.OPEN_TAB,
        ActionType.FINISH,
        ActionType.PRESS,  # keyboard nav / search submit may be gated by text
    }
)

# Controlled mutations — only when policy allows and data is marked as test
SAFE_WRITE_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.FILL,
        ActionType.SELECT,
        ActionType.CHECK,
        ActionType.UNCHECK,
        ActionType.CLICK,  # click may be read or write depending on control text
    }
)

# Intent phrases that are always prohibited (normalized matching applied elsewhere)
PROHIBITED_INTENT_PATTERNS: tuple[str, ...] = (
    "delete",
    "remove permanently",
    "destroy",
    "wipe",
    "purge",
    "drop database",
    "cancel subscription",
    "payment",
    "pay now",
    "purchase",
    "checkout",
    "refund",
    "password change",
    "change password",
    "reset password",
    "invite user",
    "invite teammate",
    "permission",
    "role change",
    "bulk action",
    "bulk delete",
    "bulk update",
    "download export",
    "export users",
    "send email",
    "send message",
    "publish",
    "deploy",
    "transfer funds",
    "withdraw",
    "deactivate",
    "terminate",
    "production config",
)


def classify_action_type(action: ActionType) -> ActionLevel:
    """Baseline level from action type alone (element text may escalate to prohibited)."""
    if action in READ_ONLY_ACTIONS:
        return ActionLevel.READ_ONLY
    if action in SAFE_WRITE_ACTIONS:
        return ActionLevel.SAFE_WRITE
    return ActionLevel.PROHIBITED
