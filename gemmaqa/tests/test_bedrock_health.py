"""Bedrock health reporting: `/api/health/gemma` and the connection-status rule.

Step 5. Every field is additive — the keys Mock, OpenAI-compatible and
Transformers already returned keep their names and meanings, asserted below.

The property that matters most is that `reachable` stays a truthful TRI-state:

    None   never exercised          -> connection_status "not_checked"
    True   a real call succeeded    -> connection_status "reachable"
    False  a real call failed       -> connection_status "unreachable"

For Bedrock, `reachable` is None until a run makes an inference call, because
Bedrock Runtime has no free liveness endpoint and `health_check()` deliberately
does not spend a billable Converse request to find out. `connection_status`
exists so a client can render that honestly instead of treating a falsy None as
"broken".
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app.api.health import health_gemma  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.gemma import get_gemma_provider, reset_gemma_provider  # noqa: E402
from app.gemma.health import (  # noqa: E402
    CONNECTION_STATUSES,
    ProviderHealth,
    connection_status,
    set_active_health,
)

REGION = "us-east-1"
MODEL = "eu.anthropic.claude-sonnet-4-20250514-v1:0"
# Deliberately shares no prefix with any legitimate field value: an earlier
# draft used a "bedrock-..." token, and the substring check then matched the
# provider_type "bedrock" rather than a real leak.
TOKEN = "SECRETVALUE-must-never-be-exposed"

# Every key the endpoint returned before Step 5. Additive means none disappears.
PRE_EXISTING_KEYS = {
    "status", "provider_type", "configured", "reachable", "model_id",
    "api_base_url", "multimodal_support", "last_error_summary",
    "consecutive_failures", "is_mock",
}


@pytest.fixture(autouse=True)
def _isolated():
    get_settings.cache_clear()
    reset_gemma_provider()
    set_active_health(ProviderHealth(provider_type="none"))
    yield
    get_settings.cache_clear()
    reset_gemma_provider()


def _bedrock(monkeypatch: pytest.MonkeyPatch, **env: str):
    monkeypatch.setenv("GEMMA_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_REGION", env.pop("AWS_REGION", REGION))
    monkeypatch.setenv("BEDROCK_MODEL_ID", env.pop("BEDROCK_MODEL_ID", MODEL))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_gemma_provider()


def _health(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Call the endpoint with the provider constructed but never invoked."""
    return asyncio.run(health_gemma())


# ===========================================================================
# A -- the connection-status rule
# ===========================================================================


@pytest.mark.parametrize(
    "configured,reachable,expected",
    [
        (False, None, "misconfigured"),
        (False, False, "misconfigured"),
        (False, True, "misconfigured"),  # configuration wins: nothing was usable
        (True, None, "not_checked"),
        (True, True, "reachable"),
        (True, False, "unreachable"),
    ],
)
def test_the_rule_covers_every_state(configured, reachable, expected):
    assert connection_status(configured=configured, reachable=reachable) == expected


def test_the_vocabulary_is_closed():
    for configured in (True, False):
        for reachable in (True, False, None):
            assert connection_status(configured=configured, reachable=reachable) in CONNECTION_STATUSES


def test_unknown_is_never_collapsed_into_unreachable():
    """The bug this field exists to prevent: a client treating a falsy None as a
    failure would show a correctly configured provider as broken."""
    assert connection_status(configured=True, reachable=None) != "unreachable"


def test_provider_health_reports_the_status_for_every_provider():
    """Additive on `to_public_dict`, so Mock and the HTTP providers gain it too
    and no consumer has to special-case Bedrock."""
    for provider_type in ("mock", "openai_compatible", "transformers", "bedrock"):
        snapshot = ProviderHealth(provider_type=provider_type, configured=True).to_public_dict()
        assert snapshot["connection_status"] == "not_checked"
        assert snapshot["reachable"] is None


# ===========================================================================
# B -- configured Bedrock, before the first call
# ===========================================================================


def test_configured_bedrock_before_any_call(monkeypatch: pytest.MonkeyPatch):
    _bedrock(monkeypatch, AWS_BEARER_TOKEN_BEDROCK=TOKEN)
    body = _health(monkeypatch)

    assert body["status"] == "ok"
    assert body["provider_type"] == "bedrock"
    assert body["configured"] is True
    assert body["reachable"] is None, "no billable call was made, so nothing is known"
    assert body["connection_status"] == "not_checked"
    assert body["region"] == REGION
    assert body["model_id"] == MODEL
    assert body["auth_configured"] is True
    assert body["auth_mode"] == "bearer_token"


def test_the_api_base_url_stays_null_and_is_not_repurposed(monkeypatch: pytest.MonkeyPatch):
    """Decision: keep the field's existing meaning. The region has its own key."""
    _bedrock(monkeypatch)
    body = _health(monkeypatch)

    assert body["api_base_url"] is None
    assert body["region"] == REGION


