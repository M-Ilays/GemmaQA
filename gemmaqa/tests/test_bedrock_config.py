"""Bedrock configuration: env loading, alias mapping, and secret containment.

Step 2 of the Bedrock integration — configuration only. No provider exists yet,
so nothing here constructs one.

The load-bearing property is the last section: `AWS_BEARER_TOKEN_BEDROCK` must
not be reachable by accident. It is a `SecretStr`, so a repr, an f-string, a
`print(settings)`, or a pydantic validation error renders `**********` instead of
the token. Callers that only need to know whether auth EXISTS ask
`bedrock_auth_configured`, which is a bool — so there is no code path where
someone has to remember to mask it.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402

TOKEN = "bedrock-bearer-token-that-must-never-leak"


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """`get_settings` is lru_cached, so env changes need the cache cleared."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    return get_settings()


# ===========================================================================
# A -- the provider alias map
# ===========================================================================


@pytest.mark.parametrize("value", ["bedrock", "BEDROCK", "aws", "aws_bedrock", "amazon_bedrock", " Bedrock "])
def test_bedrock_aliases_normalize(monkeypatch: pytest.MonkeyPatch, value: str):
    assert _settings(monkeypatch, GEMMA_PROVIDER=value).normalized_gemma_provider == "bedrock"


def test_the_existing_aliases_are_untouched(monkeypatch: pytest.MonkeyPatch):
    """Adding a branch to the alias map must not shadow any existing mapping."""
    for value, expected in (
        ("mock", "mock"), ("stub", "mock"), ("heuristic", "mock"), ("dev", "mock"),
        ("ollama", "openai_compatible"), ("openai", "openai_compatible"),
        ("api", "openai_compatible"), ("kaggle", "openai_compatible"),
        ("transformers", "transformers"), ("hf", "transformers"),
    ):
        assert _settings(monkeypatch, GEMMA_PROVIDER=value).normalized_gemma_provider == expected, value


def test_an_unknown_provider_still_falls_through_unmapped(monkeypatch: pytest.MonkeyPatch):
    """The factory relies on this to raise rather than silently use mock."""
    assert _settings(monkeypatch, GEMMA_PROVIDER="not-a-provider").normalized_gemma_provider == "not-a-provider"


def test_the_default_provider_is_still_mock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMMA_PROVIDER", raising=False)
    get_settings.cache_clear()
    assert get_settings().normalized_gemma_provider == "mock"


# ===========================================================================
# B -- Bedrock settings load from the environment
# ===========================================================================


def test_region_and_model_load_from_env(monkeypatch: pytest.MonkeyPatch):
    settings = _settings(
        monkeypatch,
        AWS_REGION="eu-west-2",
        BEDROCK_MODEL_ID="eu.anthropic.claude-sonnet-4-20250514-v1:0",
    )

    assert settings.aws_region == "eu-west-2"
    assert settings.effective_bedrock_model_id == "eu.anthropic.claude-sonnet-4-20250514-v1:0"


