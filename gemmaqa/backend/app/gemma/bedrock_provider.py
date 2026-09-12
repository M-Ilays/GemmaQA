"""Amazon Bedrock provider, via the Bedrock Runtime Converse API.

Implements the ONE abstract method the architecture asks of a provider —
`_generate(system, user, *, images, temperature) -> str` — so every capability
on `GemmaProvider` (action generation, page analysis, goal ranking, workflow
extraction, visual analysis) works through it without a single change outside
this file. The shared context pipeline `prepare_generation_request()` still
produces the prompts; this module only carries them.

AUTHENTICATION — verified against botocore 1.43.63 rather than assumed, because
the wrong answer here fails in a way that looks like a permissions problem:

  * `bedrock-runtime`'s service model declares
    `auth: ['aws.auth#sigv4', 'smithy.api#httpBearerAuth']` and
    `signingName: bedrock`, and `botocore.handlers.
    get_bearer_auth_supported_services()` returns exactly `{'bedrock'}`.
  * `botocore.utils.get_token_from_environment()` reads
    `AWS_BEARER_TOKEN_<SIGNING_NAME>` — i.e. `AWS_BEARER_TOKEN_BEDROCK` — from
    `os.environ`. There is no client kwarg for it.
  * Proven by inspecting the signed request: with that variable set the header
    is `Authorization: Bearer <token>`; without it, SigV4. **No header is ever
    injected by hand here** — doing so would duplicate an SDK responsibility and
    break the moment AWS changes the scheme.
  * `_should_prefer_bearer_auth()` skips bearer entirely when the client Config
    sets `signature_version` or `auth_scheme_preference`. Proven the same way:
    adding `signature_version='v4'` to the Config silently reverted a
    bearer-authenticated request to SigV4. **This module must therefore never
    set either field**, and `test_bedrock_provider.py` asserts it does not.

Because pydantic-settings loads `.env` into `Settings` and NOT into
`os.environ`, an operator who sets `AWS_BEARER_TOKEN_BEDROCK` only in `.env`
would otherwise get SigV4 and an opaque failure. `_export_bearer_token()`
bridges that one gap, and is the only place the secret is handled.

RETRIES are botocore's, configured once (`retries={"max_attempts": ...,
"mode": "standard"}`). This provider deliberately does NOT wrap the call in a
retry loop of its own — unlike `OpenAICompatibleGemmaProvider`, which predates
that decision. Two nested retry budgets multiply into a stall nobody sized.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from typing import Any

from app.config import get_settings
from app.gemma.base import GemmaProvider
from app.gemma.health import ProviderHealth, set_active_health
from app.gemma.images import prepare_image_data_url
from app.gemma.model_call import ModelCallRequest, ModelCallResult
from app.utils.logging import get_logger

logger = get_logger("gemma.bedrock")

# Bedrock's ImageFormat enum is ['png', 'jpeg', 'gif', 'webp'] (read from the
# service model). `prepare_image_data_url` only ever emits the three below, so
# this mapping is total for our inputs — no format is ever guessed. Anything
# outside it means the shared helper changed, and the image is dropped rather
# than sent with an invented format.
DATA_URL_MIME_TO_BEDROCK_FORMAT = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/webp": "webp",
}

# Provider-supplied error text is capped before it reaches a log or a health
# record. AWS messages are useful for diagnosis ("model identifier is invalid")
# but they are attacker- and prompt-influenced strings, so their length is
# bounded and they are never joined with the request body.
MAX_PROVIDER_MESSAGE_CHARS = 200

# Closed vocabulary for normalized failures. Everything that can go wrong on
# this path maps to exactly one of these, so a caller can branch on the kind
# instead of matching substrings of an SDK message.
FAILURE_KINDS = frozenset(
    {
        "not_installed",
        "not_configured",
        "access_denied",
        "invalid_credentials",
        "expired_credentials",
        "missing_credentials",
        "validation_error",
        "invalid_model",
        "throttled",
        "connect_timeout",
        "read_timeout",
        "network_error",
        "service_unavailable",
        "model_error",
        "malformed_response",
        "unknown",
    }
)

# Failure kinds that prove we did NOT reach a usable Bedrock endpoint, and so
# justify recording `reachable = False`.
#
# The complement matters just as much: a `validation_error`, `throttled`,
# `invalid_model` or `malformed_response` means the service ANSWERED us. Those
# calls failed, and `consecutive_failures` / `last_error_summary` record that —
# but flipping `reachable` to False for them would claim a connectivity problem
# that the evidence contradicts. Neither is `reachable` set to True by a failure
# of any kind: only a completed call earns that.
UNREACHABLE_FAILURE_KINDS = frozenset(
    {
        "not_installed",
        "not_configured",
        "connect_timeout",
        "read_timeout",
        "network_error",
        "service_unavailable",
        "missing_credentials",
        "invalid_credentials",
        "expired_credentials",
        "access_denied",
    }
)

# AWS error codes -> our kinds. Codes come from the bedrock-runtime service
# model's declared error shapes plus the credential errors botocore raises as
# ClientError.
_AWS_CODE_TO_KIND = {
    "AccessDeniedException": "access_denied",
    "UnrecognizedClientException": "invalid_credentials",
    "InvalidSignatureException": "invalid_credentials",
    "ExpiredTokenException": "expired_credentials",
    "ExpiredToken": "expired_credentials",
    "ValidationException": "validation_error",
    "ResourceNotFoundException": "invalid_model",
    "ThrottlingException": "throttled",
    "TooManyRequestsException": "throttled",
    "ModelTimeoutException": "read_timeout",
    "ServiceUnavailableException": "service_unavailable",
    "InternalServerException": "service_unavailable",
    "ModelNotReadyException": "service_unavailable",
    "ModelErrorException": "model_error",
}

# What an operator should actually do about each kind. Kept beside the mapping
# so a new kind cannot be added without an answer to "and then what?".
_KIND_GUIDANCE = {
    "not_installed": "install the Bedrock extra: pip install -r backend/requirements-bedrock.txt",
    "not_configured": "set AWS_REGION and BEDROCK_MODEL_ID",
    "access_denied": "the credentials are valid but lack bedrock:InvokeModel on this model, or model access is not enabled in this region",
    "invalid_credentials": "AWS_BEARER_TOKEN_BEDROCK or the resolved AWS credentials were rejected",
    "expired_credentials": "the credentials have expired; refresh them",
    "missing_credentials": "no credentials resolved: set AWS_BEARER_TOKEN_BEDROCK, or configure the standard AWS credential chain",
    "validation_error": "Bedrock rejected the request shape or a parameter value",
    "invalid_model": "BEDROCK_MODEL_ID is not a model available in AWS_REGION",
    "throttled": "request was throttled after the configured retries; lower concurrency or request a quota increase",
    "connect_timeout": "could not reach the Bedrock endpoint within BEDROCK_CONNECT_TIMEOUT",
    "read_timeout": "Bedrock did not respond within BEDROCK_READ_TIMEOUT",
    "network_error": "network failure reaching the Bedrock endpoint",
    "service_unavailable": "Bedrock reported a transient service problem after the configured retries",
    "model_error": "the model itself failed to produce a response",
    "malformed_response": "Bedrock returned a response this provider could not read",
    "unknown": "unclassified failure; see the error type",
}


class BedrockCallError(RuntimeError):
    """A normalized Bedrock failure.

    Carries a `kind` from `FAILURE_KINDS` so callers branch on a value rather
    than on the wording of an SDK exception, and never carries the bearer token,
    the prompt, or the raw SDK object.
    """

    def __init__(self, kind: str, detail: str = "", *, aws_code: str = "") -> None:
        self.kind = kind if kind in FAILURE_KINDS else "unknown"
        self.aws_code = aws_code
        guidance = _KIND_GUIDANCE.get(self.kind, "")
        message = f"Bedrock {self.kind}"
        if aws_code:
            message += f" [{aws_code}]"
        if guidance:
            message += f": {guidance}"
        if detail:
            message += f" ({detail[:MAX_PROVIDER_MESSAGE_CHARS]})"
        super().__init__(message)


class BedrockProvider(GemmaProvider):
    """Bedrock Runtime Converse, behind the standard provider contract."""

    name = "bedrock"
    # Bedrock Runtime has no free liveness endpoint — the cheapest probe is a
    # billable Converse call. So `health_check()` validates configuration only,
    # and reachability stays unknown until a real inference call settles it.
    supports_liveness_probe = False

    def __init__(self) -> None:
        settings = get_settings()
        self.region = (settings.aws_region or "").strip()
        self.model = settings.effective_bedrock_model_id
        self.temperature = float(settings.gemma_temperature)
        self.max_tokens = int(settings.effective_gemma_max_tokens)
        self.top_p = settings.gemma_top_p
        self.stop_sequences = list(settings.gemma_stop_sequence_list)
        self.max_retries = max(0, int(settings.bedrock_max_retries))
        self.connect_timeout = float(settings.bedrock_connect_timeout)
        self.read_timeout = float(settings.effective_bedrock_read_timeout)
        self.supports_images = bool(settings.effective_gemma_supports_images)
        self.max_provider_failures = int(settings.gemma_max_consecutive_failures)
        # A bool, never the token. See config.bedrock_auth_configured.
        self.auth_configured = bool(settings.bedrock_auth_configured)

        self._client: Any = None
        # Requirement: the provider retains a REAL result, not a synthesised one.
        # Read-only observability, same role as `last_generation_request`.
        self.last_model_call_result: ModelCallResult | None = None

        self.health = ProviderHealth(
            provider_type=self.name,
            model_id=self.model,
            multimodal_support=self.supports_images,
            configured=bool(self.region and self.model),
        )
        if not self.region:
            self.health.config_error = "AWS_REGION is not configured"
        elif not self.model:
            self.health.config_error = "BEDROCK_MODEL_ID is not configured"
        set_active_health(self.health)

    # -- configuration ------------------------------------------------------

    def _require_config(self) -> None:
        """Refuse to run half-configured, with the variable name to fix.

        Mirrors `OpenAICompatibleGemmaProvider._require_config`. Auth is NOT
        checked here: an absent bearer token is legitimate when the standard AWS
        credential chain supplies SigV4 credentials (an instance role, a
        profile). A genuinely missing credential surfaces as
        `missing_credentials` at call time, which says so precisely.
        """
        if not self.region:
            raise BedrockCallError("not_configured", "AWS_REGION is empty")
        if not self.model:
            raise BedrockCallError("not_configured", "BEDROCK_MODEL_ID is empty")

    def _export_bearer_token(self) -> None:
        """Make the token visible to botocore, which only reads `os.environ`.

        pydantic-settings loads `.env` into `Settings`, not into the process
        environment, so a token set only in `.env` would never reach the SDK and
        the run would silently fall back to SigV4. An already-exported value
        wins: a real environment variable is the operator's more explicit
        instruction, and overwriting it would be surprising.

        The value is read through `effective_bedrock_bearer_token` — the single
        sanctioned unwrap — and is never logged, echoed, or returned.
        """
        if not self.auth_configured:
            return
        env_var = "AWS_BEARER_TOKEN_BEDROCK"
        if os.environ.get(env_var):
            return
        os.environ[env_var] = get_settings().effective_bedrock_bearer_token
        logger.info("Bedrock bearer token exported for the AWS SDK (value not logged)")

    def _build_client(self) -> Any:
        """Construct the Bedrock Runtime client. Called at most once."""
        try:
            import boto3  # noqa: PLC0415 - lazy by design, see module docstring
            from botocore.config import Config  # noqa: PLC0415
        except ImportError as exc:
            raise BedrockCallError("not_installed", type(exc).__name__) from exc

        self._export_bearer_token()
        # `mode="standard"` counts max_attempts as TOTAL attempts including the
        # first, so a configured "3 retries" is 4 attempts. Deliberately no
        # `signature_version` and no `auth_scheme_preference`: either one makes
        # botocore skip bearer auth entirely (verified — see module docstring),
        # which would break Bedrock API keys in a way that reads as a
        # permissions error.
        config = Config(
            region_name=self.region,
            retries={"max_attempts": self.max_retries + 1, "mode": "standard"},
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
        )
        logger.info(
            "Bedrock client init region=%s model=%s auth=%s retries=%s "
            "connect_timeout=%.1fs read_timeout=%.1fs",
            self.region,
            self.model,
            "bearer_token" if self.auth_configured else "aws_credential_chain",
            self.max_retries,
            self.connect_timeout,
            self.read_timeout,
        )
        return boto3.client("bedrock-runtime", config=config)

    def _ensure_client(self) -> Any:
        if self._client is None:
            self._require_config()
            self._client = self._build_client()
        return self._client

    # -- request mapping ----------------------------------------------------

    def _image_content_blocks(self, images: list[str] | None) -> list[dict[str, Any]]:
        """Map screenshots to Converse image blocks, or return nothing.

        Reuses `prepare_image_data_url` rather than re-reading the file, so the
        existing size limits, downscaling, and the `should_skip_screenshot`
        refusal to send a `login_failed` screenshot all still apply. Only the
        container changes: a data URL carries its mime explicitly, so the
        Bedrock `format` is read, never inferred.
        """
        if not images or not self.supports_images:
            return []
        blocks: list[dict[str, Any]] = []
        for path in images[:1]:  # current screenshot only, as on the HTTP path
            data_url = prepare_image_data_url(path)
            if not data_url:
                continue
            try:
                header, encoded = data_url.split(",", 1)
                mime = header.split(";")[0].removeprefix("data:")
                image_format = DATA_URL_MIME_TO_BEDROCK_FORMAT.get(mime)
                if not image_format:
                    logger.info(
                        "Screenshot mime %r has no Converse image format; sending text only",
                        mime,
                    )
                    continue
                blocks.append(
                    {
                        "image": {
                            "format": image_format,
                            "source": {"bytes": base64.b64decode(encoded)},
                        }
                    }
                )
            except Exception as exc:
                logger.warning(
                    "Could not convert screenshot for Bedrock (%s); sending text only",
                    type(exc).__name__,
                )
        return blocks

    def _build_converse_kwargs(self, request: ModelCallRequest, images: list[str] | None) -> dict[str, Any]:
        """`ModelCallRequest` -> Converse arguments.

        Optional inference parameters are OMITTED when unset rather than sent as
        zero. `topP=0.0` means "consider only the single most likely token",
        which is a real instruction and not what "unconfigured" means.
        """
        content: list[dict[str, Any]] = [{"text": request.prompt}]
        content.extend(self._image_content_blocks(images))

        inference: dict[str, Any] = {"temperature": request.temperature}
        if request.max_tokens is not None:
            inference["maxTokens"] = request.max_tokens
        if request.top_p is not None:
            inference["topP"] = request.top_p
        if request.stop_sequences:
            inference["stopSequences"] = list(request.stop_sequences)

        kwargs: dict[str, Any] = {
            "modelId": request.model,
            "messages": [{"role": "user", "content": content}],
            "inferenceConfig": inference,
        }
        # Converse takes the system prompt as its own top-level list, not as a
        # message with role="system".
        if request.system_prompt:
            kwargs["system"] = [{"text": request.system_prompt}]
        return kwargs

    # -- response mapping ---------------------------------------------------

    @staticmethod
    def _safe_response_metadata(response: dict[str, Any]) -> dict[str, Any]:
        """The parts of a Converse response that are safe to retain.

        The full payload is NOT kept: `output.message.content` is the model's
        text, and model text can quote the prompt. What remains is counts,
        reasons and identifiers.
        """
        usage = response.get("usage") or {}
        metrics = response.get("metrics") or {}
        return {
            "stop_reason": response.get("stopReason"),
            "usage": {
                "input_tokens": usage.get("inputTokens"),
                "output_tokens": usage.get("outputTokens"),
                "total_tokens": usage.get("totalTokens"),
            },
            "service_latency_ms": metrics.get("latencyMs"),
            "http_status": (response.get("ResponseMetadata") or {}).get("HTTPStatusCode"),
        }

    def _to_result(
        self, response: Any, *, request: ModelCallRequest, latency_ms: int
    ) -> ModelCallResult:
        if not isinstance(response, dict):
            raise BedrockCallError("malformed_response", f"expected a dict, got {type(response).__name__}")
        try:
            content = ((response.get("output") or {}).get("message") or {}).get("content") or []
            if not isinstance(content, list):
                raise TypeError("content is not a list")
            text = "".join(
                block["text"] for block in content if isinstance(block, dict) and "text" in block
            )
        except (AttributeError, TypeError, KeyError) as exc:
            raise BedrockCallError(
                "malformed_response", "output.message.content was not readable"
            ) from exc

        if not text.strip():
            # Mirrors the HTTP provider's "returned empty content": an empty
            # completion cannot be parsed into an action, so failing here is
            # more useful than returning "" and failing in the parser.
            raise BedrockCallError(
                "malformed_response",
                f"no text content (stopReason={response.get('stopReason')})",
            )

        usage = response.get("usage") or {}
        return ModelCallResult(
            text=text,
            model=request.model,
            provider=self.name,
            # `.get` yields None when Bedrock omits usage, which is exactly the
            # "unreported" the result type distinguishes from a genuine zero.
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
            stop_reason=str(response.get("stopReason") or ""),
            latency_ms=latency_ms,
            raw_response=self._safe_response_metadata(response),
        )

    # -- error mapping ------------------------------------------------------

    def _redact(self, text: str) -> str:
        """Strip the bearer token out of provider-supplied text.

        Defence in depth, added when a test asked whether an AWS message echoing
        the token would be re-emitted — it would have been. AWS is not expected
        to echo a caller's credential, but `Error.Message` is the one string in a
        normalized error that this module does not author, so it is the only way
        a token could arrive from outside and reach a log.

        Reads the secret through the single sanctioned property, only on the
        error path, and never stores it.
        """
        if not text or not self.auth_configured:
            return text
        token = get_settings().effective_bedrock_bearer_token
        return text.replace(token, "[REDACTED]") if token else text

    def _normalize_error(self, exc: BaseException) -> BedrockCallError:
        """Any SDK failure -> one `FAILURE_KINDS` value.

        Ordered most specific first. botocore's timeout and connection errors
        are ClientError siblings rather than subclasses, so they are matched
        before the generic ClientError branch.
        """
        if isinstance(exc, BedrockCallError):
            return exc
        try:
            from botocore.exceptions import (  # noqa: PLC0415
                ClientError,
                ConnectionClosedError,
                ConnectTimeoutError,
                EndpointConnectionError,
                NoCredentialsError,
                PartialCredentialsError,
                ReadTimeoutError,
            )
        except ImportError:
            return BedrockCallError("unknown", type(exc).__name__)

        if isinstance(exc, ConnectTimeoutError):
            return BedrockCallError("connect_timeout", type(exc).__name__)
        if isinstance(exc, ReadTimeoutError):
            return BedrockCallError("read_timeout", type(exc).__name__)
        if isinstance(exc, (EndpointConnectionError, ConnectionClosedError)):
            return BedrockCallError("network_error", type(exc).__name__)
        if isinstance(exc, (NoCredentialsError, PartialCredentialsError)):
            return BedrockCallError("missing_credentials", type(exc).__name__)
        if isinstance(exc, ClientError):
            error = (getattr(exc, "response", None) or {}).get("Error") or {}
            code = str(error.get("Code") or "")
            message = self._redact(str(error.get("Message") or ""))
            return BedrockCallError(
                _AWS_CODE_TO_KIND.get(code, "unknown"), message, aws_code=code
            )
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            return BedrockCallError("read_timeout", type(exc).__name__)
        return BedrockCallError("unknown", type(exc).__name__)

    # -- the one method the architecture requires ---------------------------

    def _converse_sync(self, kwargs: dict[str, Any]) -> Any:
        """The blocking SDK call. Runs off the event loop, see `_generate`."""
        return self._ensure_client().converse(**kwargs)

    async def _generate(
        self,
        system: str,
        user: str,
        *,
        images: list[str] | None = None,
        temperature: float | None = None,
    ) -> str:
        """Return raw model text. Never logs prompts, output, or the token."""
        request = ModelCallRequest(
            prompt=user,
            system_prompt=system,
            model=self.model,
            temperature=self.temperature if temperature is None else float(temperature),
            max_tokens=self.max_tokens,
            top_p=self.top_p,
            stop_sequences=self.stop_sequences,
            metadata={"provider": self.name},
        )
        self._require_config()
        kwargs = self._build_converse_kwargs(request, images)
        logger.info("Bedrock converse request %s", request.telemetry())

        started = time.perf_counter()
        try:
            # boto3 is synchronous. Awaiting it on a worker thread keeps the
            # controller's event loop free — the same loop that drives the
            # browser, so blocking it would stall page observation too.
            response = await asyncio.to_thread(self._converse_sync, kwargs)
        except BaseException as exc:  # normalized, then re-raised
            error = self._normalize_error(exc)
            if error.kind in UNREACHABLE_FAILURE_KINDS:
                # `ProviderHealth.record_failure` counts failures but does not
                # touch `reachable`, which is right: most failures say nothing
                # about connectivity. These ones do, so record it here where the
                # kind is known rather than guessing in the shared helper.
                self._health().reachable = False
            logger.error(
                "Bedrock converse failed kind=%s aws_code=%s model=%s latency_ms=%d",
                error.kind,
                error.aws_code or "-",
                self.model,
                int((time.perf_counter() - started) * 1000),
            )
            raise error from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        result = self._to_result(response, request=request, latency_ms=latency_ms)
        self.last_model_call_result = result
        logger.info("Bedrock converse ok %s", result.telemetry())
        return result.text

    # -- health -------------------------------------------------------------

    async def health_check(self) -> bool:
        """Report configuration truthfully; never spend a model call to guess.

        Bedrock Runtime has no free liveness endpoint — the cheapest probe is a
        billable Converse call. So this checks what can be checked offline and
        leaves `reachable` to be set by real calls via
        `ProviderHealth.record_success/record_failure`. Reporting `reachable=True`
        because a client object constructed is precisely the kind of unearned
        claim the health model exists to avoid.
        """
        if not self.health.configured:
            self.health.reachable = False
            return False
        try:
            self._ensure_client()
        except BedrockCallError as exc:
            self.health.reachable = False
            self.health.last_error = f"{exc.kind}: client could not be constructed"
            return False
        return True
