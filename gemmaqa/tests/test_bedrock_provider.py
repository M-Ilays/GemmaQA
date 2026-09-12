"""BedrockProvider: request mapping, response mapping, and failure normalization.

No live AWS, no account, no credentials. The SDK boundary is stubbed by
injecting a fake client into `provider._client`, so `_build_client` — the only
place boto3 is imported — never runs. The error-mapping section needs botocore's
exception classes and skips itself if the optional extra is not installed.

Two behaviours are defended here because getting them wrong fails in a way that
LOOKS like something else:

1. `Config(signature_version=...)` makes botocore silently skip bearer auth, so
   a Bedrock API key reverts to SigV4 and the run reports a permissions error.
   Verified against botocore 1.43.63 by inspecting the signed request; asserted
   below at the source level.
2. Unset inference parameters must be OMITTED, not sent as zero. `topP=0.0` is a
   real instruction ("consider only the single most likely token"), not a
   synonym for "unconfigured".
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.gemma.bedrock_provider import (  # noqa: E402
    FAILURE_KINDS,
    BedrockCallError,
    BedrockProvider,
)

REGION = "eu-west-2"
MODEL = "eu.anthropic.claude-sonnet-4-20250514-v1:0"
TOKEN = "bedrock-token-that-must-never-appear-anywhere"
SOURCE = (BACKEND / "app" / "gemma" / "bedrock_provider.py").read_text(encoding="utf-8")


def _ok_response(text: str = "MODEL OUTPUT", **overrides):
    response = {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 1200, "outputTokens": 64, "totalTokens": 1264},
        "metrics": {"latencyMs": 812},
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }
    response.update(overrides)
    return response


class _FakeClient:
    """Stands in for a boto3 bedrock-runtime client."""

    def __init__(self, response=None, raises: BaseException | None = None):
        self._response = response if response is not None else _ok_response()
        self._raises = raises
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> BedrockProvider:
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    monkeypatch.setenv("GEMMA_TEMPERATURE", "0.2")
    monkeypatch.setenv("GEMMA_MAX_OUTPUT_TOKENS", "512")
    for unset in ("GEMMA_TOP_P", "GEMMA_STOP_SEQUENCES", "GEMMA_SUPPORTS_IMAGES"):
        monkeypatch.delenv(unset, raising=False)
    get_settings.cache_clear()
    yield BedrockProvider()
    get_settings.cache_clear()


def _generate(provider: BedrockProvider, client: _FakeClient, **kw) -> str:
    provider._client = client  # SDK boundary stubbed; _build_client never runs
    return asyncio.run(provider._generate(kw.pop("system", "SYS"), kw.pop("user", "USER"), **kw))


# ===========================================================================
# A -- request mapping
# ===========================================================================


def test_the_system_prompt_is_a_top_level_list_not_a_message(provider):
    """Converse takes `system` separately; a role="system" message is rejected."""
    client = _FakeClient()
    _generate(provider, client, system="RULES", user="QUESTION")
    sent = client.calls[0]

    assert sent["system"] == [{"text": "RULES"}]
    assert [m["role"] for m in sent["messages"]] == ["user"]
    assert sent["messages"][0]["content"][0] == {"text": "QUESTION"}


def test_the_model_id_comes_from_configuration(provider):
    client = _FakeClient()
    _generate(provider, client)

    assert client.calls[0]["modelId"] == MODEL


def test_unset_inference_parameters_are_omitted(provider):
    """Not sent as zero — see the module docstring on topP."""
    client = _FakeClient()
    _generate(provider, client)
    inference = client.calls[0]["inferenceConfig"]

    assert "topP" not in inference
    assert "stopSequences" not in inference
    assert inference["temperature"] == 0.2
    assert inference["maxTokens"] == 512


def test_configured_optional_parameters_are_sent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    monkeypatch.setenv("GEMMA_TOP_P", "0.9")
    monkeypatch.setenv("GEMMA_STOP_SEQUENCES", "</action>, END")
    get_settings.cache_clear()
    client = _FakeClient()
    _generate(BedrockProvider(), client)
    inference = client.calls[0]["inferenceConfig"]

    assert inference["topP"] == 0.9
    assert inference["stopSequences"] == ["</action>", "END"]
    get_settings.cache_clear()


def test_an_explicit_top_p_of_zero_is_still_sent(monkeypatch: pytest.MonkeyPatch):
    """The reason the setting is Optional rather than defaulting to 0.0."""
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    monkeypatch.setenv("GEMMA_TOP_P", "0")
    get_settings.cache_clear()
    client = _FakeClient()
    _generate(BedrockProvider(), client)

    assert client.calls[0]["inferenceConfig"]["topP"] == 0.0
    get_settings.cache_clear()


def test_a_per_call_temperature_overrides_the_configured_one(provider):
    client = _FakeClient()
    _generate(provider, client, temperature=0.75)

    assert client.calls[0]["inferenceConfig"]["temperature"] == 0.75


def test_images_are_not_sent_when_image_support_is_off(provider):
    """`GEMMA_SUPPORTS_IMAGES` is unset in the fixture, so text only."""
    client = _FakeClient()
    _generate(provider, client, images=["/nonexistent/shot.png"])
    content = client.calls[0]["messages"][0]["content"]

    assert content == [{"text": "USER"}]


def test_an_unreadable_screenshot_degrades_to_text(monkeypatch: pytest.MonkeyPatch):
    """A missing file must not fail the call — the run continues text-only, the
    same contract the HTTP provider already honours."""
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    monkeypatch.setenv("GEMMA_SUPPORTS_IMAGES", "true")
    get_settings.cache_clear()
    client = _FakeClient()
    _generate(BedrockProvider(), client, images=["/nonexistent/shot.png"])

    assert client.calls[0]["messages"][0]["content"] == [{"text": "USER"}]
    get_settings.cache_clear()


def test_only_formats_bedrock_declares_are_mapped():
    """`prepare_image_data_url` emits png/jpeg/webp; Converse's ImageFormat enum
    is png/jpeg/gif/webp. The mapping is total, so no format is ever guessed."""
    from app.gemma.bedrock_provider import DATA_URL_MIME_TO_BEDROCK_FORMAT

    assert set(DATA_URL_MIME_TO_BEDROCK_FORMAT.values()) <= {"png", "jpeg", "gif", "webp"}
    assert set(DATA_URL_MIME_TO_BEDROCK_FORMAT) == {"image/png", "image/jpeg", "image/webp"}


# ===========================================================================
# B -- response mapping
# ===========================================================================


def test_the_generated_text_is_returned(provider):
    assert _generate(provider, _FakeClient(_ok_response("ACTION JSON"))) == "ACTION JSON"


def test_multiple_text_blocks_are_concatenated(provider):
    response = _ok_response()
    response["output"]["message"]["content"] = [{"text": "part one "}, {"text": "part two"}]

    assert _generate(provider, _FakeClient(response)) == "part one part two"


def test_non_text_blocks_are_ignored(provider):
    """A reasoning or citation block alongside the answer must not break parsing."""
    response = _ok_response()
    response["output"]["message"]["content"] = [
        {"reasoningContent": {"reasoningText": {"text": "thinking"}}},
        {"text": "the answer"},
    ]

    assert _generate(provider, _FakeClient(response)) == "the answer"


def test_a_real_result_is_retained(provider):
    _generate(provider, _FakeClient(_ok_response("OUT")))
    result = provider.last_model_call_result

    assert result is not None
    assert result.provider == "bedrock"
    assert result.model == MODEL
    assert result.input_tokens == 1200
    assert result.output_tokens == 64
    assert result.total_tokens == 1264
    assert result.stop_reason == "end_turn"
    assert result.latency_ms is not None and result.latency_ms >= 0


def test_absent_usage_is_unreported_not_zero(provider):
    """Bedrock omitting usage must not be recorded as a zero-cost call."""
    _generate(provider, _FakeClient(_ok_response(usage={})))
    result = provider.last_model_call_result

    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.usage_reported is False


def test_the_retained_payload_holds_only_safe_metadata(provider):
    """Requirement: never retain sensitive raw responses. `output.message` is the
    model's text and model text can quote the prompt, so it is dropped."""
    _generate(provider, _FakeClient(_ok_response("SENSITIVE MODEL OUTPUT")))
    raw = provider.last_model_call_result.raw_response

    assert "SENSITIVE MODEL OUTPUT" not in repr(raw)
    assert "output" not in raw
    assert raw["usage"]["input_tokens"] == 1200
    assert raw["service_latency_ms"] == 812
    assert raw["http_status"] == 200


