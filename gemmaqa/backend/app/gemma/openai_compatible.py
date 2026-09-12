"""OpenAI-compatible Gemma endpoint provider (local or hosted)."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.gemma.base import GemmaProvider
from app.gemma.health import ProviderHealth, set_active_health
from app.gemma.images import prepare_image_data_url
from app.utils.logging import get_logger
from app.utils.sanitization import mask_secret

logger = get_logger("gemma.openai_compatible")

TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}


class OpenAICompatibleGemmaProvider(GemmaProvider):
    """
    Calls a configurable OpenAI-compatible chat completions endpoint.

    Works with Ollama, LM Studio, vLLM, cloud gateways, Kaggle/proxy endpoints, etc.
    Model id is never assumed — must come from GEMMA_MODEL_ID.
    """

    name = "openai_compatible"

    def __init__(self) -> None:
        settings = get_settings()
        self.api_base = settings.effective_gemma_api_base
        self.api_key = settings.gemma_api_key
        self.model = settings.effective_gemma_model_id
        self.temperature = float(settings.gemma_temperature)
        self.max_tokens = int(settings.effective_gemma_max_tokens)
        self.timeout = float(settings.gemma_timeout_seconds)
        self.supports_images = bool(settings.effective_gemma_supports_images)
        self.max_provider_failures = int(settings.gemma_max_consecutive_failures)

        self.health = ProviderHealth(
            provider_type=self.name,
            model_id=self.model,
            multimodal_support=self.supports_images,
            configured=bool(self.api_base and self.model),
        )
        if not self.api_base:
            self.health.config_error = "GEMMA_API_BASE_URL is not configured"
        elif not self.model:
            self.health.config_error = "GEMMA_MODEL_ID is not configured"
        set_active_health(self.health)

    def _require_config(self) -> None:
        if not self.api_base:
            raise RuntimeError(
                "GEMMA_API_BASE_URL is not configured. "
                "Set it to your OpenAI-compatible base (e.g. http://127.0.0.1:11434/v1)."
            )
        if not self.model:
            raise RuntimeError(
                "GEMMA_MODEL_ID is not configured. "
                "Set it to the model id exposed by your endpoint (do not guess a name)."
            )

    async def _generate(
        self,
        system: str,
        user: str,
        *,
        images: list[str] | None = None,
        temperature: float | None = None,
    ) -> str:
        self._require_config()
        temp = self.temperature if temperature is None else temperature
        last_exc: Exception | None = None
        for attempt in range(2):  # initial + one retry for transient errors
            try:
                return await self._call_once(system, user, images=images, temperature=temp)
            except Exception as exc:
                last_exc = exc
                transient = self._is_transient(exc)
                logger.warning(
                    "OpenAI-compatible generate attempt %s failed (%s) transient=%s",
                    attempt + 1,
                    type(exc).__name__,
                    transient,
                )
                if not transient or attempt == 1:
                    break
        assert last_exc is not None
        raise last_exc

    def _is_transient(self, exc: Exception) -> bool:
        # Timeouts are not retried: Gemma 3 on local Ollama routinely exceeds
        # GEMMA_TIMEOUT_SECONDS, and a retry doubles the wait without recovering.
        if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            return False
        if isinstance(exc, httpx.TransportError):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in TRANSIENT_STATUS
        msg = str(exc).lower()
        return any(k in msg for k in ("temporarily", "unavailable", "connection reset"))

    async def _call_once(
        self,
        system: str,
        user: str,
        *,
        images: list[str] | None,
        temperature: float,
    ) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        user_content: Any = user
        if images and self.supports_images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": user}]
            attached = 0
            for path in images[:1]:  # current screenshot only
                data_url = prepare_image_data_url(path)
                if data_url:
                    parts.append({"type": "image_url", "image_url": {"url": data_url}})
                    attached += 1
            if attached:
                user_content = parts
            else:
                logger.info("Image support enabled but screenshot omitted; using text-only")

        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        }
        endpoint = (
            self.api_base
            if self.api_base.endswith("/chat/completions")
            else f"{self.api_base}/chat/completions"
        )
        logger.info(
            "Gemma openai_compatible call model=%s endpoint=%s key=%s",
            self.model,
            endpoint,
            mask_secret(self.api_key) if self.api_key else "(none)",
        )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
                if response.status_code in TRANSIENT_STATUS:
                    response.raise_for_status()
                if response.status_code >= 400:
                    # Avoid logging response bodies (may contain echoed prompts)
                    raise httpx.HTTPStatusError(
                        f"Gemma endpoint returned HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                data = response.json()
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"Gemma endpoint timeout after {self.timeout}s") from exc

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Unexpected Gemma response shape (expected choices[0].message.content)") from exc
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Gemma returned empty content")
        return content

    async def health_check(self) -> bool:
        if not self.health.configured:
            self.health.reachable = False
            self.health.last_checked_at = self.health.last_checked_at
            return False
        try:
            base = self.api_base.replace("/chat/completions", "").rstrip("/")
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            async with httpx.AsyncClient(timeout=min(10.0, self.timeout)) as client:
                resp = await client.get(f"{base}/models", headers=headers)
                ok = resp.status_code < 500
                self.health.reachable = ok
                if not ok:
                    self.health.last_error = f"models endpoint HTTP {resp.status_code}"
                return ok
        except Exception as exc:
            self.health.reachable = False
            self.health.last_error = f"{type(exc).__name__}: health probe failed"
            return False