def test_nothing_bedrock_is_hardcoded(monkeypatch: pytest.MonkeyPatch):
    """Unset means empty, never a guessed region or a default model id."""
    for key in ("AWS_REGION", "BEDROCK_MODEL_ID", "AWS_BEARER_TOKEN_BEDROCK"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    settings = get_settings()

    assert settings.aws_region == ""
    assert settings.effective_bedrock_model_id == ""
    assert settings.bedrock_auth_configured is False


def test_the_model_id_never_falls_back_to_the_gemma_one(monkeypatch: pytest.MonkeyPatch):
    """A Bedrock model id and an Ollama tag look nothing alike. Borrowing one for
    the other turns a missing-config error into an opaque AWS ValidationException.
    """
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    settings = _settings(monkeypatch, GEMMA_MODEL_ID="gemma3:4b")

    assert settings.effective_gemma_model_id == "gemma3:4b"
    assert settings.effective_bedrock_model_id == ""


def test_transport_defaults_are_sane(monkeypatch: pytest.MonkeyPatch):
    for key in ("BEDROCK_MAX_RETRIES", "BEDROCK_CONNECT_TIMEOUT", "BEDROCK_READ_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    settings = get_settings()

    assert settings.bedrock_max_retries == 3
    assert settings.bedrock_connect_timeout == 10.0


def test_transport_settings_are_overridable(monkeypatch: pytest.MonkeyPatch):
    settings = _settings(
        monkeypatch,
        BEDROCK_MAX_RETRIES="5",
        BEDROCK_CONNECT_TIMEOUT="2.5",
        BEDROCK_READ_TIMEOUT="90",
    )

    assert settings.bedrock_max_retries == 5
    assert settings.bedrock_connect_timeout == 2.5
    assert settings.effective_bedrock_read_timeout == 90.0


def test_the_read_timeout_inherits_the_existing_global_one(monkeypatch: pytest.MonkeyPatch):
    """Preserves existing behaviour: GEMMA_TIMEOUT_SECONDS keeps governing unless
    Bedrock is given its own read timeout."""
    monkeypatch.delenv("BEDROCK_READ_TIMEOUT", raising=False)
    settings = _settings(monkeypatch, GEMMA_TIMEOUT_SECONDS="42")

    assert settings.bedrock_read_timeout is None
    assert settings.effective_bedrock_read_timeout == 42.0


# ===========================================================================
# C -- provider-neutral decoding knobs
# ===========================================================================


def test_top_p_is_unset_by_default_not_zero(monkeypatch: pytest.MonkeyPatch):
    """None means "do not send the parameter"; 0.0 would mean "consider only the
    single most likely token", which is a very different request."""
    monkeypatch.delenv("GEMMA_TOP_P", raising=False)
    get_settings.cache_clear()

    assert get_settings().gemma_top_p is None


def test_top_p_loads_when_set(monkeypatch: pytest.MonkeyPatch):
    assert _settings(monkeypatch, GEMMA_TOP_P="0.9").gemma_top_p == 0.9


def test_top_p_of_zero_is_distinguishable_from_unset(monkeypatch: pytest.MonkeyPatch):
    assert _settings(monkeypatch, GEMMA_TOP_P="0").gemma_top_p == 0.0


def test_stop_sequences_parse_from_a_comma_separated_list(monkeypatch: pytest.MonkeyPatch):
    settings = _settings(monkeypatch, GEMMA_STOP_SEQUENCES="</action>, END , ")

    assert settings.gemma_stop_sequence_list == ["</action>", "END"]


def test_no_stop_sequences_means_an_empty_list(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMMA_STOP_SEQUENCES", raising=False)
    get_settings.cache_clear()

    assert get_settings().gemma_stop_sequence_list == []


# ===========================================================================
# D -- the bearer token must not leak
# ===========================================================================


def test_auth_configured_is_reported_without_revealing_the_token(monkeypatch: pytest.MonkeyPatch):
    settings = _settings(monkeypatch, AWS_BEARER_TOKEN_BEDROCK=TOKEN)

    assert settings.bedrock_auth_configured is True
    assert TOKEN not in repr(settings.bedrock_auth_configured)


def test_a_whitespace_only_token_does_not_count_as_configured(monkeypatch: pytest.MonkeyPatch):
    """Otherwise a stray space in a .env reads as "auth is set up" and the
    failure surfaces later as an opaque AccessDenied."""
    assert _settings(monkeypatch, AWS_BEARER_TOKEN_BEDROCK="   ").bedrock_auth_configured is False


def test_the_token_is_absent_from_the_settings_repr(monkeypatch: pytest.MonkeyPatch):
    settings = _settings(monkeypatch, AWS_BEARER_TOKEN_BEDROCK=TOKEN)

    assert TOKEN not in repr(settings)
    assert TOKEN not in str(settings)


def test_the_token_is_absent_from_a_model_dump(monkeypatch: pytest.MonkeyPatch):
    """`model_dump()` is what a careless debug endpoint would serialise."""
    settings = _settings(monkeypatch, AWS_BEARER_TOKEN_BEDROCK=TOKEN)

    assert TOKEN not in repr(settings.model_dump())


def test_the_token_is_absent_from_the_single_field_repr(monkeypatch: pytest.MonkeyPatch):
    settings = _settings(monkeypatch, AWS_BEARER_TOKEN_BEDROCK=TOKEN)

    assert TOKEN not in repr(settings.aws_bearer_token_bedrock)
    assert TOKEN not in f"{settings.aws_bearer_token_bedrock}"


def test_exactly_one_property_unwraps_the_token(monkeypatch: pytest.MonkeyPatch):
    """The provider needs the real value to build its client. That unwrap lives
    in one named place so it is greppable and reviewable."""
    settings = _settings(monkeypatch, AWS_BEARER_TOKEN_BEDROCK=f"  {TOKEN}  ")

    assert settings.effective_bedrock_bearer_token == TOKEN

    source = (BACKEND / "app" / "config.py").read_text(encoding="utf-8")
    assert source.count("aws_bearer_token_bedrock.get_secret_value()") == 2, (
        "only bedrock_auth_configured and effective_bedrock_bearer_token may "
        "unwrap the Bedrock token"
    )


def test_no_module_outside_config_unwraps_the_token():
    """Step 2 boundary: nothing reads the secret yet. When the provider lands it
    must go through `effective_bedrock_bearer_token`, not re-unwrap it."""
    offenders = [
        str(path.relative_to(BACKEND))
        for path in (BACKEND / "app").rglob("*.py")
        if path.name != "config.py"
        and "aws_bearer_token_bedrock" in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], offenders
