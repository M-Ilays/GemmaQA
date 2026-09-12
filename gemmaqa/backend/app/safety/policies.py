"""Safety policies for exploratory testing."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.safety.action_levels import PROHIBITED_INTENT_PATTERNS
from app.safety.sensitive import DEFAULT_SENSITIVE_PATTERNS
from app.safety.url_guard import DEFAULT_CDN_HOST_SUFFIXES

# The ONLY `metadata["risk_class"]` value that lets a HIGH-risk action through
# `ActionValidator`, and then only when the run set allow_destructive_actions=True
# and the action names the Temporary Record Registry entry it is cleaning up.
#
# Without this, HIGH risk was blocked one line before allow_destructive_actions
# was ever consulted, so the entire cleanup path was unreachable by construction:
# every run discovered its delete control, planned the delete, and had it refused
# with "Risk level 'RiskLevel.HIGH' is blocked" -- leaving GemmaQA's own test
# records behind on the application under test.
#
# Written only by app.agent.cleanup_planner; a generic "destructive" label from
# anywhere else must NOT earn this exemption.
CLEANUP_RISK_CLASS = "temporary_record_cleanup"

# Patterns that indicate destructive or high-risk UI actions (configurable)
BLOCKED_TEXT_PATTERNS: tuple[str, ...] = tuple(
    dict.fromkeys([*DEFAULT_SENSITIVE_PATTERNS, *PROHIBITED_INTENT_PATTERNS])
)

BLOCKED_URL_PATTERNS: tuple[str, ...] = (
    "/billing/pay",
    "/checkout",
    "/payment",
    "/refund",
    "/delete",
    "/destroy",
    "/invite",
    "/password/change",
    "/password/reset",
    "/subscriptions/cancel",
    "/admin/deploy",
    "/settings/permissions",
)

# Real-money-transaction vocabulary — blocked unconditionally UNLESS the operator has
# explicitly set allow_financial_actions=True for this run (e.g. a known public demo
# store with no real payment backend, such as SauceDemo's checkout). Distinct from
# destructive patterns (delete/destroy/purge/...), which stay gated by
# allow_destructive_actions regardless of this flag.
FINANCIAL_TEXT_PATTERNS: tuple[str, ...] = (
    "payment",
    "purchase",
    "checkout",
    "refund",
    "pay now",
    "buy now",
    "transfer",
    "withdraw",
)
FINANCIAL_URL_PATTERNS: tuple[str, ...] = (
    "/billing/pay",
    "/checkout",
    "/payment",
    "/refund",
)

# In controlled write mode, only create clearly identifiable test data
TEST_DATA_PREFIX = "GemmaQA_TEST_"


@dataclass
class SafetyPolicy:
    """Runtime safety constraints for a QA run."""

    authorized_url: str
    authorized_domain: str
    safe_mode: bool = True
    allow_controlled_writes: bool = False
    allow_login: bool = True
    allow_test_account_creation: bool = True
    allow_safe_test_data_creation: bool = False
    allow_destructive_actions: bool = False
    allow_financial_actions: bool = False
    allow_cross_domain: bool = False
    allow_subdomains: bool = False
    allow_local_targets: bool = False
    # Unused as stop limits (kept so existing SafetyPolicy construction stays valid).
    max_actions: int = 50
    max_pages: int = 20
    max_runtime_seconds: int = 900  # not a run stop; validator/controller ignore this
    max_retries: int = 3
    max_screenshots: int = 200
    max_prompt_chars: int = 24000
    max_evidence_bytes: int = 200 * 1024 * 1024  # 200 MiB per run
    no_progress_limit: int = 5
    blocked_text_patterns: tuple[str, ...] = field(default_factory=lambda: BLOCKED_TEXT_PATTERNS)
    blocked_url_patterns: tuple[str, ...] = field(default_factory=lambda: BLOCKED_URL_PATTERNS)
    cdn_host_suffixes: tuple[str, ...] = field(default_factory=lambda: DEFAULT_CDN_HOST_SUFFIXES)
    # Destructive / prohibited actions always disabled unless explicitly enabled
    destructive_actions_disabled: bool = True
    store_full_html: bool = False

    def __post_init__(self) -> None:
        if self.allow_destructive_actions:
            self.destructive_actions_disabled = False
        elif self.destructive_actions_disabled is False and not self.allow_destructive_actions:
            self.destructive_actions_disabled = True
        if self.allow_financial_actions:
            # Real-money vocabulary stays blocked by default; the operator must
            # explicitly opt in per run (e.g. a known public demo store with no real
            # payment backend). Destructive patterns are untouched — this flag never
            # loosens delete/destroy/purge-style blocks, only financial ones.
            financial_text = set(FINANCIAL_TEXT_PATTERNS)
            self.blocked_text_patterns = tuple(
                p for p in self.blocked_text_patterns if p not in financial_text
            )
            financial_url = set(FINANCIAL_URL_PATTERNS)
            self.blocked_url_patterns = tuple(
                p for p in self.blocked_url_patterns if p not in financial_url
            )
