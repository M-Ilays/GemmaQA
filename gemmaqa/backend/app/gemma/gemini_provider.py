"""Google Gemini provider via the official google-genai SDK.

Implements the ONE abstract method the architecture asks of a provider —
`_generate(system, user, *, images, temperature) -> str` — so every capability
on `GemmaProvider` (action generation, page analysis, goal ranking, workflow
extraction, visual analysis) works through it without a single change outside
this file. The shared context pipeline `prepare_generation_request()` still
produces the prompts; this module only carries them.

AUTHENTICATION — uses GEMINI_API_KEY from environment:
  * The API key is read from Settings.gemini_api_key (sourced from GEMINI_API_KEY env var)
  * Never hardcoded or logged in cleartext
  * Wrapped in SecretStr for safety
  * Missing key produces a clear configuration error without crashing other providers

MULTIMODAL SUPPORT:
  * Gemini 3.5 Flash supports vision
  * Images are converted to base64-encoded data URLs
  * Set GEMMA_SUPPORTS_IMAGES=true to enable

MODEL SELECTION:
  * Uses the verified model ID: gemini-3.5-flash
  * Configurable via GEMINI_MODEL_ID (defaults to gemini-3.5-flash)
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.gemma.base import GemmaProvider
from app.gemma.health import ProviderHealth, set_active_health
from app.gemma.images import prepare_image_data_url
from app.utils.logging import get_logger
from app.utils.sanitization import mask_secret

logger = get_logger("gemma.gemini")

# Default model verified during source verification audit
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"

# Gemini 3.x thinking tokens share max_output_tokens with the visible answer.
# AgentController / New Run never pass max_output_tokens; without this floor the
# env default of 512 truncates structured JSON (finish_reason=MAX_TOKENS).
# ADK still passes STRATEGIC_MAX_OUTPUT_TOKENS explicitly.
GEMINI_MIN_OUTPUT_TOKENS = 4096


class GeminiProvider(GemmaProvider):
    """
    Google Gemini provider using the official google-genai SDK.
    
    Supports Gemini 3.5+ models with text and multimodal capabilities.
    Requires GEMINI_API_KEY environment variable.
    """

    name = "gemini"
    # Gemini has no free liveness endpoint - the only way to verify reachability
    # is to make a billable inference request. health_check() validates config only.
    supports_liveness_probe: bool = False

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.effective_gemini_api_key
        self.model = settings.effective_gemini_model_id or DEFAULT_GEMINI_MODEL
        self.temperature = float(settings.gemma_temperature)
        self.max_tokens = int(settings.effective_gemma_max_tokens)
        self.timeout = float(settings.gemma_timeout_seconds)
        self.supports_images = bool(settings.effective_gemma_supports_images)
        self.max_provider_failures = int(settings.gemma_max_consecutive_failures)
        self.top_p = settings.gemma_top_p
        self.stop_sequences = settings.gemma_stop_sequence_list

        # Client is lazy-initialized on first call to avoid import-time SDK loading
        self._client: Any = None
        self._genai_module: Any = None

        self.health = ProviderHealth(
            provider_type=self.name,
            model_id=self.model,
            multimodal_support=self.supports_images,
            configured=bool(self.api_key),
        )
        if not self.api_key:
            self.health.config_error = "GEMINI_API_KEY is not configured"
            self.health.reachable = None  # Unknown until a call is attempted
        
        set_active_health(self.health)

    def _lazy_init_client(self) -> None:
        """Initialize the google-genai client on first use.
        
        Deferred to avoid import-time overhead when other providers are used.
        Raises RuntimeError if SDK is not installed or API key is missing.
        """
        if self._client is not None:
            return

        if not self.api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not configured. "
                "Set it to your Google AI API key (obtain from https://aistudio.google.com/apikey). "
                "Never commit this key to version control."
            )

        try:
            # Import google-genai SDK - deferred so other providers work without it
            from google import genai
            self._genai_module = genai
        except ImportError as exc:
            raise RuntimeError(
                "google-genai SDK not installed. "
                "Install with: pip install -r backend/requirements-gemini.txt"
            ) from exc

        try:
            # Configure client with API key
            self._client = self._genai_module.Client(api_key=self.api_key)
            logger.info(
                "Gemini client initialized model=%s key=%s multimodal=%s",
                self.model,
                mask_secret(self.api_key),
                self.supports_images,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize Gemini client: {type(exc).__name__}: {exc}"
            ) from exc

    async def _generate(
        self,
        system: str,
        user: str,
        *,
        images: list[str] | None = None,
        temperature: float | None = None,
        json_mode: bool = True,
        max_output_tokens: int | None = None,
    ) -> str:
        """Generate text from Gemini API.
        
        Args:
            system: System prompt (combined with user message for Gemini)
            user: User prompt
            images: Optional list of image paths for multimodal requests
            temperature: Sampling temperature (0.0 = deterministic)
            json_mode: If True (default), force JSON MIME for AgentController
                structured parsers. Pass False only for rare free-text probes.
            max_output_tokens: Optional per-call output budget. When omitted,
                Gemini uses at least GEMINI_MIN_OUTPUT_TOKENS so New Run
                thinking + JSON is not truncated. Thinking models consume this
                budget for internal reasoning as well as the visible answer.
            
        Returns:
            Generated text from the model
            
        Raises:
            RuntimeError: If configuration is invalid or SDK not installed
            Exception: On API errors (network, rate limit, etc.)
        """
        self._lazy_init_client()
        
        temp = self.temperature if temperature is None else temperature
        
        # Build the prompt - Gemini combines system and user into message content
        # For GemmaQA's structured prompts, we prefix the system context
        full_prompt = f"{system}\n\n{user}"
        
        # Build content parts for multimodal support
        content_parts: list[Any] = []
        
        # Add text content
        content_parts.append(full_prompt)
        
        # Add images if supported and provided
        if images and self.supports_images:
            attached = 0
            for image_path in images[:1]:  # Current screenshot only
                try:
                    # Load and encode image for Gemini
                    path_obj = Path(image_path)
                    if path_obj.exists() and path_obj.is_file():
                        # Read image bytes
                        image_bytes = path_obj.read_bytes()
                        
                        # Determine mime type from extension
                        ext = path_obj.suffix.lower()
                        mime_map = {
                            ".png": "image/png",
                            ".jpg": "image/jpeg",
                            ".jpeg": "image/jpeg",
                            ".webp": "image/webp",
                            ".gif": "image/gif",
                        }
                        mime_type = mime_map.get(ext, "image/png")
                        
                        # Create Gemini Part for image
                        # Using the google-genai SDK's types module
                        from google.genai import types
                        
                        image_part = types.Part.from_bytes(
                            data=image_bytes,
                            mime_type=mime_type
                        )
                        content_parts.append(image_part)
                        attached += 1
                        logger.debug("Attached image: %s (%s)", image_path, mime_type)
                    else:
                        logger.warning("Image path does not exist: %s", image_path)
                except Exception as exc:
                    logger.warning(
                        "Failed to attach image %s: %s",
                        image_path,
                        type(exc).__name__,
                    )
            
            if attached:
                logger.info("Multimodal request with %d image(s)", attached)
            else:
                logger.info("Image support enabled but no images attached; using text-only")
        
        # Gemini 3.x models spend internal "thinking" tokens from the same
        # max_output_tokens budget as the visible answer. A budget sized only for
        # the answer can be fully consumed by thinking, yielding a truncated
        # response with finish_reason=MAX_TOKENS. AgentController omits this
        # kwarg, so New Run must still get a thinking-safe floor. Explicit
        # callers (ADK) keep the value they pass.
        if max_output_tokens is None:
            effective_max_tokens = max(int(self.max_tokens), GEMINI_MIN_OUTPUT_TOKENS)
        else:
            effective_max_tokens = int(max_output_tokens)

        # Build generation config
        config_kwargs: dict[str, Any] = {
            "temperature": temp,
            "max_output_tokens": effective_max_tokens,
        }
        
        if self.top_p is not None:
            config_kwargs["top_p"] = float(self.top_p)
        
        if self.stop_sequences:
            config_kwargs["stop_sequences"] = self.stop_sequences
        
        # Enable JSON mode for structured outputs (Gemini 1.5+ feature)
        if json_mode:
            config_kwargs["response_mime_type"] = "application/json"
        
        logger.info(
            "Gemini generate call model=%s temp=%.2f max_tokens=%d",
            self.model,
            temp,
            effective_max_tokens,
        )
        
        try:
            # Make synchronous call to Gemini API
            # The google-genai SDK uses async internally but we wrap it
            import asyncio
            
            # Call generate_content with the content and config
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self.model,
                contents=content_parts,
                config=config_kwargs,
            )
            
            # Extract text from response
            if not response or not response.text:
                raise RuntimeError("Gemini API returned empty response")
            
            result_text = response.text
            logger.info("Gemini generate succeeded, response_length=%d", len(result_text))
            
            # Mark provider as reachable on first success
            if self.health.reachable is None or not self.health.reachable:
                self.health.reachable = True
            
            return result_text
            
        except Exception as exc:
            # Mark as unreachable if this looks like a connectivity/auth issue
            error_str = str(exc).lower()
            if any(
                keyword in error_str
                for keyword in [
                    "api key",
                    "invalid key",
                    "authentication",
                    "unauthorized",
                    "forbidden",
                    "not found",
                    "connection",
                    "timeout",
                    "network",
                ]
            ):
                self.health.reachable = False
            
            logger.error(
                "Gemini generate failed: %s: %s",
                type(exc).__name__,
                str(exc)[:200],
            )
            raise

    async def health_check(self) -> bool:
        """Check provider health.
        
        For Gemini, we can only validate configuration without making a billable
        API call. Actual reachability is determined by the first real request.
        
        Returns:
            True if API key is configured, False otherwise
        """
        configured = bool(self.api_key)
        self.health.configured = configured
        
        if not configured:
            self.health.config_error = "GEMINI_API_KEY is not configured"
            # Don't set reachable=False here - we haven't tried yet
            logger.warning("Gemini health check: API key not configured")
        else:
            self.health.config_error = None
            logger.info("Gemini health check: configuration valid")
        
        return configured