def test_result_telemetry_never_contains_the_output(provider):
    _generate(provider, _FakeClient(_ok_response("SENSITIVE MODEL OUTPUT")))

    assert "SENSITIVE" not in repr(provider.last_model_call_result.telemetry())


# ===========================================================================
# C -- malformed responses
# ===========================================================================


@pytest.mark.parametrize(
    "response",
    [
        pytest.param("not a dict", id="not_a_dict"),
        pytest.param({}, id="empty"),
        pytest.param({"output": {}}, id="no_message"),
        pytest.param({"output": {"message": {"content": "text"}}}, id="content_not_a_list"),
        pytest.param({"output": {"message": {"content": []}}}, id="no_blocks"),
        pytest.param({"output": {"message": {"content": [{"text": "   "}]}}}, id="whitespace_only"),
    ],
)
def test_a_malformed_response_is_normalized(provider, response):
    with pytest.raises(BedrockCallError) as excinfo:
        _generate(provider, _FakeClient(response))

    assert excinfo.value.kind == "malformed_response"


def test_a_malformed_response_does_not_overwrite_the_last_result(provider):
    """A failed call must not leave a half-built result behind for a reporter to
    read as though it succeeded."""
    _generate(provider, _FakeClient(_ok_response("GOOD")))
    good = provider.last_model_call_result
    with pytest.raises(BedrockCallError):
        _generate(provider, _FakeClient({}))

    assert provider.last_model_call_result is good


