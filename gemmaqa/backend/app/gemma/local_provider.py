"""Legacy local provider alias."""

from __future__ import annotations

from app.config import get_settings
from app.gemma.base import GemmaProvider
from app.gemma.openai_compatible import OpenAICompatibleGemmaProvider
from app.gemma.transformers_provider import TransformersGemmaProvider


class LocalGemmaProvider(GemmaProvider):
    """
    Backward-compatible wrapper.

    Prefer GEMMA_PROVIDER=openai_compatible or transformers directly.
    """

    name = "local"

    def __init__(self) -> None:
        settings = get_settings()
        backend = (settings.gemma_local_backend or "openai_compatible").lower().strip()
        if backend == "transformers":
            self._inner: GemmaProvider = TransformersGemmaProvider()
        else:
            self._inner = OpenAICompatibleGemmaProvider()
        self.name = f"local:{self._inner.name}"
        self.health = self._inner.health
        self.max_provider_failures = getattr(self._inner, "max_provider_failures", 3)

    async def _generate(
        self,
        system: str,
        user: str,
        *,
        images: list[str] | None = None,
        temperature: float | None = None,
    ) -> str:
        return await self._inner._generate(
            system, user, images=images, temperature=temperature
        )

    async def health_check(self) -> bool:
        return await self._inner.health_check()
