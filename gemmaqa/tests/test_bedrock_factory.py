"""Factory registration and runtime reporting for the Bedrock provider.

Step 4. Selecting `GEMMA_PROVIDER=bedrock` must switch the whole application
without any other module changing, and must refuse rather than quietly degrade
when configuration is incomplete.

The no-silent-fallback rule is the one worth defending hardest. A provider that
answers with mock heuristics when its real backend is misconfigured produces a
report that looks like a successful test run and is worthless — the failure has
to be loud at construction.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.gemma import (  # noqa: E402
    BedrockProvider,
    get_gemma_provider,
    reset_gemma_provider,
)
from app.gemma.mock_provider import MockGemmaProvider  # noqa: E402
from app.runtime_info import (  # noqa: E402
    build_runtime_info,
    provider_capability_mode,
    provider_display_name,
    run_mode_label,
)

REGION = "us-east-1"
MODEL = "eu.anthropic.claude-sonnet-4-20250514-v1:0"


@pytest.fixture(autouse=True)
def _isolated_provider():
    get_settings.cache_clear()
    reset_gemma_provider()
    yield
    get_settings.cache_clear()
    reset_gemma_provider()


def _configure(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    monkeypatch.setenv("GEMMA_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_REGION", env.pop("AWS_REGION", REGION))
    monkeypatch.setenv("BEDROCK_MODEL_ID", env.pop("BEDROCK_MODEL_ID", MODEL))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_gemma_provider()


# ===========================================================================
# A -- selecting bedrock switches the application
# ===========================================================================


def test_the_factory_returns_a_bedrock_provider(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)

    assert isinstance(get_gemma_provider(force_new=True), BedrockProvider)


@pytest.mark.parametrize("alias", ["bedrock", "aws", "aws_bedrock", "amazon_bedrock", "BEDROCK"])
def test_every_alias_reaches_the_provider(monkeypatch: pytest.MonkeyPatch, alias: str):
    _configure(monkeypatch)
    monkeypatch.setenv("GEMMA_PROVIDER", alias)
    get_settings.cache_clear()
    reset_gemma_provider()

    assert isinstance(get_gemma_provider(force_new=True), BedrockProvider)


def test_the_provider_is_cached_like_the_others(monkeypatch: pytest.MonkeyPatch):
    """One client, one set of credentials, one health record per process."""
    _configure(monkeypatch)

    assert get_gemma_provider(force_new=True) is get_gemma_provider()


def test_the_provider_is_exported_from_the_package(monkeypatch: pytest.MonkeyPatch):
    import app.gemma as gemma_package

    assert "BedrockProvider" in gemma_package.__all__
    assert gemma_package.BedrockProvider is BedrockProvider


def test_construction_needs_no_aws_call(monkeypatch: pytest.MonkeyPatch):
    """The factory must stay constructible offline: no network, no credential
    resolution, and no client until the first generate.

    (Whether boto3 itself stays unimported is asserted in
    `test_bedrock_provider.py::test_boto3_is_imported_lazily`, which reads the
    module's AST — checking `sys.modules` here would be unreliable, since any
    earlier test in the session may legitimately have imported it.)
    """
    _configure(monkeypatch)
    provider = get_gemma_provider(force_new=True)

    assert provider._client is None


# ===========================================================================
# B -- no silent fallback
# ===========================================================================


def test_a_missing_region_raises_and_names_the_variable(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch, AWS_REGION="")

    with pytest.raises(RuntimeError) as excinfo:
        get_gemma_provider(force_new=True)

    assert "AWS_REGION" in str(excinfo.value)
    assert "Refusing silent fallback to mock" in str(excinfo.value)


def test_a_missing_model_raises_and_names_the_variable(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch, BEDROCK_MODEL_ID="")

    with pytest.raises(RuntimeError) as excinfo:
        get_gemma_provider(force_new=True)

    assert "BEDROCK_MODEL_ID" in str(excinfo.value)


def test_misconfiguration_never_yields_a_mock_provider(monkeypatch: pytest.MonkeyPatch):
    """The whole point: a report from a mock provider looks like a successful
    test run. Failing loudly at construction is the only safe behaviour."""
    _configure(monkeypatch, AWS_REGION="", BEDROCK_MODEL_ID="")

    with pytest.raises(RuntimeError):
        get_gemma_provider(force_new=True)
    assert not isinstance(getattr(get_gemma_provider, "_cached", None), MockGemmaProvider)


def test_a_missing_bearer_token_is_not_a_configuration_error(monkeypatch: pytest.MonkeyPatch):
    """An absent token is legitimate: the standard AWS credential chain (instance
    role, named profile) supplies SigV4. Refusing here would break a valid
    deployment; a genuinely missing credential surfaces at call time instead."""
    _configure(monkeypatch)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    get_settings.cache_clear()
    reset_gemma_provider()

    provider = get_gemma_provider(force_new=True)
    assert isinstance(provider, BedrockProvider)
    assert provider.auth_configured is False
    assert provider.health.configured is True


def test_the_unknown_provider_message_lists_bedrock(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMMA_PROVIDER", "not-a-real-provider")
    get_settings.cache_clear()
    reset_gemma_provider()

    with pytest.raises(RuntimeError) as excinfo:
        get_gemma_provider(force_new=True)

    assert "bedrock" in str(excinfo.value)


def test_the_other_providers_still_resolve(monkeypatch: pytest.MonkeyPatch):
    """Adding a branch must not disturb the ones beside it."""
    monkeypatch.setenv("GEMMA_PROVIDER", "mock")
    get_settings.cache_clear()
    reset_gemma_provider()

    assert isinstance(get_gemma_provider(force_new=True), MockGemmaProvider)


# ===========================================================================
# C -- runtime reporting
# ===========================================================================


def test_the_display_name_does_not_claim_the_model_is_gemma(monkeypatch: pytest.MonkeyPatch):
    """BEDROCK_MODEL_ID is frequently Claude, Nova or Llama. "Gemma via Bedrock"
    would misreport every such run."""
    _configure(monkeypatch)

    assert provider_display_name("bedrock") == "Amazon Bedrock"
    assert "Gemma" not in provider_display_name("bedrock")


def test_bedrock_is_not_labelled_local_ai(monkeypatch: pytest.MonkeyPatch):
    """Prompts leave the machine. Telling an operator "Local AI" would be the
    opposite of the truth about where the page content they test ends up."""
    _configure(monkeypatch)

    assert run_mode_label("bedrock") == "Cloud AI (Amazon Bedrock)"
    assert "Local" not in run_mode_label("bedrock")


def test_the_capability_mode_treats_bedrock_as_a_real_reasoning_provider():
    """No code change was needed — the existing non-mock branch already covers
    it. Asserted so a future edit to that branch cannot silently demote Bedrock
    to `exploration_only`."""
    assert provider_capability_mode("bedrock") == "real_model_reasoning"
    assert (
        provider_capability_mode("bedrock", enable_autonomous_investigation=True)
        == "scenario_execution_capable"
    )


def test_the_runtime_snapshot_reports_the_bedrock_model(monkeypatch: pytest.MonkeyPatch):
    """Before this, `model_id` came only from GEMMA_MODEL_ID (empty for Bedrock)
    or from provider health, so a configured run reported null until its first
    call."""
    _configure(monkeypatch)
    info = build_runtime_info()

    assert info["provider_type"] == "bedrock"
    assert info["provider"] == "Amazon Bedrock"
    assert info["model_id"] == MODEL
    assert info["is_mock_provider"] is False


def test_the_runtime_snapshot_never_carries_the_token(monkeypatch: pytest.MonkeyPatch):
    token = "token-that-must-not-reach-a-report"
    _configure(monkeypatch, AWS_BEARER_TOKEN_BEDROCK=token)

    assert token not in repr(build_runtime_info())


def test_no_api_base_url_is_invented_for_bedrock(monkeypatch: pytest.MonkeyPatch):
    """Bedrock's endpoint is derived by the SDK from the region; there is no
    operator-set base URL to report."""
    _configure(monkeypatch)

    assert build_runtime_info()["api_base_url"] is None


# ===========================================================================
# D -- scope: nothing outside the provider layer changed
# ===========================================================================


@pytest.mark.parametrize(
    "module",
    [
        "app/agent/planner.py",
        "app/agent/controller.py",
        "app/browser/observer.py",
        "app/browser/executor.py",
        "app/browser/manager.py",
        "app/reporting/report_builder.py",
    ],
)
def test_bedrock_is_invisible_to_every_consumer(module: str):
    """Integration is confined to the provider layer. Any CONSUMER of a provider
    naming Bedrock would mean the abstraction leaked.

    `app/gemma/base.py` was in this list during Step 4 and has been removed: it is
    the provider INTERFACE, not a consumer, so it is inside the layer this work is
    allowed to touch. Step 5 added one defaulted class attribute there
    (`supports_liveness_probe`) whose docstring names Bedrock as the motivating
    case. The shared context pipeline in that same file —
    `prepare_generation_request` — is untouched, which the next test asserts.
    """
    source = (BACKEND / module).read_text(encoding="utf-8")

    assert "bedrock" not in source.lower(), module


def test_the_shared_context_pipeline_is_untouched():
    """`base.py` is in-scope as the provider interface, but its context pipeline
    is not: every provider must keep going through the same
    `prepare_generation_request`, and no Bedrock-specific branch may appear in it.
    """
    import inspect

    from app.gemma.base import GemmaProvider

    pipeline = inspect.getsource(GemmaProvider.prepare_generation_request)

    assert "bedrock" not in pipeline.lower()
    assert "supports_liveness_probe" not in pipeline


def test_no_provider_registry_was_introduced():
    """Deferred by decision — the factory keeps its explicit if/elif chain."""
    factory = (BACKEND / "app" / "gemma" / "__init__.py").read_text(encoding="utf-8")

    assert "elif provider_name == \"bedrock\":" in factory
    for forbidden in ("PROVIDER_REGISTRY", "_REGISTRY", "register_provider"):
        assert forbidden not in factory


def test_a_stale_health_record_from_another_provider_is_not_trusted(monkeypatch: pytest.MonkeyPatch):
    """Bug found while writing Step 4's tests, and not specific to Bedrock.

    `get_active_health()` is a process-wide global set by whichever provider was
    constructed last. The old guard was `provider != "mock"`, which asks whether
    the CONFIGURED provider is mock — not whether the health record belongs to it.
    So constructing Mock and then reading runtime info under
    GEMMA_PROVIDER=bedrock reported `model_id: "mock-heuristic"` for a Bedrock run.
    """
    from app.gemma.health import ProviderHealth, set_active_health

    _configure(monkeypatch)
    set_active_health(ProviderHealth(provider_type="mock", model_id="mock-heuristic"))

    assert build_runtime_info()["model_id"] == MODEL


def test_a_matching_health_record_is_still_authoritative(monkeypatch: pytest.MonkeyPatch):
    """The health record must keep winning when it does describe this provider —
    it reflects what the provider actually resolved, config only what was asked."""
    from app.gemma.health import ProviderHealth, set_active_health

    _configure(monkeypatch)
    set_active_health(ProviderHealth(provider_type="bedrock", model_id="resolved.model.v2"))

    assert build_runtime_info()["model_id"] == "resolved.model.v2"