# ===========================================================================
# D -- failure normalization (needs the optional botocore)
# ===========================================================================

botocore_exceptions = pytest.importorskip(
    "botocore.exceptions", reason="Bedrock extra not installed"
)


def _client_error(code: str):
    return botocore_exceptions.ClientError(
        {"Error": {"Code": code, "Message": f"{code} from AWS"}}, "Converse"
    )


@pytest.mark.parametrize(
    "code,kind",
    [
        ("AccessDeniedException", "access_denied"),
        ("UnrecognizedClientException", "invalid_credentials"),
        ("InvalidSignatureException", "invalid_credentials"),
        ("ExpiredTokenException", "expired_credentials"),
        ("ValidationException", "validation_error"),
        ("ResourceNotFoundException", "invalid_model"),
        ("ThrottlingException", "throttled"),
        ("ModelTimeoutException", "read_timeout"),
        ("ServiceUnavailableException", "service_unavailable"),
        ("InternalServerException", "service_unavailable"),
        ("ModelNotReadyException", "service_unavailable"),
        ("ModelErrorException", "model_error"),
        ("SomethingAWSAddedLater", "unknown"),
    ],
)
def test_aws_error_codes_map_to_kinds(provider, code, kind):
    with pytest.raises(BedrockCallError) as excinfo:
        _generate(provider, _FakeClient(raises=_client_error(code)))

    assert excinfo.value.kind == kind
    assert excinfo.value.aws_code == code


