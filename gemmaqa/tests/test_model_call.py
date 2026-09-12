"""The provider transport vocabulary: ModelCallRequest / ModelCallResult.

These are the TRANSPORT layer. `app.gemma.base.GenerationRequest` is the CONTEXT
layer and keeps its existing meaning — the two are deliberately separate objects,
and a test here asserts they have not been conflated, because one concept with
two implementations is the failure shape this codebase has paid for repeatedly.

The two properties worth defending by test:

1. A token count nobody reported is `None`, never `0`. "The call used no input
   tokens" and "this provider does not tell us" are different facts.
2. `telemetry()` is an allowlist. Prompt text, model output and raw payloads
   never reach a log through it, because a prompt can quote page content and page
   content can quote credentials the run typed in.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.gemma.model_call import ModelCallRequest, ModelCallResult  # noqa: E402

SECRET = "hunter2-should-never-be-logged"


# ===========================================================================
# A -- ModelCallRequest
# ===========================================================================


def test_only_the_prompt_is_required():
    """Everything else is resolved by the provider, which owns validation and
    the actionable error message when configuration is missing."""
    request = ModelCallRequest(prompt="hello")

    assert request.prompt == "hello"
    assert request.system_prompt == ""
    assert request.model == ""
    assert request.temperature == 0.0


def test_unset_decoding_knobs_are_none_not_zero():
    """`max_tokens=0` would ask for an empty completion; None means "let the
    service decide". They must not be the same value."""
    request = ModelCallRequest(prompt="hello")

    assert request.max_tokens is None
    assert request.top_p is None


def test_mutable_defaults_are_not_shared_between_requests():
    a, b = ModelCallRequest(prompt="a"), ModelCallRequest(prompt="b")
    a.stop_sequences.append("STOP")
    a.metadata["task"] = "planner"

    assert b.stop_sequences == []
    assert b.metadata == {}


def test_the_request_carries_a_routing_hint_without_interpreting_it():
    """The seam for later per-task model routing (planner -> one model, vision ->
    another) without Planner changes. This layer only carries it."""
    request = ModelCallRequest(prompt="x", metadata={"task": "vision"})

    assert request.metadata["task"] == "vision"


def test_request_telemetry_reports_prompt_sizes_never_prompt_text():
    request = ModelCallRequest(
        prompt=f"page content including {SECRET}",
        system_prompt=f"system rules including {SECRET}",
        model="some.model",
    )
    telemetry = request.telemetry()

    assert SECRET not in repr(telemetry)
    assert telemetry["prompt_chars"] == len(request.prompt)
    assert telemetry["system_prompt_chars"] == len(request.system_prompt)


def test_request_telemetry_reports_metadata_keys_never_values():
    """Metadata is caller-supplied and may carry identifiers; keys are enough to
    debug routing."""
    request = ModelCallRequest(prompt="x", metadata={"task": SECRET, "run": SECRET})
    telemetry = request.telemetry()

    assert telemetry["metadata_keys"] == ["run", "task"]
    assert SECRET not in repr(telemetry)


# ===========================================================================
# B -- ModelCallResult
# ===========================================================================


def test_only_the_text_is_required():
    result = ModelCallResult(text="answer")

    assert result.text == "answer"
    assert result.provider == ""
    assert result.stop_reason == ""


def test_unreported_usage_is_none_not_zero():
    result = ModelCallResult(text="answer")

    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.total_tokens is None
    assert result.usage_reported is False


def test_reported_usage_totals():
    result = ModelCallResult(text="answer", input_tokens=120, output_tokens=45)

    assert result.total_tokens == 165
    assert result.usage_reported is True


def test_a_genuine_zero_is_distinguishable_from_unreported():
    """The whole reason these fields are Optional."""
    reported_zero = ModelCallResult(text="", input_tokens=0, output_tokens=0)
    unreported = ModelCallResult(text="")

    assert reported_zero.total_tokens == 0
    assert reported_zero.usage_reported is True
    assert unreported.total_tokens is None
    assert unreported.usage_reported is False


