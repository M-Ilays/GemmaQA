"""Application configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
import os

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

CANONICAL_GEMMA_PROVIDERS = frozenset(
    {"mock", "openai_compatible", "gemini", "bedrock", "transformers"}
)

_RUNTIME_OVERRIDE: str | None = None
_RUNTIME_OVERRIDE_LOADED = False


def canonical_gemma_provider(name: str, *, local_backend: str = "") -> str:
    """Map env aliases to canonical provider ids."""
    raw = (name or "mock").lower().strip()
    if raw in {"mock", "stub", "heuristic", "dev"}:
        return "mock"
    if raw in {"bedrock", "aws", "aws_bedrock", "amazon_bedrock"}:
        return "bedrock"
    if raw in {"gemini", "google", "google_gemini", "google_ai"}:
        return "gemini"
    if raw in {"transformers", "hf", "huggingface"}:
        return "transformers"
    if raw in {"openai_compatible", "openai", "api", "remote", "http", "ollama", "kaggle"}:
        return "openai_compatible"
    if raw in {"local", "offline"}:
        backend = (local_backend or "openai_compatible").lower().strip()
        if backend == "transformers":
            return "transformers"
        return "openai_compatible"
    return raw


def runtime_provider_file() -> Path:
    custom = (os.environ.get("GEMMAQA_RUNTIME_PROVIDER_FILE") or "").strip()
    if custom:
        return Path(custom)
    return Path(__file__).resolve().parents[2] / ".runtime-provider"


def reset_runtime_provider_state() -> None:
    """Forget a loaded override (tests). Does not delete the operator's file."""
    global _RUNTIME_OVERRIDE, _RUNTIME_OVERRIDE_LOADED
    _RUNTIME_OVERRIDE = None
    _RUNTIME_OVERRIDE_LOADED = False


def get_runtime_provider_override() -> str | None:
    """In-memory / file override of GEMMA_PROVIDER. None means use .env."""
    global _RUNTIME_OVERRIDE, _RUNTIME_OVERRIDE_LOADED
    if not _RUNTIME_OVERRIDE_LOADED:
        _RUNTIME_OVERRIDE_LOADED = True
        path = runtime_provider_file()
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    mapped = canonical_gemma_provider(text)
                    _RUNTIME_OVERRIDE = (
                        mapped if mapped in CANONICAL_GEMMA_PROVIDERS else None
                    )
        except OSError:
            _RUNTIME_OVERRIDE = None
    return _RUNTIME_OVERRIDE


def set_runtime_provider_override(provider_id: str | None) -> None:
    """Set the live provider id and persist it (id only, never secrets)."""
    global _RUNTIME_OVERRIDE, _RUNTIME_OVERRIDE_LOADED
    _RUNTIME_OVERRIDE_LOADED = True
    if provider_id:
        mapped = canonical_gemma_provider(provider_id)
        if mapped not in CANONICAL_GEMMA_PROVIDERS:
            raise ValueError(f"Unknown provider: {provider_id}")
        _RUNTIME_OVERRIDE = mapped
    else:
        _RUNTIME_OVERRIDE = None
    path = runtime_provider_file()
    try:
        if _RUNTIME_OVERRIDE:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_RUNTIME_OVERRIDE + "\n", encoding="utf-8")
        elif path.is_file():
            path.unlink()
    except OSError:
        pass