@pytest.mark.parametrize(
    "exc,kind",
    [
        (botocore_exceptions.ConnectTimeoutError(endpoint_url="https://x"), "connect_timeout"),
        (botocore_exceptions.ReadTimeoutError(endpoint_url="https://x"), "read_timeout"),
        (botocore_exceptions.EndpointConnectionError(endpoint_url="https://x"), "network_error"),
        (botocore_exceptions.ConnectionClosedError(endpoint_url="https://x"), "network_error"),
        (botocore_exceptions.NoCredentialsError(), "missing_credentials"),
    ],
)
def test_transport_failures_map_to_kinds(provider, exc, kind):
    with pytest.raises(BedrockCallError) as excinfo:
        _generate(provider, _FakeClient(raises=exc))

    assert excinfo.value.kind == kind


def test_every_kind_is_in_the_closed_vocabulary(provider):
    for code in ("AccessDeniedException", "ValidationException", "Whatever"):
        with pytest.raises(BedrockCallError) as excinfo:
            _generate(provider, _FakeClient(raises=_client_error(code)))
        assert excinfo.value.kind in FAILURE_KINDS


def test_an_aws_message_is_truncated_in_the_normalized_error(provider):
    """AWS messages are prompt- and attacker-influenced strings, so their length
    reaching a log is bounded."""
    from app.gemma.bedrock_provider import MAX_PROVIDER_MESSAGE_CHARS

    long_message = "x" * 5000
    exc = botocore_exceptions.ClientError(
        {"Error": {"Code": "ValidationException", "Message": long_message}}, "Converse"
    )
    with pytest.raises(BedrockCallError) as excinfo:
        _generate(provider, _FakeClient(raises=exc))

    assert len(str(excinfo.value)) < MAX_PROVIDER_MESSAGE_CHARS + 400


def test_every_kind_has_operator_guidance():
    from app.gemma.bedrock_provider import _KIND_GUIDANCE

    assert set(_KIND_GUIDANCE) == set(FAILURE_KINDS)


# ===========================================================================
# E -- authentication, retries, and secret containment
# ===========================================================================


def _code_string_literals() -> list[str]:
    """Every string constant in the module EXCEPT docstrings.

    Checking raw source would match the module docstring, which quotes
    `Authorization: Bearer <token>` while documenting how the mechanism was
    verified. What matters is whether any executable code builds that header.
    """
    import ast

    tree = ast.parse(SOURCE)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_no_authorization_header_is_built_by_hand():
    """The SDK owns bearer signing. Injecting the header manually would duplicate
    an SDK responsibility and break when AWS changes the scheme."""
    offenders = [
        literal
        for literal in _code_string_literals()
        if "authorization" in literal.lower() or literal.startswith("Bearer ")
    ]

    assert offenders == [], offenders


def test_the_client_config_never_sets_a_signature_version():
    """Verified against botocore 1.43.63: setting `signature_version` or
    `auth_scheme_preference` makes `_should_prefer_bearer_auth` return False, so
    a Bedrock API key silently reverts to SigV4 and surfaces as a permissions
    error. This is the single easiest way to break API-key auth.

    Asserted on the parsed call, not on raw text, so a mention in a comment
    explaining WHY does not satisfy the test.
    """
    import ast

    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Config":
            passed = {kw.arg for kw in node.keywords}
            assert "signature_version" not in passed
            assert "auth_scheme_preference" not in passed
            break
    else:
        pytest.fail("no Config(...) call found — the client construction moved")


def test_retries_are_botocores_and_are_not_wrapped(provider):
    """Requirement: respect the configured retry limit, add no second loop. Two
    nested retry budgets multiply into a stall nobody sized."""
    with pytest.raises(BedrockCallError):
        _generate(provider, client := _FakeClient(raises=_client_error("ThrottlingException")))

    assert len(client.calls) == 1, "the provider retried on its own"


