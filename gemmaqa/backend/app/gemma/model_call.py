"""The provider transport layer's request/response vocabulary.

Two layers, deliberately separate:

  app.gemma.base.GenerationRequest   CONTEXT layer. What one model call should
                                     say: the rendered system/user prompts, the
                                     evidence registry, the projection and
                                     reduction records. Produced by
                                     `prepare_generation_request()`, which every
                                     provider goes through so their contexts are
                                     equivalent rather than merely similar.

  ModelCallRequest / ModelCallResult TRANSPORT layer (this module). How that
                                     call is made and what came back: model id,
                                     decoding knobs, token counts, latency, stop
                                     reason, raw payload.

They are NOT the same object under two names, and neither replaces the other.
`GenerationRequest` keeps its meaning untouched; a call to Bedrock takes a
`GenerationRequest`'s prompts and carries them in a `ModelCallRequest`.

Nothing here imports boto3, httpx or any SDK. This module is pure data, so it
stays importable in an environment with no provider backends installed at all —
the same property that lets `GEMMA_PROVIDER=mock` run without transformers or
AWS present.

Two conventions inherited from the rest of the codebase, both load-bearing:

1. **Absence is absence.** A token count the provider did not report is `None`,
   never `0`. "The model used no input tokens" and "this provider does not tell
   us" are different facts, and a report that shows `0` for both is wrong. The
   same reasoning as `ActionGenerationRequest.testing_objective_provided` and
   the graded activation basis.
2. **Telemetry is opt-in, not opt-out.** `telemetry()` builds the loggable view
   by naming the safe fields, so prompt text, raw payloads and anything in
   `metadata` cannot reach a log by default. A denylist would leak the first
   field someone forgot to add to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

@dataclass
class ModelCallRequest:
    """One provider call, described in provider-neutral terms.

    Deliberately dumb: no validation, no defaulting from settings, no client
    construction. Providers own those decisions — see
    `OpenAICompatibleGemmaProvider._require_config` for the established pattern
    of a provider refusing its own missing configuration with an actionable
    message. A DTO that silently supplied a default model id would be the exact
    "silent fallback" the factory already refuses.
    """

    # The rendered user prompt. Comes from `GenerationRequest.user`; this layer
    # never builds or edits prompt text.
    prompt: str
    system_prompt: str = ""
    # Empty means "the provider has not resolved one yet". Providers validate.
    model: str = ""
    temperature: float = 0.0
    # None means "let the provider/service decide", which is not the same as a
    # caller asking for zero tokens.
    max_tokens: int | None = None
    top_p: float | None = None
    stop_sequences: list[str] = field(default_factory=list)
    # Free-form routing/correlation hints — e.g. {"task": "planner"} for the
    # per-task model routing this vocabulary is meant to make possible later
    # without Planner changes. Treated as potentially sensitive: never logged
    # wholesale, only the key names (see `telemetry`).
    metadata: dict[str, Any] = field(default_factory=dict)

    def telemetry(self) -> dict[str, Any]:
        """Secret-free description of the call, for structured logging.

        Reports prompt SIZES, never prompt text: a prompt can contain page
        content, and page content can contain credentials the run typed in.
        Reports metadata KEYS, never values, for the same reason.
        """
        return {
            "model": self.model or None,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "stop_sequences": len(self.stop_sequences),
            "prompt_chars": len(self.prompt),
            "system_prompt_chars": len(self.system_prompt),
            "metadata_keys": sorted(self.metadata),
        }


@dataclass
class ModelCallResult:
    """What a provider actually returned, normalized across providers.

    Bedrock populates every field it is given by the service. The existing
    providers keep their `_generate() -> str` contract; where a normalized result
    is needed for one of them, `from_text` synthesises a minimal instance whose
    unknown fields are honestly `None` rather than plausible-looking zeros.
    """

    text: str
    model: str = ""
    provider: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop_reason: str = ""
    latency_ms: int | None = None
    # The untouched provider payload, for debugging a response we failed to
    # parse. `repr=False` because it can be large, and it is excluded from
    # `telemetry()` because it echoes the prompt back.
    raw_response: Any = field(default=None, repr=False)

    @property
    def total_tokens(self) -> int | None:
        """Sum of what was reported, or None when nothing was.

        Treats one reported side as usable rather than discarding it: a provider
        that gives output tokens only should not report total usage as unknown.
        """
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    @property
    def usage_reported(self) -> bool:
        """Whether the provider told us anything about token usage at all."""
        return self.input_tokens is not None or self.output_tokens is not None

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        provider: str = "",
        model: str = "",
        latency_ms: int | None = None,
    ) -> "ModelCallResult":
        """Wrap a plain string from a provider that predates this vocabulary.

        The point of this constructor is that it CANNOT invent usage data. Every
        field the string does not carry stays `None`, so a report can distinguish
        "openai_compatible does not surface token counts here" from "the call
        used no tokens".
        """
        return cls(
            text=text,
            provider=provider,
            model=model,
            latency_ms=latency_ms,
        )

    def telemetry(self) -> dict[str, Any]:
        """Secret-free description of the response, for structured logging.

        Excludes `text` and `raw_response`: both contain model output, and model
        output can quote the prompt — which can quote credentials.
        """
        return {
            "provider": self.provider or None,
            "model": self.model or None,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "usage_reported": self.usage_reported,
            "stop_reason": self.stop_reason or None,
            "latency_ms": self.latency_ms,
            "response_chars": len(self.text),
        }