class Settings(BaseSettings):
    """Runtime settings for GemmaQA."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "GemmaQA"
    app_version: str = "0.1.0"
    debug: bool = True
    # Prefer localhost for demos; the API has no authentication layer.
    host: str = "127.0.0.1"
    port: int = 8000

    # Database
    database_url: str = "sqlite+aiosqlite:///./gemmaqa.db"

    # CORS — local Vite (5173) plus local production-container smoke tests (8080).
    # Same-origin Cloud Run (SPA + API on one service) does not need extra CORS.
    # Set CORS_ORIGINS in the environment if the UI is ever hosted on another origin.
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:8080,http://127.0.0.1:8080"
    )

    # Gemma provider: mock | openai_compatible | transformers | bedrock | gemini
    # Legacy aliases still accepted: stub/heuristic→mock, api/local/remote/ollama→openai_compatible
    gemma_provider: str = "mock"
    gemma_model_id: str = ""
    gemma_api_base_url: str = ""
    gemma_api_key: str = ""
    gemma_timeout_seconds: float = 60.0
    gemma_max_output_tokens: int = 512
    gemma_temperature: float = 0.1
    # Multimodal (preferred name)
    gemma_supports_images: bool = False
    # Legacy alias for gemma_supports_images
    gemma_supports_vision: bool = False
    # Used when GEMMA_PROVIDER=transformers (or legacy local+transformers)
    gemma_local_backend: str = "openai_compatible"
    gemma_local_model_path: str = ""
    # Stop model-driven actions after this many consecutive provider failures
    gemma_max_consecutive_failures: int = 3
    # Provider-neutral decoding knobs. None means "do not send this parameter,
    # let the service apply its own default" — which is NOT the same as sending
    # 0.0, so these stay Optional rather than taking a numeric sentinel.
    gemma_top_p: float | None = None
    # Comma-separated, like blocked_text_patterns_extra and playwright_mcp_args.
    # Read through `gemma_stop_sequence_list`.
    gemma_stop_sequences: str = ""

    # Legacy aliases (still accepted from older .env files)
    gemma_api_url: str = ""
    gemma_model_name: str = ""
    gemma_max_tokens: int = 0

    # -- Google Gemini (GEMMA_PROVIDER=gemini) ---------------------------------
    # Deliberately separate from AWS Bedrock pattern but reuses gemma_api_key.
    # The API key is read from GEMINI_API_KEY for clarity, but internally maps
    # to gemma_api_key (which openai_compatible also uses). Model ID defaults to
    # the verified gemini-3.5-flash but can be overridden via GEMINI_MODEL_ID.
    # SecretStr so the value cannot reach a repr, log line, or validation error.
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model_id: str = ""

    # -- AWS Strands Agents (Agents for Humans hackathon) ---------------------
    # Off by default so existing Gemini / mock New Run paths stay unchanged.
    # When true, POST /api/runs is coordinated by a Strands Agent that calls
    # existing run_manager tools (AgentController + Playwright stay intact).
    use_strands_orchestration: bool = Field(default=False, env="USE_STRANDS_ORCHESTRATION")
    strands_model_id: str = Field(default="", env="STRANDS_MODEL_ID")

    # -- Amazon Bedrock (GEMMA_PROVIDER=bedrock) ------------------------------
    # Deliberately separate from the gemma_* names above rather than reusing
    # them: a Bedrock model id and an Ollama model tag look nothing alike, and
    # silently feeding GEMMA_MODEL_ID to Bedrock would produce a confusing
    # ValidationException instead of an actionable "BEDROCK_MODEL_ID is not
    # configured". Read through the `effective_bedrock_*` properties.
    aws_region: str = ""
    # SecretStr so the value cannot reach a repr, a log line, a pydantic
    # validation error, or an accidental `print(settings)`. The provider reads
    # it through `effective_bedrock_bearer_token`; everything else that needs to
    # know whether auth exists asks `bedrock_auth_configured`, which is a bool.
    aws_bearer_token_bedrock: SecretStr = SecretStr("")
    bedrock_model_id: str = ""
    # Maps to botocore's retries={"max_attempts": N}. Distinct from
    # gemma_max_consecutive_failures, which counts failures ACROSS calls before
    # the run stops making model-driven decisions at all.
    bedrock_max_retries: int = 3
    bedrock_connect_timeout: float = 10.0
    # None means "inherit GEMMA_TIMEOUT_SECONDS", so the existing timeout knob
    # keeps governing unless Bedrock is given its own.
    bedrock_read_timeout: float | None = None

    # Browser adapter: direct_playwright | playwright_mcp
    browser_adapter: str = "direct_playwright"
    # Playwright MCP (optional second adapter — never called directly by Gemma)
    playwright_mcp_command: str = "npx"
    playwright_mcp_args: str = "-y,@playwright/mcp@latest"
    playwright_mcp_url: str = ""  # e.g. http://127.0.0.1:8931/mcp for HTTP transport
    playwright_mcp_timeout_seconds: float = 60.0

    # Browser / agent defaults
    playwright_headless: bool = True
    action_timeout_ms: int = 15000
    navigation_timeout_ms: int = 30000
    no_progress_limit: int = 5
    viewport_width: int = 1440
    viewport_height: int = 900
    enable_tracing: bool = True
    observe_max_elements: int = 100
    observe_text_max_chars: int = 800

    # Safety / target policy
    # Default deny for local/private targets. Set true only for demo/dev.
    allow_local_targets: bool = False
    # When not debug, credentials require HTTPS targets
    require_https_credentials: bool = True
    allow_subdomains_default: bool = False
    # Authentication / controlled-write policy (env: ALLOW_LOGIN, etc.)
    allow_login: bool = True
    allow_test_account_creation: bool = True
    allow_safe_test_data_creation: bool = False
    allow_destructive_actions: bool = False
    allow_financial_actions: bool = False
    max_retries: int = 3
    max_screenshots: int = 200
    max_prompt_chars: int = 24000
    max_evidence_bytes: int = 209715200  # 200 MiB
    store_full_html: bool = False
    # Comma-separated extra blocked UI phrases (merged with defaults)
    blocked_text_patterns_extra: str = ""

    # Paths (relative to project root gemmaqa/)
    project_root: Path = Path(__file__).resolve().parents[2]
    evidence_dir: Path = project_root / "evidence"
    screenshots_dir: Path = evidence_dir / "screenshots"
    traces_dir: Path = evidence_dir / "traces"
    reports_dir: Path = evidence_dir / "reports"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def effective_gemma_api_base(self) -> str:
        return (self.gemma_api_base_url or self.gemma_api_url or "").rstrip("/")

    @property
    def effective_database_url(self) -> str:
        """`database_url`, anchored to the project root rather than the cwd.

        The default, `sqlite+aiosqlite:///./gemmaqa.db`, is a RELATIVE path —
        SQLAlchemy resolves it against whatever directory the process happened
        to be started from. Starting the backend from `backend/` one day and
        from the project root the next therefore reads and writes two
        different files with two unrelated run histories: measured live,
        334 runs in one, 631 in the other, zero overlap, both silently
        "correct" from inside that process. Merged back into one file by hand
        once; this stops it from splitting again.

        Only the unmodified default is rewritten, so an operator's explicit
        `DATABASE_URL` — a different sqlite path, or a real server — is used
        exactly as given.
        """
        default_relative = "sqlite+aiosqlite:///./gemmaqa.db"
        if self.database_url != default_relative:
            return self.database_url
        return f"sqlite+aiosqlite:///{(self.project_root / 'gemmaqa.db').as_posix()}"

    @property
    def effective_gemma_model_id(self) -> str:
        return self.gemma_model_id or self.gemma_model_name or ""

    @property
    def effective_gemma_max_tokens(self) -> int:
        if self.gemma_max_output_tokens:
            return self.gemma_max_output_tokens
        if self.gemma_max_tokens:
            return self.gemma_max_tokens
        return 512

    @property
    def effective_gemma_supports_images(self) -> bool:
        return bool(self.gemma_supports_images or self.gemma_supports_vision)

    @property
    def gemma_stop_sequence_list(self) -> list[str]:
        return [s.strip() for s in (self.gemma_stop_sequences or "").split(",") if s.strip()]

    @property
    def effective_bedrock_model_id(self) -> str:
        """BEDROCK_MODEL_ID only — never a fallback to GEMMA_MODEL_ID.

        The absence of a fallback is the feature. A provider that quietly used a
        model id meant for a different backend would fail deep inside the AWS
        SDK with a ValidationException about an unrecognised model, instead of
        saying which environment variable is missing.
        """
        return (self.bedrock_model_id or "").strip()

    @property
    def effective_bedrock_read_timeout(self) -> float:
        """Bedrock's own read timeout, or the existing global one."""
        if self.bedrock_read_timeout is not None:
            return float(self.bedrock_read_timeout)
        return float(self.gemma_timeout_seconds)

    @property
    def bedrock_auth_configured(self) -> bool:
        """Whether a bearer token was supplied — the only auth question anything
        outside the provider is allowed to ask.

        Health responses and log lines call this instead of touching the token,
        so there is no code path where a caller has to remember to mask it.
        """
        return bool(self.aws_bearer_token_bedrock.get_secret_value().strip())

    @property
    def effective_bedrock_bearer_token(self) -> str:
        """The raw token, for the Bedrock provider's client construction only.

        The single place the secret is unwrapped. Callers must not log, echo, or
        put the return value in an error message.
        """
        return self.aws_bearer_token_bedrock.get_secret_value().strip()

    @property
    def effective_gemini_api_key(self) -> str:
        """The Gemini API key for the Gemini provider only.
        
        Reads from GEMINI_API_KEY first (preferred), falling back to GEMMA_API_KEY
        for backward compatibility with openai_compatible. This allows the same
        .env to support both providers without conflicts.
        
        The single place the secret is unwrapped. Callers must not log, echo, or
        put the return value in an error message.
        """
        # Prefer dedicated GEMINI_API_KEY
        gemini_key = self.gemini_api_key.get_secret_value().strip()
        if gemini_key:
            return gemini_key
        # Fall back to shared GEMMA_API_KEY (used by openai_compatible)
        return (self.gemma_api_key or "").strip()

    @property
    def effective_gemini_model_id(self) -> str:
        """GEMINI_MODEL_ID with fallback to GEMMA_MODEL_ID.
        
        Allows gemini provider to use either the dedicated GEMINI_MODEL_ID
        or the shared GEMMA_MODEL_ID (similar to how openai_compatible works).
        """
        return (self.gemini_model_id or self.gemma_model_id or "").strip()

    @property
    def env_gemma_provider(self) -> str:
        """Provider from GEMMA_PROVIDER only — ignores the UI runtime override."""
        return canonical_gemma_provider(
            self.gemma_provider or "mock",
            local_backend=self.gemma_local_backend,
        )

    @property
    def normalized_gemma_provider(self) -> str:
        """Active provider: UI/runtime override if set, otherwise .env."""
        override = get_runtime_provider_override()
        if override:
            return canonical_gemma_provider(
                override, local_backend=self.gemma_local_backend
            )
        return self.env_gemma_provider

    @property
    def normalized_browser_adapter(self) -> str:
        name = (self.browser_adapter or "direct_playwright").lower().strip()
        if name in {"direct_playwright", "direct", "playwright", "local"}:
            return "direct_playwright"
        if name in {"playwright_mcp", "mcp", "playwright-mcp"}:
            return "playwright_mcp"
        return name

    @property
    def playwright_mcp_arg_list(self) -> list[str]:
        raw = (self.playwright_mcp_args or "").strip()
        if not raw:
            return ["-y", "@playwright/mcp@latest"]
        return [p.strip() for p in raw.split(",") if p.strip()]

    def run_evidence_dir(self, run_id: str) -> Path:
        """Per-run evidence root: evidence/<run_id>/{screenshots,traces,reports}."""
        root = self.evidence_dir / run_id
        (root / "screenshots").mkdir(parents=True, exist_ok=True)
        (root / "traces").mkdir(parents=True, exist_ok=True)
        (root / "reports").mkdir(parents=True, exist_ok=True)
        return root

    @property
    def credentials_require_https(self) -> bool:
        """In deployed (non-debug) mode, credentials must use HTTPS targets."""
        if self.debug:
            return False
        return self.require_https_credentials

    @property
    def effective_strands_model_id(self) -> str:
        """STRANDS_MODEL_ID, else BEDROCK_MODEL_ID. Empty means not configured."""
        return (self.strands_model_id or self.bedrock_model_id or "").strip()

    @property
    def extra_blocked_patterns(self) -> tuple[str, ...]:
        return tuple(
            p.strip().lower()
            for p in self.blocked_text_patterns_extra.split(",")
            if p.strip()
        )


@lru_cache
def get_settings() -> Settings:
    """Return cached settings singleton."""
    settings = Settings()
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)
    settings.screenshots_dir.mkdir(parents=True, exist_ok=True)
    settings.traces_dir.mkdir(parents=True, exist_ok=True)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    return settings