def test_the_configured_retry_count_becomes_total_attempts(monkeypatch: pytest.MonkeyPatch):
    """botocore's `max_attempts` in standard mode counts the FIRST attempt, so
    N retries is N+1 attempts. Off by one here silently changes the budget."""
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    monkeypatch.setenv("BEDROCK_MAX_RETRIES", "4")
    get_settings.cache_clear()
    captured = {}

    import boto3

    monkeypatch.setattr(
        boto3, "client",
        lambda service, config=None, **kw: captured.setdefault("config", config) or _FakeClient(),
    )
    BedrockProvider()._ensure_client()

    assert captured["config"].retries["max_attempts"] == 5
    assert captured["config"].retries["mode"] == "standard"
    get_settings.cache_clear()


def test_the_token_reaches_the_sdk_only_through_the_environment(monkeypatch: pytest.MonkeyPatch):
    """botocore reads AWS_BEARER_TOKEN_BEDROCK from os.environ and offers no
    client kwarg for it, while pydantic-settings loads .env into Settings only —
    so this bridge is required, and it is the one place the secret is handled."""
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    get_settings.cache_clear()
    p = BedrockProvider()
    # Simulate "set in .env only": Settings knows it, os.environ does not.
    monkeypatch.setattr(p, "auth_configured", True)
    monkeypatch.setattr(
        "app.gemma.bedrock_provider.get_settings",
        lambda: type("S", (), {"effective_bedrock_bearer_token": TOKEN})(),
    )
    p._export_bearer_token()

    import os as _os
    assert _os.environ["AWS_BEARER_TOKEN_BEDROCK"] == TOKEN
    get_settings.cache_clear()


def test_an_already_exported_token_is_not_overwritten(monkeypatch: pytest.MonkeyPatch):
    """A real environment variable is the operator's more explicit instruction."""
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "already-exported")
    get_settings.cache_clear()
    BedrockProvider()._export_bearer_token()

    import os as _os
    assert _os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "already-exported"
    get_settings.cache_clear()


def test_the_provider_never_unwraps_the_secret_itself():
    """Only `config.effective_bedrock_bearer_token` may call get_secret_value."""
    assert "get_secret_value" not in SOURCE
    assert "aws_bearer_token_bedrock" not in SOURCE.replace("AWS_BEARER_TOKEN_BEDROCK", "")


def test_nothing_logs_the_prompt_the_output_or_the_token(provider, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        _generate(provider, _FakeClient(_ok_response("SENSITIVE OUTPUT")), user="SENSITIVE PROMPT")
    logged = caplog.text

    assert "SENSITIVE PROMPT" not in logged
    assert "SENSITIVE OUTPUT" not in logged
    assert TOKEN not in logged


# ===========================================================================
# F -- configuration refusal, and the provider contract
# ===========================================================================


def test_a_missing_region_refuses_with_the_variable_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    monkeypatch.setenv("AWS_REGION", "")
    get_settings.cache_clear()
    p = BedrockProvider()

    assert p.health.configured is False
    assert "AWS_REGION" in p.health.config_error
    with pytest.raises(BedrockCallError) as excinfo:
        asyncio.run(p._generate("s", "u"))
    assert excinfo.value.kind == "not_configured"
    get_settings.cache_clear()


def test_a_missing_model_refuses_with_the_variable_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("BEDROCK_MODEL_ID", "")
    get_settings.cache_clear()
    p = BedrockProvider()

    assert "BEDROCK_MODEL_ID" in p.health.config_error
    with pytest.raises(BedrockCallError):
        asyncio.run(p._generate("s", "u"))
    get_settings.cache_clear()


def test_misconfiguration_never_falls_back_to_another_provider(monkeypatch: pytest.MonkeyPatch):
    """It raises. It does not quietly answer with heuristics."""
    monkeypatch.setenv("AWS_REGION", "")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "")
    get_settings.cache_clear()

    with pytest.raises(BedrockCallError):
        asyncio.run(BedrockProvider()._generate("s", "u"))
    get_settings.cache_clear()