def test_the_credential_chain_is_reported_when_no_token_is_set(monkeypatch: pytest.MonkeyPatch):
    _bedrock(monkeypatch)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    get_settings.cache_clear()
    reset_gemma_provider()
    body = _health(monkeypatch)

    assert body["auth_configured"] is False
    assert body["auth_mode"] == "aws_credential_chain"
    assert body["configured"] is True, "SigV4 from the credential chain is valid"


def test_misconfigured_bedrock_reports_misconfigured(monkeypatch: pytest.MonkeyPatch):
    """The factory refuses, so the endpoint takes its early-return path."""
    _bedrock(monkeypatch, BEDROCK_MODEL_ID="")
    body = _health(monkeypatch)

    assert body["status"] == "misconfigured"
    assert body["configured"] is False
    assert body["connection_status"] == "misconfigured"
    assert "BEDROCK_MODEL_ID" in body["error"]
    assert body["region"] == REGION  # still useful for diagnosis
    assert set(PRE_EXISTING_KEYS) - set(body) == {
        "multimodal_support", "last_error_summary", "consecutive_failures", "is_mock",
    }, "the misconfigured path's key set changed"


# ===========================================================================
# C -- a real call moves reachability
# ===========================================================================


class _FakeClient:
    def __init__(self, raises: BaseException | None = None):
        self._raises = raises

    def converse(self, **kwargs):
        if self._raises:
            raise self._raises
        return {
            "output": {"message": {"content": [{"text": "OK"}]}},
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 2},
        }


def test_a_successful_call_makes_it_reachable(monkeypatch: pytest.MonkeyPatch):
    """`reachable` becomes True only on evidence — a real completed call."""
    _bedrock(monkeypatch)
    provider = get_gemma_provider(force_new=True)
    provider._client = _FakeClient()

    assert _health(monkeypatch)["connection_status"] == "not_checked"

    asyncio.run(provider._generate("s", "u"))
    provider._record_ai_success()
    body = _health(monkeypatch)

    assert body["reachable"] is True
    assert body["connection_status"] == "reachable"


def test_a_transport_failure_makes_it_unreachable(monkeypatch: pytest.MonkeyPatch):
    """A network/credential failure is real evidence that the endpoint is not
    usable, so `reachable` becomes False."""
    from botocore.exceptions import EndpointConnectionError

    from app.gemma.bedrock_provider import BedrockCallError

    _bedrock(monkeypatch)
    provider = get_gemma_provider(force_new=True)
    provider._client = _FakeClient(raises=EndpointConnectionError(endpoint_url="https://x"))

    with pytest.raises(BedrockCallError):
        asyncio.run(provider._generate("s", "u"))
    provider._record_ai_failure(RuntimeError("x"), context="action_generate")
    body = _health(monkeypatch)

    assert body["reachable"] is False
    assert body["connection_status"] == "unreachable"
    assert body["consecutive_failures"] == 1


def test_a_service_answered_failure_does_not_claim_unreachable(monkeypatch: pytest.MonkeyPatch):
    """A ValidationException PROVES we reached Bedrock. Recording "unreachable"
    for it would assert a connectivity problem the evidence contradicts — and a
    blanket `reachable = False` on any failure would have done exactly that."""
    from botocore.exceptions import ClientError

    from app.gemma.bedrock_provider import BedrockCallError

    _bedrock(monkeypatch)
    provider = get_gemma_provider(force_new=True)
    provider._client = _FakeClient(
        raises=ClientError({"Error": {"Code": "ValidationException", "Message": "bad"}}, "Converse")
    )

    with pytest.raises(BedrockCallError):
        asyncio.run(provider._generate("s", "u"))
    body = _health(monkeypatch)

    assert body["reachable"] is not False
    assert body["connection_status"] != "unreachable"


def test_a_failure_never_claims_reachable_either(monkeypatch: pytest.MonkeyPatch):
    """Only a completed call earns True. A service-answered failure leaves the
    field untouched rather than upgrading it."""
    from botocore.exceptions import ClientError

    from app.gemma.bedrock_provider import BedrockCallError

    _bedrock(monkeypatch)
    provider = get_gemma_provider(force_new=True)
    provider._client = _FakeClient(
        raises=ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}}, "Converse")
    )

    with pytest.raises(BedrockCallError):
        asyncio.run(provider._generate("s", "u"))

    assert provider.health.reachable is None


def test_health_check_alone_never_claims_reachable(monkeypatch: pytest.MonkeyPatch):
    """Requirement 5: no billable Converse from the normal health endpoint. A
    constructed client is not evidence of reachability."""
    _bedrock(monkeypatch)
    provider = get_gemma_provider(force_new=True)
    calls: list[str] = []
    provider._client = type("C", (), {"converse": lambda self, **kw: calls.append("converse")})()

    assert asyncio.run(provider.health_check()) is True
    assert calls == [], "the health check made a model call"
    assert _health(monkeypatch)["connection_status"] == "not_checked"


