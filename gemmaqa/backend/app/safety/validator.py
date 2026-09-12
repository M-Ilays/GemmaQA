"""Validate AI-proposed actions against safety policies."""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas import ActionType, BrowserAction, PageState, RiskLevel
from app.safety.action_levels import ActionLevel, classify_action_type
import re

from app.safety.policies import CLEANUP_RISK_CLASS, SafetyPolicy, TEST_DATA_PREFIX
from app.safety.sensitive import find_element, match_sensitive, nearby_form_text, scan_element
from app.safety.url_guard import host_in_scope, validate_target_url
from app.utils.logging import get_logger

logger = get_logger("safety")


@dataclass
class ValidationResult:
    allowed: bool
    reason: str = ""
    sanitized_action: BrowserAction | None = None
    action_level: ActionLevel = ActionLevel.READ_ONLY
    matched_pattern: str = ""
    safety_decision: str = "allow"  # allow | block | warn
    execution_decision: str = "execute"  # execute | skip | rewrite
    policy_rule: str = ""
    # Aliases matching the platform validation contract
    risk: str = ""  # read_only | safe_write | destructive | forbidden

    def __post_init__(self) -> None:
        if not self.risk:
            mapping = {
                ActionLevel.READ_ONLY: "read_only",
                ActionLevel.AUTHENTICATION_WRITE: "authentication_write",
                ActionLevel.SAFE_WRITE: "safe_write",
                ActionLevel.PROHIBITED: "forbidden",
            }
            self.risk = mapping.get(self.action_level, self.action_level.value)
        if not self.policy_rule:
            if not self.allowed:
                self.policy_rule = self.matched_pattern or "POLICY_BLOCK"
            elif self.action_level == ActionLevel.AUTHENTICATION_WRITE:
                self.policy_rule = "AUTHENTICATION_WRITE_ALLOW"
            elif self.action_level == ActionLevel.SAFE_WRITE:
                self.policy_rule = "SAFE_WRITE_TEST_DATA"
            else:
                self.policy_rule = "READ_ONLY_ALLOW"


WRITE_ACTIONS = {
    ActionType.FILL,
    ActionType.SELECT,
    ActionType.CHECK,
    ActionType.UNCHECK,
}

# Values whose SHAPE is the whole point of them. Prefixing these does not make
# a test record identifiable — it makes the write fail, and a write that fails
# creates nothing to identify.
#
# Found by a live run: every fill was prefixed unconditionally, so the agent
# submitted `GemmaQA_TEST_2026-01-15` into a date field, `GemmaQA_TEST_+1-555-0105`
# into a phone capped at 15 characters, and — on retry — the double-prefixed
# `GemmaQA_TEST_106 GemmaQA_TEST_..._Test Street`. Every create was rejected with
# 400, and no amount of correcting the generated data could help, because the
# corruption happened after generation.
_FORMAT_CONSTRAINED_VALUE = re.compile(
    r"""^\s*(
        \d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?)?   # ISO date / datetime
      | \d{1,2}[/-]\d{1,2}[/-]\d{2,4}                  # locale date
      | \d{2}:\d{2}(:\d{2})?                           # time
      | [+(]?[\d][\d\s().-]{5,}                        # phone-shaped
      | -?\d+([.,]\d+)?%?                              # number / decimal / percent
      | [^@\s]+@[^@\s]+\.[^@\s]+                       # email
      | https?://\S+                                   # url
    )\s*$""",
    re.VERBOSE,
)


def _needs_test_data_prefix(value: str, meta: dict) -> bool:
    """Should this fill value be tagged with the test-data marker?

    The marker exists so records GemmaQA creates can be found and cleaned up
    afterwards. That goal is already met when the value carries the marker, and
    is actively defeated when tagging makes the write invalid.
    """
    # Already traceable. Checked anywhere in the value, not just at the start,
    # so a generator-produced value like "106 GemmaQA_TEST_..._Test Street" is
    # not prefixed a second time.
    if TEST_DATA_PREFIX in value:
        return False
    # Produced by GemmaQA's own test-data generator, which already applies the
    # marker wherever the field's type allows one.
    if meta.get("generated_test_data") or meta.get("form_workflow_write"):
        return False
    if _FORMAT_CONSTRAINED_VALUE.match(value):
        logger.info("Skipped test-data prefix on a format-constrained value (would invalidate the write)")
        return False
    return True


