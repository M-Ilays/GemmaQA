"""Lazy Hugging Face Transformers Gemma provider."""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.gemma.base import GemmaProvider
from app.gemma.health import ProviderHealth, set_active_health
from app.utils.logging import get_logger

logger = get_logger("gemma.transformers")


class TransformersGemmaProvider(GemmaProvider):
    """
    Local transformers inference.

    The model is loaded on the first generate call only (never at import / app startup).
    Requires optional dependency: transformers (+ torch).
    """

    name = "transformers"

    def __init__(self) -> None:
        settings = get_settings()
        self.model_id = settings.effective_gemma_model_id
        self.model_path = settings.gemma_local_model_path
        self.max_tokens = int(settings.effective_gemma_max_tokens)
        self.temperature = float(settings.gemma_temperature)
        self.supports_images = bool(settings.effective_gemma_supports_images)
        self.max_provider_failures = int(settings.gemma_max_consecutive_failures)
        self._pipeline: Any = None
        self._load_lock = asyncio.Lock()

        model_ref = self.model_path or self.model_id
        self.health = ProviderHealth(
            provider_type=self.name,
            model_id=model_ref,
            multimodal_support=False,  # text-generation path is text-only
            configured=bool(model_ref),
        )
        if not model_ref:
            self.health.config_error = (
                "GEMMA_MODEL_ID or GEMMA_LOCAL_MODEL_PATH is required for transformers provider"
            )
        set_active_health(self.health)

    def _model_ref(self) -> str:
        ref = self.model_path or self.model_id
        if not ref:
            raise RuntimeError(
                "Transformers provider misconfigured: set GEMMA_MODEL_ID or "
                "GEMMA_LOCAL_MODEL_PATH to a local path or Hub id (no default model name)."
            )
        return ref

    async def _ensure_pipeline(self) -> Any:
        if self._pipeline is not None:
            return self._pipeline
        async with self._load_lock:
            if self._pipeline is not None:
                return self._pipeline
            model_ref = self._model_ref()
            logger.info("Lazy-loading transformers model: %s", model_ref)

            def _load() -> Any:
                try:
                    from transformers import pipeline  # type: ignore
                except ImportError as exc:
                    raise RuntimeError(
                        "transformers is not installed. Install optional deps "
                        "(transformers, torch) or switch GEMMA_PROVIDER=openai_compatible|mock."
                    ) from exc
                return pipeline(
                    "text-generation",
                    model=model_ref,
                    max_new_tokens=self.max_tokens,
                )

            try:
                self._pipeline = await asyncio.to_thread(_load)
                self.health.reachable = True
            except Exception as exc:
                self.health.reachable = False
                self.health.last_error = f"{type(exc).__name__}: model load failed"
                raise
            return self._pipeline

    async def _generate(
        self,
        system: str,
        user: str,
        *,
        images: list[str] | None = None,
        temperature: float | None = None,
    ) -> str:
        if images and self.supports_images:
            logger.info("Transformers text pipeline ignores images; using text-only context")

        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                pipe = await self._ensure_pipeline()
                prompt = f"System: {system}\n\nUser: {user}\n\nAssistant:"
                temp = self.temperature if temperature is None else temperature
                do_sample = temp > 0.0

                def _run() -> str:
                    kwargs: dict[str, Any] = {
                        "max_new_tokens": self.max_tokens,
                        "do_sample": do_sample,
                        "return_full_text": True,
                    }
                    if do_sample:
                        kwargs["temperature"] = max(float(temp), 1e-5)
                    outputs = pipe(prompt, **kwargs)
                    text = outputs[0]["generated_text"]
                    if "Assistant:" in text:
                        return text.split("Assistant:", 1)[-1].strip()
                    return text[len(prompt) :].strip() or text

                result = await asyncio.to_thread(_run)
                if not result.strip():
                    raise RuntimeError("Transformers model returned empty text")
                return result
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Transformers generate attempt %s failed (%s)",
                    attempt + 1,
                    type(exc).__name__,
                )
                if attempt == 1:
                    break
        assert last_exc is not None
        raise last_exc

    async def health_check(self) -> bool:
        if not self.health.configured:
            self.health.reachable = False
            return False
        # Do not load the model during health checks — report configured/lazy status
        if self._pipeline is not None:
            self.health.reachable = True
            return True
        # Configured but not yet loaded counts as unknown/reachable=None → treat as configured OK
        self.health.reachable = None
        return True