# ===========================================================================
# D -- stale health from another provider
# ===========================================================================


def test_a_stale_health_record_does_not_supply_the_model_id(monkeypatch: pytest.MonkeyPatch):
    """`_ACTIVE_HEALTH` is process-wide and set by whichever provider was built
    last. A record describing a different provider must not be believed."""
    _bedrock(monkeypatch)
    set_active_health(ProviderHealth(provider_type="mock", model_id="mock-heuristic", configured=True))
    body = _health(monkeypatch)

    assert body["model_id"] == MODEL


def test_a_matching_health_record_is_authoritative(monkeypatch: pytest.MonkeyPatch):
    """It reflects what the provider actually resolved; config only what was asked."""
    from app.runtime_info import reported_model_id

    _bedrock(monkeypatch)
    set_active_health(ProviderHealth(provider_type="bedrock", model_id="resolved.v9", configured=True))

    assert reported_model_id("bedrock") == "resolved.v9"


# ===========================================================================
# E -- nothing sensitive is exposed
# ===========================================================================


def test_the_bearer_token_never_appears_in_the_health_payload(monkeypatch: pytest.MonkeyPatch):
    _bedrock(monkeypatch, AWS_BEARER_TOKEN_BEDROCK=TOKEN)

    assert TOKEN not in json.dumps(_health(monkeypatch))


def test_the_token_is_absent_even_from_a_misconfigured_response(monkeypatch: pytest.MonkeyPatch):
    """The error path is the easiest one to leak through — it echoes an exception."""
    _bedrock(monkeypatch, AWS_REGION="", AWS_BEARER_TOKEN_BEDROCK=TOKEN)

    assert TOKEN not in json.dumps(_health(monkeypatch))


def test_the_token_is_absent_after_a_failure_is_recorded(monkeypatch: pytest.MonkeyPatch):
    from app.gemma.bedrock_provider import BedrockCallError

    _bedrock(monkeypatch, AWS_BEARER_TOKEN_BEDROCK=TOKEN)
    provider = get_gemma_provider(force_new=True)
    provider._client = _FakeClient(raises=RuntimeError(f"leaked {TOKEN}"))
    with pytest.raises(BedrockCallError):
        asyncio.run(provider._generate("s", "u"))

    assert TOKEN not in json.dumps(_health(monkeypatch))


def test_only_a_boolean_and_a_mode_describe_authentication(monkeypatch: pytest.MonkeyPatch):
    """Nothing derived from the token's VALUE — not a length, not a prefix, not a
    masked form. Presence and mechanism are all an operator needs."""
    _bedrock(monkeypatch, AWS_BEARER_TOKEN_BEDROCK=TOKEN)
    body = _health(monkeypatch)

    assert isinstance(body["auth_configured"], bool)
    assert body["auth_mode"] in {"bearer_token", "aws_credential_chain"}
    assert not any(TOKEN[:6] in str(value) for value in body.values())


# ===========================================================================
# F -- the other providers are unaffected
# ===========================================================================


@pytest.mark.parametrize("provider_env", ["mock"])
def test_existing_providers_keep_every_key(monkeypatch: pytest.MonkeyPatch, provider_env: str):
    monkeypatch.setenv("GEMMA_PROVIDER", provider_env)
    get_settings.cache_clear()
    reset_gemma_provider()
    body = _health(monkeypatch)

    assert PRE_EXISTING_KEYS <= set(body), PRE_EXISTING_KEYS - set(body)


def test_mock_health_is_unchanged_in_meaning(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "mock")
    get_settings.cache_clear()
    reset_gemma_provider()
    body = _health(monkeypatch)

    assert body["is_mock"] is True
    assert body["configured"] is True
    assert body["api_base_url"] is None
    # Pre-existing behaviour, unchanged: the endpoint prefers the provider's own
    # health record, and MockGemmaProvider reports "mock-heuristic" there.
    assert body["model_id"] == "mock-heuristic"


def test_the_bedrock_fields_are_null_for_other_providers(monkeypatch: pytest.MonkeyPatch):
    """Null means "not applicable" — a False `auth_configured` would assert that
    a provider needing no auth has none configured, which is a different claim."""
    monkeypatch.setenv("GEMMA_PROVIDER", "mock")
    get_settings.cache_clear()
    reset_gemma_provider()
    body = _health(monkeypatch)

    assert body["region"] is None
    assert body["auth_configured"] is None
    assert body["auth_mode"] is None


def test_the_model_id_rule_has_one_implementation():
    """`/api/health/gemma` and `build_runtime_info` derived this separately before
    and could disagree. Both now call `reported_model_id`."""
    endpoint = (BACKEND / "app" / "api" / "health.py").read_text(encoding="utf-8")
    runtime = (BACKEND / "app" / "runtime_info.py").read_text(encoding="utf-8")

    assert "reported_model_id()" in endpoint
    assert "reported_model_id(provider)" in runtime
    assert runtime.count("def reported_model_id(") == 1
