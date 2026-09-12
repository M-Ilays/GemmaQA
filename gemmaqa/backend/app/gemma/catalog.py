"""Implemented LLM catalog for UI switching.

Config checks only — never probes a live model (Bedrock quota, Gemini billing).
Secrets never appear in catalog payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import (
    CANONICAL_GEMMA_PROVIDERS,
    canonical_gemma_provider,
    get_runtime_provider_override,
    get_settings,
    set_runtime_provider_override,
)
from app.runtime_info import provider_display_name, run_mode_label


class ProviderSwitchError(ValueError):
    """Operator tried to select an unknown or unconfigured provider."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    description: str


# Adding a provider: append a spec here AND a factory branch in gemma/__init__.py.
# The UI lists this tuple — no extra frontend change is required.
PROVIDER_CATALOG: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="mock",
        label="Mock",
        description="Deterministic heuristics. No live model — exploration only.",
    ),
    ProviderSpec(
        id="openai_compatible",
        label="Gemma (Ollama / OpenAI-compatible)",
        description="Local Ollama or any OpenAI-compatible chat API.",
    ),
    ProviderSpec(
        id="gemini",
        label="Google Gemini",
        description="Google AI Studio / Gemini API.",
    ),
    ProviderSpec(
        id="bedrock",
        label="Amazon Bedrock",
        description="AWS Bedrock (Nova, Claude, Llama, and others).",
    ),
    ProviderSpec(
        id="transformers",
        label="Gemma (Transformers)",
        description="Local Hugging Face transformers weights.",
    ),
)


def _configured_for(provider_id: str) -> tuple[bool, str, str | None]:
    """Return (configured, operator note, model id). Never returns secrets."""
    settings = get_settings()
    if provider_id == "mock":
        return True, "No live model. Fine for UI and safety checks.", None

    if provider_id == "openai_compatible":
        model = settings.effective_gemma_model_id or None
        base = settings.effective_gemma_api_base
        if base and model:
            return True, f"Endpoint {base}", model
        return (
            False,
            "Set GEMMA_API_BASE_URL and GEMMA_MODEL_ID in backend .env",
            model,
        )

    if provider_id == "gemini":
        model = settings.effective_gemini_model_id or "gemini-3.5-flash"
        if settings.effective_gemini_api_key:
            return True, "GEMINI_API_KEY is set", model
        return False, "Set GEMINI_API_KEY in backend .env", model

    if provider_id == "bedrock":
        model = settings.effective_bedrock_model_id or None
        region = (settings.aws_region or "").strip()
        if region and model:
            auth_note = (
                "Bedrock API key is set"
                if settings.bedrock_auth_configured
                else "No Bedrock API key; will use the AWS credential chain"
            )
            return True, f"Region {region}. {auth_note}", model
        missing: list[str] = []
        if not region:
            missing.append("AWS_REGION")
        if not model:
            missing.append("BEDROCK_MODEL_ID")
        return False, "Set " + " and ".join(missing) + " in backend .env", model

    if provider_id == "transformers":
        model = settings.effective_gemma_model_id or None
        local = str(settings.gemma_local_model_path or "").strip() or None
        shown = model or local
        if shown:
            return True, "Local weights path or model id is set", shown
        return (
            False,
            "Set GEMMA_MODEL_ID or GEMMA_LOCAL_MODEL_PATH in backend .env",
            None,
        )

    return False, f"Unknown provider {provider_id}", None


def list_provider_catalog(*, active_runs: int = 0) -> dict[str, Any]:
    """Public catalog for the UI. No secrets, no live probes."""
    settings = get_settings()
    active = settings.normalized_gemma_provider
    env_default = settings.env_gemma_provider
    override = get_runtime_provider_override()
    providers: list[dict[str, Any]] = []
    for spec in PROVIDER_CATALOG:
        configured, note, model_id = _configured_for(spec.id)
        providers.append(
            {
                "id": spec.id,
                "label": spec.label,
                "description": spec.description,
                "configured": configured,
                "selectable": configured,
                "note": note,
                "model_id": model_id,
            }
        )
    return {
        "active": active,
        "active_label": provider_display_name(active),
        "run_mode": run_mode_label(active),
        "env_default": env_default,
        "override": override,
        "applies_to": "new_runs",
        "active_runs": active_runs,
        "providers": providers,
    }


def switch_active_provider(provider_id: str, *, active_runs: int = 0) -> dict[str, Any]:
    """Persist a catalog id and drop the cached provider. In-flight runs are unchanged."""
    raw = (provider_id or "").strip()
    if not raw:
        raise ProviderSwitchError("provider is required")
    mapped = canonical_gemma_provider(raw)
    if mapped not in CANONICAL_GEMMA_PROVIDERS:
        known = ", ".join(spec.id for spec in PROVIDER_CATALOG)
        raise ProviderSwitchError(
            f"Unknown provider {raw!r}. Implemented providers: {known}."
        )
    configured, note, _model = _configured_for(mapped)
    if not configured:
        raise ProviderSwitchError(
            f"{mapped} is not configured. {note}"
        )
    from app.gemma import reset_gemma_provider

    set_runtime_provider_override(mapped)
    reset_gemma_provider()
    return list_provider_catalog(active_runs=active_runs)


assert {spec.id for spec in PROVIDER_CATALOG} == CANONICAL_GEMMA_PROVIDERS