class ActionValidator:
    """Gate every Gemma-proposed action through safety rules."""

    def __init__(self, policy: SafetyPolicy) -> None:
        self.policy = policy

    def _is_authorized_record_cleanup(self, action: BrowserAction) -> bool:
        """The one narrow exemption to the HIGH-risk block.

        Removing a record GemmaQA itself created is unavoidably HIGH risk and
        must stay labelled that way — so instead of softening the label (which
        would hide the risk from every other consumer), the action carries an
        explicit marker and is admitted only when all three hold:

          * the run opted in via `allow_destructive_actions`;
          * the action declares `risk_class == CLEANUP_RISK_CLASS`, written
            nowhere but `app.agent.cleanup_planner`; and
          * it names the Temporary Record Registry entry being cleaned up, so
            the exemption is tied to a tracked, run-owned record rather than to
            "any delete button".

        CRITICAL is never exempt.
        """
        if not self.policy.allow_destructive_actions:
            return False
        meta = action.metadata or {}
        return meta.get("risk_class") == CLEANUP_RISK_CLASS and bool(
            meta.get("cleanup_temporary_record_id")
        )

    def validate(
        self,
        action: BrowserAction,
        page_state: PageState | None = None,
        actions_taken: int = 0,
        pages_visited: int = 0,
        screenshots_taken: int = 0,
        runtime_seconds: float = 0.0,
        retries_for_action: int = 0,
    ) -> ValidationResult:
        level = classify_action_type(action.action)

        if (
            action.action == ActionType.TAKE_SCREENSHOT
            and screenshots_taken >= self.policy.max_screenshots
        ):
            return self._block(level, "Screenshot budget exhausted")

        if retries_for_action >= self.policy.max_retries:
            return self._block(level, "Maximum retries exceeded for this action")

        if action.risk == RiskLevel.CRITICAL or (
            action.risk == RiskLevel.HIGH and not self._is_authorized_record_cleanup(action)
        ):
            return self._block(level, f"Risk level '{action.risk}' is blocked", ActionLevel.PROHIBITED)

        # URL / navigation targets
        if action.action == ActionType.OPEN_URL:
            target = action.url or action.value
            if not target:
                return self._block(level, "open_url requires a url")
            url_check = validate_target_url(
                target,
                allow_local_targets=self.policy.allow_local_targets,
            )
            if not url_check.ok:
                return self._block(level, url_check.reason, ActionLevel.PROHIBITED)
            in_scope, scope_reason = host_in_scope(
                target,
                authorized_hostname=self.policy.authorized_domain,
                allow_subdomains=self.policy.allow_subdomains,
                allow_cross_domain=self.policy.allow_cross_domain,
                cdn_suffixes=self.policy.cdn_host_suffixes,
            )
            if not in_scope:
                return self._block(
                    level,
                    f"URL not authorized ({scope_reason}): {target}",
                    ActionLevel.PROHIBITED,
                    matched_pattern=scope_reason,
                )
            if self._url_blocked(target):
                return self._block(
                    level,
                    f"Blocked URL pattern: {target}",
                    ActionLevel.PROHIBITED,
                )

        # Click / fill targets that carry hrefs
        el = find_element(page_state, action.element_id)
        nearby = nearby_form_text(page_state, action.element_id)
        if el and el.href:
            in_scope, scope_reason = host_in_scope(
                el.href,
                authorized_hostname=self.policy.authorized_domain,
                allow_subdomains=self.policy.allow_subdomains,
                allow_cross_domain=self.policy.allow_cross_domain,
                cdn_suffixes=self.policy.cdn_host_suffixes,
            )
            if not in_scope and scope_reason != "relative":
                return self._block(
                    level,
                    f"Cross-domain / out-of-scope link blocked ({scope_reason})",
                    ActionLevel.PROHIBITED,
                    matched_pattern=scope_reason,
                )

        # Sensitive element / intent text
        combined_bits = [action.reason or "", action.value or "", action.expected_result or ""]
        meta = action.metadata or {}
        is_auth_write = bool(meta.get("auth_write") or meta.get("risk_class") == "authentication_write")
        is_safe_test_write = bool(meta.get("risk_class") == "safe_test_data_write")
        # An authorized cleanup necessarily targets a control labelled "Delete"
        # and describes itself as deleting, so it matches BOTH the element-text
        # and the intent-text pattern gates below. Each was refused in turn on a
        # live run even after the operator opted in. The gate that must still
        # decide is `destructive_actions_disabled`, further down.
        is_record_cleanup = self._is_authorized_record_cleanup(action)
        if el:
            # Password fields: only allowed for explicit authentication_write actions
            input_kind = (el.input_type or el.type or "").lower()
            passwordish = input_kind == "password" or (
                (el.role or "").lower() == "textbox"
                and (
                    "password" in (el.name or "").lower()
                    or "password" in (el.label or "").lower()
                    or "password" in (el.accessible_name or "").lower()
                )
            )
            if passwordish and action.action in WRITE_ACTIONS | {ActionType.CLICK}:
                if not (
                    is_auth_write
                    and (
                        (meta.get("auth_method") == "login" and self.policy.allow_login)
                        or (
                            meta.get("auth_method") == "registration"
                            and self.policy.allow_test_account_creation
                        )
                    )
                ):
                    return self._block(
                        ActionLevel.PROHIBITED,
                        "Password fields are blocked outside authenticated login/registration workflows",
                        ActionLevel.PROHIBITED,
                        matched_pattern="password_field",
                    )
            sensitive = scan_element(el, self.policy.blocked_text_patterns, nearby=nearby)
            if sensitive.matched and sensitive.severity == "block":
                # Allow authentication submit buttons (Sign up / Login) despite generic patterns
                if not (is_auth_write and meta.get("auth_submit")) and not is_record_cleanup:
                    return self._block(
                        ActionLevel.PROHIBITED,
                        f"Blocked by sensitive control pattern '{sensitive.pattern}'",
                        ActionLevel.PROHIBITED,
                        matched_pattern=sensitive.pattern,
                    )
            combined_bits.append(sensitive.source_text)

        combined = " ".join(combined_bits)
        intent = match_sensitive(combined, self.policy.blocked_text_patterns)
        if intent.matched and not (is_auth_write and intent.pattern in {"publish"}):
            # Auth reasons may mention "create account" — do not treat as invite/publish
            if not is_auth_write and not is_record_cleanup:
                return self._block(
                    ActionLevel.PROHIBITED,
                    f"Blocked by safety pattern '{intent.pattern}'",
                    ActionLevel.PROHIBITED,
                    matched_pattern=intent.pattern,
                )

        if self.policy.destructive_actions_disabled:
            destructive = match_sensitive(
                combined, ("delete", "destroy", "purge", "wipe", "terminate")
            )
            if destructive.matched and not is_auth_write:
                return self._block(
                    ActionLevel.PROHIBITED,
                    "Destructive actions are disabled",
                    ActionLevel.PROHIBITED,
                    matched_pattern=destructive.pattern,
                )

        # Authentication writes (login / registration)
        if is_auth_write and action.action in WRITE_ACTIONS | {ActionType.CLICK}:
            method = str(meta.get("auth_method") or "")
            if method == "login" and not self.policy.allow_login:
                return self._block(
                    ActionLevel.PROHIBITED,
                    "Login writes disabled by policy (ALLOW_LOGIN=false)",
                    ActionLevel.PROHIBITED,
                    matched_pattern="allow_login",
                )
            if method == "registration" and not self.policy.allow_test_account_creation:
                return self._block(
                    ActionLevel.PROHIBITED,
                    "Test account creation disabled by policy",
                    ActionLevel.PROHIBITED,
                    matched_pattern="allow_test_account_creation",
                )
            return ValidationResult(
                True,
                "ok (authentication_write)",
                sanitized_action=action,
                action_level=ActionLevel.AUTHENTICATION_WRITE,
                safety_decision="allow",
                execution_decision="execute",
            )

        # Write gating for non-auth controlled writes
        if action.action in WRITE_ACTIONS:
            level = ActionLevel.SAFE_WRITE
            if is_safe_test_write and self.policy.allow_safe_test_data_creation:
                pass
            elif self.policy.safe_mode and not self.policy.allow_controlled_writes:
                return self._block(
                    level,
                    "Write actions blocked in read-only safe mode",
                )
            if (
                (self.policy.allow_controlled_writes or self.policy.allow_safe_test_data_creation)
                and action.action == ActionType.FILL
            ):
                value = action.value or ""
                if value and _needs_test_data_prefix(value, meta):
                    # Never rewrite credential-looking values that slipped through
                    if meta.get("credential_ref"):
                        return ValidationResult(
                            True,
                            "ok (credential ref)",
                            sanitized_action=action,
                            action_level=level,
                            safety_decision="allow",
                            execution_decision="execute",
                        )
                    action = action.model_copy(
                        update={"value": f"{TEST_DATA_PREFIX}{value}"}
                    )
                    logger.info("Prefixed fill value with test-data marker")
                    return ValidationResult(
                        True,
                        "ok (rewritten test data)",
                        sanitized_action=action,
                        action_level=level,
                        safety_decision="allow",
                        execution_decision="rewrite",
                    )

        # Never interact with cross-domain embeds via open_tab to foreign hosts
        if action.action == ActionType.OPEN_TAB and (action.url or action.value):
            target = action.url or action.value or ""
            in_scope, scope_reason = host_in_scope(
                target,
                authorized_hostname=self.policy.authorized_domain,
                allow_subdomains=self.policy.allow_subdomains,
                allow_cross_domain=self.policy.allow_cross_domain,
                cdn_suffixes=self.policy.cdn_host_suffixes,
            )
            if not in_scope:
                return self._block(
                    ActionLevel.PROHIBITED,
                    f"Blocked cross-domain tab ({scope_reason})",
                    ActionLevel.PROHIBITED,
                    matched_pattern=scope_reason,
                )

        return ValidationResult(
            True,
            "ok",
            sanitized_action=action,
            action_level=level,
            safety_decision="allow",
            execution_decision="execute",
        )

    def validate_navigation_result(self, current_url: str) -> ValidationResult:
        """After any navigation, ensure we did not leave authorized scope."""
        in_scope, scope_reason = host_in_scope(
            current_url,
            authorized_hostname=self.policy.authorized_domain,
            allow_subdomains=self.policy.allow_subdomains,
            allow_cross_domain=self.policy.allow_cross_domain,
            cdn_suffixes=self.policy.cdn_host_suffixes,
        )
        if not in_scope:
            return self._block(
                ActionLevel.PROHIBITED,
                f"Navigation left authorized scope ({scope_reason}): {current_url}",
                ActionLevel.PROHIBITED,
                matched_pattern=scope_reason,
            )
        return ValidationResult(True, "ok", action_level=ActionLevel.READ_ONLY)

    def _url_blocked(self, url: str) -> bool:
        lower = url.lower()
        return any(p in lower for p in self.policy.blocked_url_patterns)

    @staticmethod
    def _block(
        level: ActionLevel,
        reason: str,
        force_level: ActionLevel | None = None,
        matched_pattern: str = "",
    ) -> ValidationResult:
        blocked_level = force_level or level
        return ValidationResult(
            False,
            reason,
            action_level=blocked_level,
            matched_pattern=matched_pattern,
            safety_decision="block",
            execution_decision="skip",
            policy_rule=matched_pattern or "POLICY_BLOCK",
            risk="forbidden"
            if blocked_level == ActionLevel.PROHIBITED
            else "destructive",
        )