def test_it_implements_the_existing_provider_contract(provider):
    from app.gemma.base import GemmaProvider

    assert isinstance(provider, GemmaProvider)
    assert provider.name == "bedrock"
    # Inherits every capability by implementing the one abstract method.
    for capability in ("generate_action", "analyze_page", "rank_goals", "extract_workflows"):
        assert hasattr(provider, capability)


def test_generate_still_returns_a_plain_string(provider):
    """Backward compatibility: the abstract contract is unchanged."""
    assert isinstance(_generate(provider, _FakeClient()), str)


def test_the_client_is_built_once_and_reused(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    get_settings.cache_clear()
    p = BedrockProvider()
    builds = []
    monkeypatch.setattr(p, "_build_client", lambda: builds.append(1) or _FakeClient())

    p._ensure_client()
    p._ensure_client()
    p._ensure_client()

    assert len(builds) == 1
    get_settings.cache_clear()


# A source-text check for `asyncio.to_thread` lived here. Replaced by
# `test_the_sdk_call_runs_off_the_event_loop` in section J, which proves the
# property behaviourally: a concurrent coroutine keeps ticking during the
# blocking call, and the SDK runs on a different thread. A string match would
# still pass if the call were moved back onto the loop by other means.


def test_boto3_is_imported_lazily():
    """Mock and the local providers must keep working with the extra absent."""
    import ast

    tree = ast.parse(SOURCE)
    module_level = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in getattr(node, "names", [])
    } | {
        (node.module or "").split(".")[0]
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    }

    assert "boto3" not in module_level
    assert "botocore" not in module_level


# `health_check()` not claiming unearned reachability was asserted here too.
# Removed as duplicate: `test_bedrock_health.py::test_health_check_alone_never_
# claims_reachable` covers the same property and additionally proves no Converse
# call is made and what `connection_status` the endpoint then reports.


def test_health_check_is_false_when_unconfigured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AWS_REGION", "")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "")
    get_settings.cache_clear()

    assert asyncio.run(BedrockProvider().health_check()) is False
    get_settings.cache_clear()


# Factory registration was asserted here during Steps 3-4 by grepping
# `app/gemma/__init__.py`. Removed as duplicate: `test_bedrock_factory.py` now
# proves the same thing behaviourally (`test_the_factory_returns_a_bedrock_provider`,
# `test_the_provider_is_exported_from_the_package`), and a substring check that
# agrees with a behavioural test adds no coverage while adding a second thing to
# update.


# ===========================================================================
# G -- response-field edge cases
# ===========================================================================
#
# Section B covers the happy path and absent usage. These are the fields Bedrock
# may omit or report as a genuine zero, where "unreported" and "reported as 0"
# must stay distinguishable all the way into the retained result.


def test_reported_zero_tokens_are_recorded_as_zero_not_unreported(provider):
    """A call genuinely billed at 0 output tokens is not the same fact as a
    provider that never told us. Collapsing them would misreport cost."""
    _generate(provider, _FakeClient(_ok_response(usage={"inputTokens": 0, "outputTokens": 0})))
    result = provider.last_model_call_result

    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.total_tokens == 0
    assert result.usage_reported is True


def test_a_partially_reported_usage_keeps_the_missing_side_unreported(provider):
    _generate(provider, _FakeClient(_ok_response(usage={"outputTokens": 7})))
    result = provider.last_model_call_result

    assert result.input_tokens is None
    assert result.output_tokens == 7
    assert result.total_tokens == 7


def test_a_missing_stop_reason_is_empty_not_invented(provider):
    response = _ok_response()
    response.pop("stopReason")
    _generate(provider, _FakeClient(response))

    assert provider.last_model_call_result.stop_reason == ""


def test_every_stop_reason_bedrock_declares_is_carried_through(provider):
    """From the service model's StopReason enum. `max_tokens` especially: the
    text is real but truncated, and a caller may want to know that."""
    for reason in ("end_turn", "max_tokens", "stop_sequence", "content_filtered"):
        _generate(provider, _FakeClient(_ok_response(stopReason=reason)))
        assert provider.last_model_call_result.stop_reason == reason