def test_one_reported_side_is_still_usable():
    """A provider that reports output tokens only should not have its usage
    discarded as unknown."""
    result = ModelCallResult(text="answer", output_tokens=45)

    assert result.total_tokens == 45
    assert result.usage_reported is True


# ===========================================================================
# C -- synthesising a result for a provider that returns a plain string
# ===========================================================================


def test_from_text_wraps_an_existing_provider_output():
    """Mock / openai_compatible / transformers keep `_generate() -> str`; this is
    how a normalized result is obtained for them without a retrofit."""
    result = ModelCallResult.from_text("raw model text", provider="mock", model="none")

    assert result.text == "raw model text"
    assert result.provider == "mock"
    assert result.model == "none"


def test_from_text_cannot_invent_usage_data():
    """A synthesised result must stay honest about what it does not know — a
    plausible-looking 0 here would misreport every legacy provider's cost."""
    result = ModelCallResult.from_text("raw model text", provider="mock")

    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.stop_reason == ""
    assert result.raw_response is None
    assert result.usage_reported is False


def test_from_text_accepts_a_measured_latency():
    """Latency is measurable around any call, so it is the one field a caller
    can legitimately supply for a legacy provider."""
    result = ModelCallResult.from_text("t", provider="mock", latency_ms=42)

    assert result.latency_ms == 42


# ===========================================================================
# D -- telemetry never leaks
# ===========================================================================


def test_result_telemetry_excludes_the_model_output():
    result = ModelCallResult(text=f"the model echoed {SECRET}", provider="bedrock")
    telemetry = result.telemetry()

    assert SECRET not in repr(telemetry)
    assert telemetry["response_chars"] == len(result.text)


def test_result_telemetry_excludes_the_raw_payload():
    result = ModelCallResult(
        text="ok",
        provider="bedrock",
        raw_response={"echoed_prompt": SECRET, "authorization": SECRET},
    )

    assert SECRET not in repr(result.telemetry())


def test_the_raw_payload_stays_out_of_the_dataclass_repr():
    """`repr=False`, so an exception traceback or a debug log that prints the
    result does not dump the payload."""
    result = ModelCallResult(text="ok", raw_response={"secret": SECRET})

    assert SECRET not in repr(result)


def test_telemetry_is_an_allowlist_not_a_denylist():
    """A denylist leaks the first field someone forgets to add to it. Assert the
    exact key set so a new sensitive field cannot silently join the log."""
    assert set(ModelCallResult(text="x").telemetry()) == {
        "provider", "model", "input_tokens", "output_tokens", "total_tokens",
        "usage_reported", "stop_reason", "latency_ms", "response_chars",
    }
    assert set(ModelCallRequest(prompt="x").telemetry()) == {
        "model", "temperature", "max_tokens", "top_p", "stop_sequences",
        "prompt_chars", "system_prompt_chars", "metadata_keys",
    }


# ===========================================================================
# E -- layering
# ===========================================================================


def test_this_module_pulls_in_no_sdk():
    """Pure data, so it stays importable with no provider backend installed —
    the property that lets GEMMA_PROVIDER=mock run without AWS or transformers.
    """
    import ast

    source = (BACKEND / "app" / "gemma" / "model_call.py").read_text(encoding="utf-8")
    imported = {
        (node.module or "").split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }

    assert imported <= {"dataclasses", "typing", "__future__"}, imported


def test_the_context_layer_object_is_left_alone():
    """`GenerationRequest` belongs to the prompt pipeline and keeps its meaning.
    If these two ever converge, the transport layer has grown context concerns
    (or vice versa) and the separation this module exists for is gone."""
    from app.gemma.base import GenerationRequest

    context_fields = set(GenerationRequest.__dataclass_fields__)
    transport_fields = set(ModelCallRequest.__dataclass_fields__)

    assert "evidence" in context_fields and "evidence" not in transport_fields
    assert "model" in transport_fields and "model" not in context_fields
    assert context_fields != transport_fields