def test_missing_service_metrics_leave_the_service_latency_unknown(provider):
    """Our own measurement is always available; Bedrock's is not."""
    response = _ok_response()
    response.pop("metrics")
    _generate(provider, _FakeClient(response))
    result = provider.last_model_call_result

    assert result.raw_response["service_latency_ms"] is None
    assert result.latency_ms is not None, "our measurement does not depend on Bedrock"


def test_measured_latency_and_service_latency_are_separate_facts(provider):
    """`latency_ms` is wall clock as the caller experienced it, network included;
    `service_latency_ms` is Bedrock's own figure. Reporting one as the other
    would hide time spent outside the model."""
    _generate(provider, _FakeClient(_ok_response(metrics={"latencyMs": 999999})))
    result = provider.last_model_call_result

    assert result.raw_response["service_latency_ms"] == 999999
    assert result.latency_ms < 999999


def test_a_missing_http_status_does_not_break_the_result(provider):
    response = _ok_response()
    response.pop("ResponseMetadata")
    _generate(provider, _FakeClient(response))

    assert provider.last_model_call_result.raw_response["http_status"] is None


# ===========================================================================
# H -- credential failure variants not covered in section D
# ===========================================================================


@pytest.mark.parametrize("code", ["ExpiredTokenException", "ExpiredToken"])
def test_both_spellings_of_an_expired_credential_map_to_the_same_kind(provider, code):
    """AWS uses both across services; matching only one leaves the other as
    `unknown`, which sends an operator looking in the wrong place."""
    with pytest.raises(BedrockCallError) as excinfo:
        _generate(provider, _FakeClient(raises=_client_error(code)))

    assert excinfo.value.kind == "expired_credentials"


def test_a_partial_credential_set_is_reported_as_missing(provider):
    """An access key with no secret is a configuration mistake, not a network
    problem, and must not be reported as one."""
    exc = botocore_exceptions.PartialCredentialsError(
        provider="env", cred_var="AWS_SECRET_ACCESS_KEY"
    )
    with pytest.raises(BedrockCallError) as excinfo:
        _generate(provider, _FakeClient(raises=exc))

    assert excinfo.value.kind == "missing_credentials"


def test_the_guidance_for_a_credential_failure_names_the_variable(provider):
    """The error has to be actionable without reading this source file."""
    with pytest.raises(BedrockCallError) as excinfo:
        _generate(provider, _FakeClient(raises=botocore_exceptions.NoCredentialsError()))

    assert "AWS_BEARER_TOKEN_BEDROCK" in str(excinfo.value)


# ===========================================================================
# I -- transport configuration actually reaches botocore
# ===========================================================================


def _captured_config(monkeypatch: pytest.MonkeyPatch, **env: str):
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    captured = {}

    import boto3

    monkeypatch.setattr(
        boto3,
        "client",
        lambda service, config=None, **kw: captured.setdefault("config", config) or _FakeClient(),
    )
    BedrockProvider()._ensure_client()
    get_settings.cache_clear()
    return captured["config"]


def test_the_timeouts_reach_the_client_config(monkeypatch: pytest.MonkeyPatch):
    """Settings that never reach botocore are settings that do nothing. The retry
    count was already asserted; these were not."""
    config = _captured_config(
        monkeypatch, BEDROCK_CONNECT_TIMEOUT="3.5", BEDROCK_READ_TIMEOUT="45"
    )

    assert config.connect_timeout == 3.5
    assert config.read_timeout == 45.0


def test_the_read_timeout_falls_back_to_the_global_one(monkeypatch: pytest.MonkeyPatch):
    """Preserves existing behaviour: GEMMA_TIMEOUT_SECONDS keeps governing until
    Bedrock is given its own read timeout."""
    monkeypatch.delenv("BEDROCK_READ_TIMEOUT", raising=False)
    config = _captured_config(monkeypatch, GEMMA_TIMEOUT_SECONDS="21")

    assert config.read_timeout == 21.0


def test_the_region_reaches_the_client_config(monkeypatch: pytest.MonkeyPatch):
    assert _captured_config(monkeypatch).region_name == REGION


def test_zero_configured_retries_still_allows_one_attempt(monkeypatch: pytest.MonkeyPatch):
    """`max_attempts=0` would mean botocore never sends the request at all."""
    config = _captured_config(monkeypatch, BEDROCK_MAX_RETRIES="0")

    assert config.retries["max_attempts"] == 1


# ===========================================================================
# J -- the event loop is genuinely free during the blocking SDK call
# ===========================================================================


def test_the_sdk_call_runs_off_the_event_loop(provider):
    """Behavioural, not a source-text match. boto3 is synchronous, and the loop
    it would block is the SAME loop that drives the browser — so an inline call
    freezes page observation for the duration of every model call.

    Proven two ways at once: another coroutine keeps running while the SDK is
    blocked, and the SDK executes on a different thread from the loop's.
    """
    import threading
    import time as _time

    loop_thread = None
    sdk_thread = None

    class _BlockingClient:
        def converse(self, **kwargs):
            nonlocal sdk_thread
            sdk_thread = threading.get_ident()
            _time.sleep(0.2)  # a real, blocking SDK call
            return _ok_response()

    async def scenario():
        nonlocal loop_thread
        loop_thread = threading.get_ident()
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(ticker())
        provider._client = _BlockingClient()
        await provider._generate("s", "u")
        beat.cancel()
        return ticks

    ticks = asyncio.run(scenario())

    assert ticks > 0, "the event loop was blocked for the whole SDK call"
    assert sdk_thread is not None and sdk_thread != loop_thread


# ===========================================================================
# K -- the token stays out of every provider-side surface
# ===========================================================================
#
# The health payload is covered in test_bedrock_health.py. These are the
# provider-side surfaces: the raised exception, the retained result, and the
# provider object itself.


@pytest.fixture
def provider_with_token(monkeypatch: pytest.MonkeyPatch) -> BedrockProvider:
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", TOKEN)
    get_settings.cache_clear()
    yield BedrockProvider()
    get_settings.cache_clear()


def test_the_token_is_absent_from_the_provider_repr(provider_with_token):
    """It is never stored on the instance — only `auth_configured`, a bool."""
    assert TOKEN not in repr(provider_with_token)
    assert TOKEN not in repr(vars(provider_with_token))


def test_the_token_is_absent_from_the_retained_result(provider_with_token):
    _generate(provider_with_token, _FakeClient())
    result = provider_with_token.last_model_call_result

    assert TOKEN not in repr(result)
    assert TOKEN not in repr(result.telemetry())
    assert TOKEN not in repr(result.raw_response)


def test_an_aws_message_echoing_the_token_is_redacted(provider_with_token):
    """Defence in depth. AWS is not expected to echo a caller's credential, but
    the provider-supplied message is the one string in a normalized error that
    this code does not author — so it is the one place a token could arrive from
    outside and be re-emitted into a log."""
    exc = botocore_exceptions.ClientError(
        {"Error": {"Code": "ValidationException", "Message": f"bad request using {TOKEN}"}},
        "Converse",
    )
    with pytest.raises(BedrockCallError) as excinfo:
        _generate(provider_with_token, _FakeClient(raises=exc))

    assert TOKEN not in str(excinfo.value)
    assert excinfo.value.kind == "validation_error"


def test_redaction_does_not_swallow_the_rest_of_the_message(provider_with_token):
    exc = botocore_exceptions.ClientError(
        {"Error": {"Code": "ValidationException", "Message": f"model {TOKEN} unsupported"}},
        "Converse",
    )
    with pytest.raises(BedrockCallError) as excinfo:
        _generate(provider_with_token, _FakeClient(raises=exc))

    assert "unsupported" in str(excinfo.value)
